import csv
import json
import math
import subprocess
import sys
import threading
import time
import requests

from locust import HttpUser, task, between, events, LoadTestShape

class MinimalJSONEncoder(json.JSONEncoder):
    def default(self, o):
        try:
            return o.__dict__
        except:
            return str(o)

# ===================================
# CONFIG
# ===================================
PROM_URL = "http://192.168.1.251:30090"
CACHE_TTL = 5  # secondi

# ===================================
# LOG FILE (raw timings)
# ===================================
raw_file = open("raw_timings.csv", "w", newline="")
raw_writer = csv.writer(raw_file)
raw_writer.writerow([
    "timestamp",
    "request_type",
    "name",
    "response_time_ms",
    "success",
    "gpu_usage_percentage",
    "http_requests_in_progress",
    "node_ip"
])
raw_file.flush()

# ===================================
# PROMETHEUS HELPERS
# ===================================
_last_metrics = {"time": 0, "gpu": {}, "http": {}}


def query_gpu_usage_per_node():
    try:
        r = requests.get(f"{PROM_URL}/api/v1/query",
                         params={"query": "gpu_usage_percentage"},
                         timeout=2)
        r.raise_for_status()
        results = r.json().get("data", {}).get("result", [])
        gpu = {}
        for res in results:
            instance = res["metric"].get("instance", "")
            node_ip = instance.split(":")[0]
            gpu[node_ip] = float(res["value"][1])
        return gpu
    except Exception as e:
        print("⚠️ GPU query error:", e)
        return {}


def query_http_in_progress_per_step():
    try:
        r = requests.get(f"{PROM_URL}/api/v1/query",
                         params={"query": "http_requests_in_progress"},
                         timeout=2)
        r.raise_for_status()
        results = r.json().get("data", {}).get("result", [])
        http = {}
        for res in results:
            step_id = res["metric"].get("step_id")
            if step_id:
                http[f"step-{step_id}"] = float(res["value"][1])
        return http
    except Exception as e:
        print("⚠️ HTTP query error:", e)
        return {}


def get_metrics_cached():
    now = time.time()
    if now - _last_metrics["time"] > CACHE_TTL:
        _last_metrics["gpu"] = query_gpu_usage_per_node()
        _last_metrics["http"] = query_http_in_progress_per_step()
        _last_metrics["time"] = now
    return _last_metrics["gpu"], _last_metrics["http"]


