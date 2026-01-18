import csv
import json
import math
import subprocess
import sys
import threading
import time
import uuid
import requests
import random
import os
from locust import HttpUser, task, events, LoadTestShape
from locust.clients import HttpSession
from urllib.parse import urlparse


# ==========================
# CONFIG
# ==========================
PROM_URL = "http://192.168.1.251:30090"
CACHE_TTL = 5

# Test id unico per ogni run Locust
TEST_ID = f"locust-{uuid.uuid4().hex[:8]}"
TEST_START_TS = None
TEST_STOP_TS = None
REALTIME_RUNNING = True
LOAD_PROFILE = "light"

# ==========================
# PROM QUERIES (GLOBALI)
# ==========================

# finestra abbastanza grande per step lenti (Nano)
TS_WINDOW = "10m"

Q_RPS_TS = lambda test_id: f'''
sum(rate(step_processing_time_seconds_count{{test_id="{test_id}"}}[{TS_WINDOW}])) by (step_id)
'''

Q_P95_TS = lambda test_id: f'''
histogram_quantile(
  0.95,
  sum(rate(step_processing_time_seconds_bucket{{test_id="{test_id}"}}[{TS_WINDOW}])) by (le, step_id)
)
'''

# ==========================
# FILES
# ==========================
raw_file = open("raw_timings.csv", "w", newline="")
raw_writer = csv.writer(raw_file)
raw_writer.writerow([
    "timestamp",
    "test_id",
    "request_type",
    "name",
    "response_time_ms",
    "success",
    "status_code",
    "gpu_usage_percentage",
    "http_requests_in_progress",
    "node_ip",
    "load_profile"
])
raw_file.flush()

# ==========================
# PROM CACHE
# ==========================
_last_metrics = {"time": 0, "gpu": {}, "http": {}}

# GLOBAL RPS
_last_req_count = 0
_last_ts = None

def prom_query_instant(query: str, timeout=5):
    r = requests.get(f"{PROM_URL}/api/v1/query", params={"query": query}, timeout=timeout)
    r.raise_for_status()
    return r.json().get("data", {}).get("result", [])
    
def prom_query_range(query, start, end, step):
    r = requests.get(
        f"{PROM_URL}/api/v1/query_range",
        params={
            "query": query,
            "start": start,
            "end": end,
            "step": step,
        },
        timeout=10
    )
    r.raise_for_status()
    return r.json()["data"]["result"]
