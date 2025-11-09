from locust import HttpUser, task, between, events, LoadTestShape
import subprocess
import time
import csv
import requests
import sys
import math

# ===================================
# CONFIG
# ===================================
#PROM_URL = "http://prometheus-stack-kube-prom-prometheus.observability.svc.cluster.local:9090"
PROM_URL = "http://192.168.1.251:30090"
CACHE_TTL = 5  # secondi

# ===================================
# LOG FILE
# ===================================
log_file = open("raw_timings.csv", "w", newline="")
writer = csv.writer(log_file)
writer.writerow([
    "timestamp",
    "request_type",
    "name",
    "response_time_ms",
    "success",
    "gpu_usage_percentage",
    "http_requests_in_progress",
    "node_ip"
])
log_file.flush()

# ===================================
# PROMETHEUS HELPERS
# ===================================
_last_metrics = {"time": 0, "gpu": {}, "http": {}}
def measure_gpu_average(node_ip, duration, interval=0.5):
    values = []
    start = time.time()
    while time.time() - start < duration:
        gpu_dict, _ = get_metrics_cached()
        val = gpu_dict.get(node_ip, 0)
        values.append(val)
        time.sleep(interval)
    return sum(values)/len(values) if values else 0

def query_gpu_usage_per_node():
    """
    Ritorna {node_ip -> gpu_usage_percentage}, ignorando la porta.
    """
    try:
        r = requests.get(f"{PROM_URL}/api/v1/query",
                         params={"query": "gpu_usage_percentage"},
                         timeout=2)
        r.raise_for_status()
        results = r.json().get("data", {}).get("result", [])
        gpu_metrics = {}
        for res in results:
            instance = res["metric"].get("instance", "")
            node_ip = instance.split(":")[0]  # prendi solo IP, ignora porta
            val = float(res["value"][1])
            gpu_metrics[node_ip] = val
        return gpu_metrics
    except Exception as e:
        print(f"⚠️ Errore query Prometheus GPU: {e}")
        return {}

def query_http_in_progress_per_step():
    """Ritorna {step-name -> http_requests_in_progress}."""
    try:
        resp = requests.get(f"{PROM_URL}/api/v1/query",
                            params={"query": "http_requests_in_progress"},
                            timeout=2)
        resp.raise_for_status()
        results = resp.json().get("data", {}).get("result", [])
        step_metrics = {}
        for r in results:
            step_id = r["metric"].get("step_id")
            value = float(r["value"][1])
            if step_id is not None:
                step_name = f"step-{step_id}"
                step_metrics[step_name] = value
        return step_metrics
    except Exception as e:
        print(f"⚠️ Errore query Prometheus HTTP: {e}")
        return {}

def get_metrics_cached():
    """Caching di GPU usage per nodo e http_requests per step."""
    now = time.time()
    if now - _last_metrics["time"] > CACHE_TTL:
        _last_metrics["gpu"] = query_gpu_usage_per_node()
        _last_metrics["http"] = query_http_in_progress_per_step()
        _last_metrics["time"] = now
    return _last_metrics["gpu"], _last_metrics["http"]

# ===================================
# K8S ENTRYPOINT PER LA PIPELINE (solo step-0)
# ===================================
def get_pipeline_entrypoints():
    pod_name_result = subprocess.run(
        ["kubectl", "get", "pods", "--no-headers", "-o", "custom-columns=:metadata.name"],
        stdout=subprocess.PIPE,
        text=True,
        check=True
    )
    pod_name = next((p for p in pod_name_result.stdout.splitlines() if "step-0" in p), None)
    node_ip_result = subprocess.run(
        ["kubectl", "get", "pod", pod_name, "-o", "jsonpath={.status.hostIP}"],
        stdout=subprocess.PIPE,
        text=True,
        check=True
    )
    node_ip = node_ip_result.stdout.strip()

    svc_result = subprocess.run(
        ["kubectl", "get", "svc",
         "-o", "jsonpath={range .items[*]}{.metadata.name} {.spec.type} {.spec.ports[0].nodePort}{\"\\n\"}{end}"],
        stdout=subprocess.PIPE,
        text=True,
        check=True
    )
    entrypoints = []
    for line in svc_result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3:
            name, svc_type, node_port = parts
            if name.endswith("step-0") and svc_type == "NodePort":
                entrypoints.append((name, f"http://{node_ip}:{node_port}"))
    return entrypoints

ENTRYPOINTS = get_pipeline_entrypoints()
print("Entrypoints trovati:", ENTRYPOINTS)

# ===================================
# STEP -> NODE MAP (tutti i pod della pipeline)
# ===================================
def get_step_node_map(pipeline_prefix="pipeline-"):
    """
    Ritorna un dict {step-name -> node-ip} interrogando tutti i pod della pipeline.
    """
    pod_name_result = subprocess.run(
        ["kubectl", "get", "pods", "-n", "default", "--no-headers",
         "-o", "custom-columns=:metadata.name,:status.hostIP"],
        stdout=subprocess.PIPE,
        text=True,
        check=True
    )

    step_node_map = {}
    for line in pod_name_result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        pod_name, host_ip = parts
        if pipeline_prefix in pod_name and "step-" in pod_name:
            step_name = "-".join(pod_name.split("-")[3:5])  # es. step-0
            step_node_map[step_name] = host_ip
    return step_node_map

STEP_NODE_MAP = get_step_node_map()
print("Mappa step -> nodo:", STEP_NODE_MAP)