# ===================================
# DISCOVER ENTRYPOINTS (step-0 services)
# ===================================
def get_pipeline_entrypoints():
    pod_list = subprocess.run(
        ["kubectl", "get", "pods", "--no-headers", "-o", "custom-columns=:metadata.name"],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    )
    pod = next((p for p in pod_list.stdout.splitlines() if "step-0" in p), None)

    node_ip = subprocess.run(
        ["kubectl", "get", "pod", pod, "-o", "jsonpath={.status.hostIP}"],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout.strip()

    svc_list = subprocess.run(
        [
            "kubectl",
            "get",
            "svc",
            "-o",
            "jsonpath={range .items[*]}{.metadata.name} {.spec.type} {.spec.ports[0].nodePort}{\"\\n\"}{end}",
        ],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    )

    entry = []
    for line in svc_list.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3:
            name, svc_type, node_port = parts
            if name.endswith("step-0") and svc_type == "NodePort":
                entry.append((name, f"http://{node_ip}:{node_port}"))
    return entry


ENTRYPOINTS = get_pipeline_entrypoints()
print("Entrypoints:", ENTRYPOINTS)

# ===================================
# MAPPA STEP -> NODE
# ===================================
def get_step_node_map(prefix="pipeline-"):
    out = subprocess.run(
        ["kubectl", "get", "pods", "-n", "default", "--no-headers",
         "-o", "custom-columns=:metadata.name,:status.hostIP"],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    )

    mapping = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        pod, host_ip = parts
        if prefix in pod and "step-" in pod:
            step_name = "-".join(pod.split("-")[3:5])
            mapping[step_name] = host_ip
    return mapping


STEP_NODE_MAP = get_step_node_map()
print("Step → Node:", STEP_NODE_MAP)

# ===================================
# LOG REQUEST LISTENER
# ===================================
@events.request.add_listener
def log_request(request_type, name, response_time, response_length, exception, **kwargs):
    gpu_dict, http_dict = get_metrics_cached()
    step_idx = None

    if name.startswith("X-Step-") and "-Time" in name:
        try:
            step_idx = int(name.split("-")[2])
        except:
            pass

    node_ip = None
    if step_idx is not None:
        for k in STEP_NODE_MAP:
            if k.startswith(f"{step_idx}-"):
                node_ip = STEP_NODE_MAP[k]
                name = f"step-{step_idx}"
                break

    if node_ip is None and "step-0" in name:
        for k in STEP_NODE_MAP:
            if k.startswith("0-"):
                node_ip = STEP_NODE_MAP[k]
                break

    gpu_val = gpu_dict.get(node_ip, 0)
    http_val = http_dict.get(name, 0)

    raw_writer.writerow([
        time.strftime("%Y-%m-%d %H:%M:%S"),
        request_type,
        name,
        f"{response_time:.2f}",
        "OK" if exception is None else "FAIL",
        f"{gpu_val:.2f}",
        f"{http_val:.2f}",
        node_ip or "unknown"
    ])
    raw_file.flush()

# ===================================
# LOCUST USER
# ===================================
class PipelineUser(HttpUser):
    wait_time = between(1, 3)
    host = "http://dummy"

    @task
    def send_to_all(self):
        if not ENTRYPOINTS:
            return

        img = "your_image.jpg"

        for name, base_url in ENTRYPOINTS:
            self.client.base_url = base_url
            with open(img, "rb") as f:
                files = {"image": (img, f, "image/jpeg")}
                with self.client.post("/process", files=files, name=name, catch_response=True) as resp:
                    if resp.status_code == 200:
                        step_times = {k: float(v) for k, v in resp.headers.items() if k.startswith("X-Step-")}
                        resp.success()

                        for step, elapsed in step_times.items():
                            try:
                                step_idx = int(step.split("-")[2])
                            except:
                                continue

                            node_ip = None
                            for k in STEP_NODE_MAP:
                                if k.startswith(f"{step_idx}-"):
                                    node_ip = STEP_NODE_MAP[k]
                                    break

                            gpu_avg = 0
                            if node_ip:
                                interval = max(1, int(elapsed))
                                q = f'avg_over_time(gpu_usage_percentage{{instance="{node_ip}:9401"}}[{interval}s])'
                                try:
                                    r = requests.get(f"{PROM_URL}/api/v1/query", params={"query": q}, timeout=2)
                                    result = r.json().get("data", {}).get("result", [])
                                    if result:
                                        gpu_avg = float(result[0]["value"][1])
                                except:
                                    gpu_avg = 0

                            events.request.fire(
                                request_type="STEP",
                                name=step,
                                response_time=elapsed * 1000,
                                response_length=0,
                                exception=None,
                                context={"gpu_avg": gpu_avg, "node": node_ip}
                            )
                    else:
                        resp.failure(f"HTTP {resp.status_code}")

# ===================================
# EXPORT METRICHE LOCUST FINALI
# ===================================
@events.test_stop.add_listener
def export_locust_stats(environment, **_kwargs):
    print("📊 Esporto metriche Locust...")

    # --------- JSON AGGREGATO PRINCIPALE ---------
    with open("locust_stats.json", "w") as f:
        try:
            # versioni recenti di Locust
            stats = environment.stats.serialize_stats()
        except AttributeError:
            # fallback per versioni più vecchie
            stats = {
                "entries": [
                    {
                        "name": s.name,
                        "method": s.method,
                        "num_requests": s.num_reqs,
                        "num_failures": s.num_failures,
                        "avg_response_time": s.avg_response_time,
                        "min_response_time": s.min_response_time,
                        "max_response_time": s.max_response_time,
                        "median_response_time": s.median_response_time,
                        "p95": s.get_response_time_percentile(0.95),
                        "rps": s.total_rps,
                    }
                    for s in environment.stats.entries.values()
                ]
            }

        json.dump(stats, f, cls=MinimalJSONEncoder, indent=2)

    # --------- STORIA TEMPORALE (SE SUPPORTATA) ---------
    with open("locust_times.json", "w") as f:
        try:
            history = environment.stats.serialize_stats_history()
        except AttributeError:
            history = {}  # la tua versione di Locust non la supporta

        json.dump(history, f, cls=MinimalJSONEncoder, indent=2)

    # --------- CSV AGGREGATO ---------
    with open("locust_summary.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "name", "method", "requests", "failures",
            "avg", "min", "max", "median", "p95", "rps"
        ])
        for s in environment.stats.entries.values():
            writer.writerow([
                s.name,
                s.method,
                s.num_reqs,
                s.num_failures,
                s.avg_response_time,
                s.min_response_time,
                s.max_response_time,
                s.median_response_time,
                s.get_response_time_percentile(0.95),
                s.total_rps
            ])

    print("📁 Salvati: locust_stats.json, locust_times.json, locust_summary.csv")