def export_prom_timeseries(test_id, start_ts, end_ts):
    step = 60  # 1 punto al minuto
  
    rps_ts = prom_query_range(Q_RPS_TS(test_id), start_ts, end_ts, step)
    p95_ts = prom_query_range(Q_P95_TS(test_id), start_ts, end_ts, step)


    with open("prom_timeseries.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["test_id", "timestamp", "step_id", "rps", "p95_s"])

        # indicizza per step_id + timestamp
        tmp = {}

        for series in rps_ts:
            sid = series["metric"]["step_id"]
            for ts, val in series["values"]:
                tmp.setdefault((sid, ts), {})["rps"] = float(val)

        for series in p95_ts:
            sid = series["metric"]["step_id"]
            for ts, val in series["values"]:
                tmp.setdefault((sid, ts), {})["p95"] = float(val)

        for (sid, ts), vals in sorted(tmp.items()):
            w.writerow([
                test_id,
                int(float(ts)),
                sid,
                vals.get("rps", 0),
                vals.get("p95", 0),
            ])

def query_gpu_usage_per_node():
    try:
        results = prom_query_instant("gpu_usage_percentage")
        gpu = {}
        for res in results:
            instance = res["metric"].get("instance", "")
            node_ip = instance.split(":")[0]
            gpu[node_ip] = float(res["value"][1])
        return gpu
    except Exception as e:
        print("⚠️ GPU query error:", e)
        return {}
def build_node_name_map():
    out = subprocess.run(
        ["kubectl", "get", "nodes", "-o", "wide"],
        stdout=subprocess.PIPE, text=True, check=True
    )
    mapping = {}
    lines = out.stdout.splitlines()
    header = lines[0].split()
    name_idx = header.index("NAME")
    ip_idx = header.index("INTERNAL-IP")

    for line in lines[1:]:
        parts = line.split()
        if len(parts) > max(name_idx, ip_idx):
            mapping[parts[ip_idx]] = parts[name_idx]
    return mapping
NODE_NAME_MAP = build_node_name_map()
def get_node_ips():
    out = subprocess.run(
        [
            "kubectl", "get", "nodes",
            "-o",
            "jsonpath={range .items[*]}{.status.addresses[?(@.type=='InternalIP')].address}{'\\n'}{end}"
        ],
        stdout=subprocess.PIPE,
        text=True,
        check=True
    )
    return [ip.strip() for ip in out.stdout.splitlines() if ip.strip()]

def query_http_in_progress_per_step():
    try:
        results = prom_query_instant("http_requests_in_progress")
        http = {}
        for res in results:
            step_id = res["metric"].get("step_id")
            if step_id is not None and step_id != "":
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

# ==========================
# DISCOVER ENTRYPOINTS
# ==========================
NODE_PORT = 32400  # step-0 NodePort fisso
NODE_IPS = get_node_ips()
def get_pipeline_entrypoints():
    if not NODE_IPS:
        return []
    # basta UN endpoint, kube-proxy fa il resto
    node_ip = random.choice(NODE_IPS)
    return [("step-0", f"http://{node_ip}:{NODE_PORT}")]
# def get_pipeline_entrypoints():
#     pod_list = subprocess.run(
#         ["kubectl", "get", "pods", "--no-headers", "-o", "custom-columns=:metadata.name"],
#         stdout=subprocess.PIPE, text=True, check=True
#     )
#     pod = next((p for p in pod_list.stdout.splitlines() if "step-0" in p), None)
#     if not pod:
#         return []

#     node_ip = subprocess.run(
#         ["kubectl", "get", "pod", pod, "-o", "jsonpath={.status.hostIP}"],
#         stdout=subprocess.PIPE, text=True, check=True
#     ).stdout.strip()

#     svc_list = subprocess.run(
#         ["kubectl", "get", "svc",
#          "-o", "jsonpath={range .items[*]}{.metadata.name} {.spec.type} {.spec.ports[0].nodePort}{\"\\n\"}{end}"],
#         stdout=subprocess.PIPE, text=True, check=True
#     )

#     entry = []
#     for line in svc_list.stdout.splitlines():
#         parts = line.split()
#         if len(parts) == 3:
#             name, svc_type, node_port = parts
#             if name.endswith("step-0") and svc_type == "NodePort":
#                 entry.append((name, f"http://{node_ip}:{node_port}"))
#     return entry

ENTRYPOINTS = get_pipeline_entrypoints()
print("Entrypoints:", ENTRYPOINTS)
print("🧪 TEST_ID:", TEST_ID)

# ==========================
# STEP -> NODE MAP
# ==========================

def get_step_node_map(prefix="pipeline-"):
    out = subprocess.run(
        ["kubectl", "get", "pods", "-n", "default", "--no-headers",
         "-o", "custom-columns=:metadata.name,:status.hostIP"],
        stdout=subprocess.PIPE, text=True, check=True
    )
    mapping = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        pod, host_ip = parts
        if prefix in pod and "step-" in pod:
            step_name = "-".join(pod.split("-")[3:5])  # es. "0-xxxxxx"
            mapping[step_name] = host_ip
    return mapping

STEP_NODE_MAP = get_step_node_map()
print("Step → Node:", STEP_NODE_MAP)

# def node_ip_for_step(step_idx: int):
#     # la tua mappa ha chiavi tipo "0-<podhash>"
#     for k, v in STEP_NODE_MAP.items():
#         if k.startswith(f"{step_idx}-"):
#             return v
#     return None

# ==========================
# REQUEST LISTENER -> raw_timings.csv
# ==========================
def pick_load_profile():
    """
    Ritorna il profilo di carico per questa request.
    - light  → 100% light
    - heavy  → 100% heavy
    - medium → 70% heavy, 30% light
    """
    if LOAD_PROFILE == "medium":
        return "heavy" if random.random() < 0.7 else "light"
    return LOAD_PROFILE

def node_ip_from_base_url():
    try:
        if not ENTRYPOINTS:
            return None
        _, base_url = ENTRYPOINTS[0]
        return urlparse(base_url).hostname
    except Exception:
        return None
@events.request.add_listener
def log_request(request_type, name, response_time, response_length, exception, **kwargs):
    gpu_dict, http_dict = get_metrics_cached()

    # nodo a cui Locust ha mandato la request (NON dove gira il pod)
    node_ip = node_ip_from_base_url()

    gpu_val = gpu_dict.get(node_ip, 0) if node_ip else 0
    http_val = http_dict.get("step-0", 0)

    status_code = (
        kwargs.get("response").status_code
        if kwargs.get("response") is not None
        else ""
    )

    raw_writer.writerow([
        time.strftime("%Y-%m-%d %H:%M:%S"),
        TEST_ID,
        request_type,
        name,
        f"{response_time:.2f}",
        "OK" if exception is None else "FAIL",
        status_code,
        f"{gpu_val:.2f}",
        f"{http_val:.2f}",
        node_ip or "cluster",
        LOAD_PROFILE,
    ])
    raw_file.flush()

# @events.request.add_listener
# def log_request(request_type, name, response_time, response_length, exception, **kwargs):
#     gpu_dict, http_dict = get_metrics_cached()

#     # Per questa run, logghiamo solo:
#     # - la POST verso step-0 (end-to-end)
#     # - eventuali errori
#     # step0_ip = node_ip_for_step(0)
#     # node_ip = step0_ip

#     gpu_val = gpu_dict.get(node_ip, 0) if node_ip else 0
#     http_val = http_dict.get("step-0", 0)

#     status_code = kwargs.get("response").status_code if kwargs.get("response") is not None else ""

#     raw_writer.writerow([
#         time.strftime("%Y-%m-%d %H:%M:%S"),
#         TEST_ID,
#         request_type,
#         name,
#         f"{response_time:.2f}",
#         "OK" if exception is None else "FAIL",
#         status_code,
#         f"{gpu_val:.2f}",
#         f"{http_val:.2f}",
#         node_ip or "unknown",
#         LOAD_PROFILE,
#     ])
#     raw_file.flush()

# ==========================
# USER
# ==========================
class PipelineUser(HttpUser):
    wait_time = lambda self: 0
    host = "http://dummy"
  
  def on_start(self):
      self.client = HttpSession(
          base_url=self.host,
          request_event=self.environment.events.request,
          user=self,
          pool_manager_kwargs={"retries": False}
      )
      self.client.headers.update({"Connection": "close"})
      
    @task
    def send_to_all(self):
        if not ENTRYPOINTS:
            return

        img = "your_image.jpg"
        profile = pick_load_profile()

        headers = {
            "X-Test-ID": TEST_ID,
            "X-Load-Profile": profile
        }
        for name, base_url in ENTRYPOINTS:
            self.client.base_url = base_url
            with open(img, "rb") as f:
                files = {"image": (img, f, "image/jpeg")}
                # ora lo step risponde 202 quasi subito
                with self.client.post(
                    "/process",
                    files=files,
                    headers=headers,
                    name=name,
                    catch_response=True
                ) as resp:
                    if resp.status_code in (200, 202):
                        resp.success()
                    else:
                        resp.failure(f"HTTP {resp.status_code}")

# ==========================
# REALTIME LOCUST (rps/users)
# ==========================
def export_realtime_metrics(environment):
    global _last_req_count, _last_ts

    if not REALTIME_RUNNING:
        return

    now = time.time()
    total_reqs = environment.stats.total.num_requests

    if _last_ts is None:
        rps = 0.0
    else:
        dt = now - _last_ts
        rps = (total_reqs - _last_req_count) / dt if dt > 0 else 0.0

    _last_req_count = total_reqs
    _last_ts = now

    with open("realtime_rps.csv", "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            now,
            TEST_ID,
            round(rps, 3),
            environment.stats.total.fail_ratio,
            environment.runner.user_count if environment.runner else 0
        ])

    threading.Timer(1, export_realtime_metrics, args=[environment]).start()