# ===================================
# LOG REQUEST LISTENER
# ===================================
@events.request.add_listener
def log_request(request_type, name, response_time, response_length, exception, **kwargs):
    gpu_usage_dict, http_dict = get_metrics_cached()

    # Estrai l'indice dello step se nome come "X-Step-5-Time"
    step_idx = None
    if name.startswith("X-Step-") and "-Time" in name:
        try:
            step_idx = int(name.split("-")[2])
        except:
            pass

    node_ip = None
    if step_idx is not None:
        # trova la chiave che inizia con "{step_idx}-"
        for k in STEP_NODE_MAP:
            if k.startswith(f"{step_idx}-"):
                node_ip = STEP_NODE_MAP[k]
                name = f"step-{step_idx}"  # rinomino per il CSV
                break
    if node_ip is None:
        if "step-0" in name:
            # prendi dinamicamente la chiave step-0 dal STEP_NODE_MAP
            for k in STEP_NODE_MAP:
                if k.startswith("0-"):
                    node_ip = STEP_NODE_MAP[k]
                    break

    gpu_val = gpu_usage_dict.get(node_ip, 0) if node_ip else 0
    http_val = http_dict.get(name, 0)  # solo per lo step corrente

    writer.writerow([
        time.strftime("%Y-%m-%d %H:%M:%S"),
        request_type,
        name,
        f"{response_time:.2f}",
        "OK" if exception is None else "FAIL",
        f"{gpu_val:.2f}",
        f"{http_val:.2f}",
        node_ip or "unknown"
    ])
    log_file.flush()

# ==================================
# LOCUST TASK
# ===================================
class PipelineUser(HttpUser):
    wait_time = between(1, 3)
    host = "http://dummy"

    @task
    def send_to_all(self):
        if not ENTRYPOINTS:
            return
        image_file = "your_image.jpg"
        for name, base_url in ENTRYPOINTS:
            self.client.base_url = base_url
            with open(image_file, "rb") as f:
                files = {"image": (image_file, f, "image/jpeg")}
                with self.client.post(
                    "/process",
                    files=files,
                    name=name,
                    catch_response=True
                ) as resp:
                    if resp.status_code == 200:
                        step_times = {
                            k: float(v) for k, v in resp.headers.items() if k.startswith("X-Step-")
                        }
                        resp.success()
                        for step, elapsed in step_times.items():
                            # step es. "X-Step-0-Time"
                            step_idx = int(step.split("-")[1])
                            node_ip = None
                            for k in STEP_NODE_MAP:
                               if k.startswith(f"{step_idx}-"):
                                  node_ip = STEP_NODE_MAP[k]
                                  step_name = f"step-{step_idx}"
                                  break
                            # calcolo GPU media durante l'elaborazione
                            gpu_avg = measure_gpu_average(node_ip, elapsed) if node_ip else 0

                            events.request.fire(
                                request_type="STEP",
                                name=step_name,
                                response_time=elapsed * 1000,  # ms
                                response_length=0,
                                exception=None,
                                context={"gpu_usage": gpu_avg, "node_ip": node_ip},
                            )
                    else:
                        resp.failure(f"Errore {resp.status_code}")

# ===================================
# OPTIONAL: CUSTOM LOAD SHAPE
# ===================================
USE_CUSTOM_SHAPE = any(arg.startswith("--curve") for arg in sys.argv)

if USE_CUSTOM_SHAPE:
    CURVE_TYPE = "ramp"
    CURVE_USERS = 20
    CURVE_DURATION = 60
    CURVE_SPAWN_RATE = 2

    @events.init_command_line_parser.add_listener
    def _(parser):
        parser.add_argument("--curve", type=str, default=CURVE_TYPE)
        parser.add_argument("--curve-users", type=int, default=CURVE_USERS)
        parser.add_argument("--curve-duration", type=int, default=CURVE_DURATION)
        parser.add_argument("--curve-spawn-rate", type=float, default=CURVE_SPAWN_RATE)

    @events.init.add_listener
    def _(environment, **kwargs):
        global CURVE_TYPE, CURVE_USERS, CURVE_DURATION, CURVE_SPAWN_RATE
        CURVE_TYPE = getattr(environment.parsed_options, "curve", CURVE_TYPE)
        CURVE_USERS = getattr(environment.parsed_options, "curve_users", CURVE_USERS)
        CURVE_DURATION = getattr(environment.parsed_options, "curve_duration", CURVE_DURATION)
        CURVE_SPAWN_RATE = getattr(environment.parsed_options, "curve_spawn_rate", CURVE_SPAWN_RATE)

    class CustomShape(LoadTestShape):
        def tick(self):
            run_time = self.get_run_time()
            if run_time > CURVE_DURATION:
                return None
            if CURVE_TYPE == "ramp":
                current_users = int(CURVE_USERS * run_time / CURVE_DURATION)
            elif CURVE_TYPE == "step":
                step_time = CURVE_DURATION / 5
                step_level = int(run_time // step_time)
                current_users = int((step_level + 1) * CURVE_USERS / 5)
            elif CURVE_TYPE == "spike":
                if run_time < CURVE_DURATION / 4 or run_time > 3 * CURVE_DURATION / 4:
                    current_users = int(CURVE_USERS / 10)
                else:
                    current_users = CURVE_USERS
            elif CURVE_TYPE == "sinus":
                current_users = int((CURVE_USERS / 2) * (1 + math.sin(run_time / CURVE_DURATION * 2 * math.pi)))
            else:
                current_users = CURVE_USERS
            return (current_users, CURVE_SPAWN_RATE)
