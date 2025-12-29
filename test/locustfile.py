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

from locust import HttpUser, task, events, LoadTestShape



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
    "node_ip"
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
def get_pipeline_entrypoints():
    pod_list = subprocess.run(
        ["kubectl", "get", "pods", "--no-headers", "-o", "custom-columns=:metadata.name"],
        stdout=subprocess.PIPE, text=True, check=True
    )
    pod = next((p for p in pod_list.stdout.splitlines() if "step-0" in p), None)
    if not pod:
        return []

    node_ip = subprocess.run(
        ["kubectl", "get", "pod", pod, "-o", "jsonpath={.status.hostIP}"],
        stdout=subprocess.PIPE, text=True, check=True
    ).stdout.strip()

    svc_list = subprocess.run(
        ["kubectl", "get", "svc",
         "-o", "jsonpath={range .items[*]}{.metadata.name} {.spec.type} {.spec.ports[0].nodePort}{\"\\n\"}{end}"],
        stdout=subprocess.PIPE, text=True, check=True
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

def node_ip_for_step(step_idx: int):
    # la tua mappa ha chiavi tipo "0-<podhash>"
    for k, v in STEP_NODE_MAP.items():
        if k.startswith(f"{step_idx}-"):
            return v
    return None

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
  
@events.request.add_listener
def log_request(request_type, name, response_time, response_length, exception, **kwargs):
    gpu_dict, http_dict = get_metrics_cached()

    # Per questa run, logghiamo solo:
    # - la POST verso step-0 (end-to-end)
    # - eventuali errori
    step0_ip = node_ip_for_step(0)
    node_ip = step0_ip

    gpu_val = gpu_dict.get(node_ip, 0) if node_ip else 0
    http_val = http_dict.get("step-0", 0)

    status_code = kwargs.get("response").status_code if kwargs.get("response") is not None else ""

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
        node_ip or "unknown",
    ])
    raw_file.flush()

# ==========================
# USER
# ==========================
class PipelineUser(HttpUser):
    wait_time = lambda self: 0
    host = "http://dummy"

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



    avg_res = prom_query_instant(q_avg)
    p95_res = prom_query_instant(q_p95)
    rps_res = prom_query_instant(q_rps)

    avg_map = {row["metric"].get("step_id", "unknown"): float(row["value"][1]) for row in avg_res}
    p95_map = {row["metric"].get("step_id", "unknown"): float(row["value"][1]) for row in p95_res}
    rps_map = {row["metric"].get("step_id", "unknown"): float(row["value"][1]) for row in rps_res}

    step_ids = sorted(set(avg_map.keys()) | set(p95_map.keys()) | set(rps_map.keys()))

    with open("prom_summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["test_id", "duration_s", "step_id", "avg_s", "p95_s", "rps"])
        for sid in step_ids:
            w.writerow([
                test_id,
                dur,
                sid,
                f"{avg_map.get(sid, 0):.6f}",
                f"{p95_map.get(sid, 0):.6f}",
                f"{rps_map.get(sid, 0):.6f}",
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
        global CURVE_TYPE, CURVE_USERS, CURVE_DURATION, CURVE_SPAWN_RATE
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