@events.test_start.add_listener
def on_test_start(environment, **_):
    global TEST_START_TS
    TEST_START_TS = time.time()

    with open("realtime_rps.csv", "w", newline="") as f:
        csv.writer(f).writerow(["timestamp", "test_id", "rps", "fail_ratio", "users"])
    export_realtime_metrics(environment)

# ==========================
# PROMETHEUS EXPORT PER TEST_ID (FINE TEST)
# ==========================
def prom_export_summary(test_id: str, duration_s: int):
    """
    Esporta in prom_summary.csv:
    - avg step time (seconds)
    - p95 step time (seconds)
    - rps (requests/sec) per step, dal count dell'Histogram
    Tutto filtrato per test_id.
    """
    dur = max(10, int(duration_s))  # evita range troppo corto
    rng = f"{dur}s"

    q_avg = f'''
    sum(increase(step_processing_time_seconds_sum{{test_id="{test_id}"}}[{rng}])) by (step_id)
    /
    sum(increase(step_processing_time_seconds_count{{test_id="{test_id}"}}[{rng}])) by (step_id)
    '''


    q_p95 = f'''
    histogram_quantile(
      0.95,
      sum(increase(step_processing_time_seconds_bucket{{test_id="{test_id}"}}[{rng}])) by (le, step_id)
    )
    '''
  
    q_rps = f'''
    sum(increase(step_processing_time_seconds_count{{test_id="{test_id}"}}[{rng}])) by (step_id)
    / {dur}
    '''
    q_gpu_60s = '''
    avg_over_time(
      gpu_usage_percentage{job="jetson-exporter"}[60s]
    )
    '''
    
    q_gpu_test = f'''
    avg_over_time(
      gpu_usage_percentage{{job="jetson-exporter"}}[{dur}s]
    )
    '''




    avg_res = prom_query_instant(q_avg)
    p95_res = prom_query_instant(q_p95)
    rps_res = prom_query_instant(q_rps)
    gpu_60s_res = prom_query_instant(q_gpu_60s)
    gpu_test_res = prom_query_instant(q_gpu_test)
    gpu_60s_map = {}
    gpu_test_map = {}
    avg_map = {row["metric"].get("step_id", "unknown"): float(row["value"][1]) for row in avg_res}
    p95_map = {row["metric"].get("step_id", "unknown"): float(row["value"][1]) for row in p95_res}
    rps_map = {row["metric"].get("step_id", "unknown"): float(row["value"][1]) for row in rps_res}

    for r in gpu_60s_res:
      inst = r["metric"].get("instance", "")
      node_ip = inst.split(":")[0]
      gpu_60s_map[node_ip] = float(r["value"][1])

    for r in gpu_test_res:
      inst = r["metric"].get("instance", "")
      node_ip = inst.split(":")[0]
      gpu_test_map[node_ip] = float(r["value"][1])

    # ------------------------
    # WRITE GPU SUMMARY
    # ------------------------
    write_header = not os.path.exists("gpu_summary.csv")

    with open("gpu_summary.csv", "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow([
                "test_id",
                "duration_s",
                "node_ip",
                "gpu_avg_60s",
                "gpu_avg_test",
                "load_profile"
            ])

        all_nodes = set(gpu_60s_map) | set(gpu_test_map)
        for node in sorted(all_nodes):
            w.writerow([
                test_id,
                dur,
                node,
                f"{gpu_60s_map.get(node, 0):.2f}",
                f"{gpu_test_map.get(node, 0):.2f}",
                LOAD_PROFILE,
            ])
    # =========================
    # GPU BY CLASS (nano / orin)
    # =========================
    
    # mappa node_name -> gpu_class
    NODE_GPU_CLASS = {}
    for node_name in NODE_NAME_MAP.values():
        lname = node_name.lower()
        if "nano" in lname:
            NODE_GPU_CLASS[node_name] = "nano"
        elif "orin" in lname or "jetson" in lname:
            NODE_GPU_CLASS[node_name] = "orin"
    
    # solo nodi coinvolti nella pipeline (IP)
    pipeline_node_ips = set(STEP_NODE_MAP.values())
    
    gpu_by_class_60s = {}
    gpu_by_class_test = {}
    
    for node_ip, gpu_val in gpu_60s_map.items():
        if node_ip not in pipeline_node_ips:
            continue
    
        node_name = NODE_NAME_MAP.get(node_ip)
        if not node_name:
            continue
    
        gpu_class = NODE_GPU_CLASS.get(node_name)
        if not gpu_class:
            continue
    
        gpu_by_class_60s.setdefault(gpu_class, []).append(gpu_val)
    
    for node_ip, gpu_val in gpu_test_map.items():
        if node_ip not in pipeline_node_ips:
            continue
    
        node_name = NODE_NAME_MAP.get(node_ip)
        if not node_name:
            continue
    
        gpu_class = NODE_GPU_CLASS.get(node_name)
        if not gpu_class:
            continue
    
        gpu_by_class_test.setdefault(gpu_class, []).append(gpu_val)
    
    avg_gpu_by_class_60s = {
        k: sum(v) / len(v) if v else 0
        for k, v in gpu_by_class_60s.items()
    }
    
    avg_gpu_by_class_test = {
        k: sum(v) / len(v) if v else 0
        for k, v in gpu_by_class_test.items()
    }
    
    # ------------------------
    # WRITE GPU BY CLASS CSV
    # ------------------------
    write_header = not os.path.exists("gpu_by_device.csv")
    
    
    with open("gpu_by_device.csv", "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow([
                "test_id",
                "gpu_class",
                "avg_gpu_60s",
                "avg_gpu_test",
                "load_profile"
            ])
    
        for gpu_class in sorted(set(avg_gpu_by_class_60s) | set(avg_gpu_by_class_test)):
            w.writerow([
                test_id,
                gpu_class,
                f"{avg_gpu_by_class_60s.get(gpu_class, 0):.2f}",
                f"{avg_gpu_by_class_test.get(gpu_class, 0):.2f}",
                LOAD_PROFILE,
            ])

    step_ids = sorted(set(avg_map.keys()) | set(p95_map.keys()) | set(rps_map.keys()))
  
    with open("prom_summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["test_id", "duration_s", "step_id", "avg_s", "p95_s", "rps","load_profile"])
        for sid in step_ids:
            w.writerow([
                test_id,
                dur,
                sid,
                f"{avg_map.get(sid, 0):.6f}",
                f"{p95_map.get(sid, 0):.6f}",
                f"{rps_map.get(sid, 0):.6f}",
                LOAD_PROFILE,
            ])

    print("📁 Salvato: prom_summary.csv (scoped by test_id)")

@events.test_stop.add_listener
def on_test_stop(environment, **_):
    global TEST_STOP_TS
    TEST_STOP_TS = time.time()
    global REALTIME_RUNNING
    REALTIME_RUNNING = False
    duration_s = int(TEST_STOP_TS - (TEST_START_TS or TEST_STOP_TS))
    print(f"📊 Export Prometheus per TEST_ID={TEST_ID}, duration={duration_s}s")

    try:
        prom_export_summary(TEST_ID, duration_s)
        export_prom_timeseries(TEST_ID, TEST_START_TS, TEST_STOP_TS )
    except Exception as e:
        print("⚠️ prom_export_summary failed:", e)

# ==========================
# LOAD SHAPE
# ==========================
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
        parser.add_argument(
            "--load-profile",
            choices=["light", "medium", "heavy"],
            default="light",
            help="GPU load profile for requests"
        )

    @events.init.add_listener
    def _(environment, **kwargs):
        global CURVE_TYPE, CURVE_USERS, CURVE_DURATION, CURVE_SPAWN_RATE, LOAD_PROFILE
        CURVE_TYPE = environment.parsed_options.curve
        CURVE_USERS = environment.parsed_options.curve_users
        CURVE_DURATION = environment.parsed_options.curve_duration
        CURVE_SPAWN_RATE = environment.parsed_options.curve_spawn_rate
        LOAD_PROFILE = environment.parsed_options.load_profile

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
                users = CURVE_USERS if (CURVE_DURATION/4 <= t <= 3*CURVE_DURATION/4) else int(CURVE_USERS/10)
            elif CURVE_TYPE == "sinus":
                users = int((CURVE_USERS/2) * (1 + math.sin(t / CURVE_DURATION * 2 * math.pi)))
            else:
                users = CURVE_USERS

            return users, CURVE_SPAWN_RATE