# ===================================
# RPS REALTIME LOGGING
# ===================================
def export_realtime_metrics(environment):
    with open("realtime_rps.csv", "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            time.time(),
            environment.stats.total.current_rps,
            environment.stats.total.fail_ratio,
            environment.runner.user_count if environment.runner else 0
        ])
    threading.Timer(1, export_realtime_metrics, args=[environment]).start()


@events.test_start.add_listener
def on_test_start(environment, **_):
    with open("realtime_rps.csv", "w", newline="") as f:
        csv.writer(f).writerow(["timestamp", "rps", "fail_ratio", "users"])
    export_realtime_metrics(environment)

# ===================================
# OPTIONAL: CUSTOM SHAPE
# ===================================
USE_SHAPE = any(arg.startswith("--curve") for arg in sys.argv)

if USE_SHAPE:
    CURVE_TYPE = "ramp"
    CURVE_USERS = 20
    CURVE_DURATION = 60
    CURVE_SPAWN_RATE = 2

    @events.init_command_line_parser.add_listener
    def _(parser):
        parser.add_argument("--curve", default=CURVE_TYPE)
        parser.add_argument("--curve-users", type=int, default=CURVE_USERS)
        parser.add_argument("--curve-duration", type=int, default=CURVE_DURATION)
        parser.add_argument("--curve-spawn-rate", type=float, default=CURVE_SPAWN_RATE)

    @events.init.add_listener
    def _(environment, **kwargs):
        global CURVE_TYPE, CURVE_USERS, CURVE_DURATION, CURVE_SPAWN_RATE
        CURVE_TYPE = environment.parsed_options.curve
        CURVE_USERS = environment.parsed_options.curve_users
        CURVE_DURATION = environment.parsed_options.curve_duration
        CURVE_SPAWN_RATE = environment.parsed_options.curve_spawn_rate

    class CustomShape(LoadTestShape):
        def tick(self):
            t = self.get_run_time()
            if t > CURVE_DURATION:
                return None
            if CURVE_TYPE == "ramp":
                users = int(CURVE_USERS * t / CURVE_DURATION)
            elif CURVE_TYPE == "step":
                step = CURVE_DURATION / 5
                lvl = int(t // step)
                users = int((lvl + 1) * CURVE_USERS / 5)
            elif CURVE_TYPE == "spike":
                users = CURVE_USERS if CURVE_DURATION/4 <= t <= 3*CURVE_DURATION/4 else int(CURVE_USERS/10)
            elif CURVE_TYPE == "sinus":
                users = int((CURVE_USERS/2) * (1 + math.sin(t / CURVE_DURATION * 2 * math.pi)))
            else:
                users = CURVE_USERS
            return users, CURVE_SPAWN_RATE
