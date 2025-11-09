from locust import HttpUser, task, between, events, LoadTestShape
import subprocess
import time
import math
import sys
import csv
import requests

# ===============================
# 🔹 CONFIGURAZIONE
# ===============================
PROM_URL = "http://192.168.1.251:30090"
CACHE_TTL = 5  # secondi di cache per le metriche Prometheus

# ===============================
# 🔹 LOG CSV
# ===============================
log_file = open("raw_timings.csv", "w", newline="")
writer = csv.writer(log_file)
writer.writerow([
    "timestamp", "request_type", "name", "response_time_ms", "success",
    "gpu_usage_percentage", "http_requests_in_progress"
])
log_file.flush()

# ===============================
# 🔹 QUERY PROMETHEUS CON CACHE
# ===============================
_last_metrics = {"time": 0, "gpu": 0, "http": 0}


def query_prometheus(metric_name):
    """Esegue una query istantanea su Prometheus e ritorna la media dei valori trovati."""
    try:
        response = requests.get(
            f"{PROM_URL}/api/v1/query",
            params={"query": metric_name},
            timeout=2
        )
        response.raise_for_status()
        data = response.json()
        results = data.get("data", {}).get("result", [])
        if not results:
            return None
        values = [float(r["value"][1]) for r in results if len(r["value"]) > 1]
        return sum(values) / len(values) if values else None
    except Exception as e:
        print(f"⚠️ Errore query Prometheus {metric_name}: {e}")
        return None


def get_metrics_cached():
    """Restituisce le metriche da cache o le aggiorna se scadute."""
    now = time.time()
    if now - _last_metrics["time"] > CACHE_TTL:
        _last_metrics["gpu"] = query_prometheus("gpu_usage_percentage") or 0
        _last_metrics["http"] = query_prometheus("http_requests_in_progress") or 0
        _last_metrics["time"] = now
    return _last_metrics["gpu"], _last_metrics["http"]


# ===============================
# 🔹 LOGGING RICHIESTE
# ===============================
@events.request.add_listener
def log_request(request_type, name, response_time, response_length, exception, **kwargs):
    gpu_usage, http_in_prog = get_metrics_cached()

    writer.writerow([
        time.strftime("%Y-%m-%d %H:%M:%S"),
        request_type,
        name,
        f"{response_time:.2f}",
        "OK" if exception is None else "FAIL",
        f"{gpu_usage:.2f}",
        f"{http_in_prog:.2f}"
    ])
    log_file.flush()


# ===============================
# 🔹 SCOPERTA ENTRYPOINTS PIPELINE
# ===============================
def get_pipeline_entrypoints():
    """Trova i NodePort dei servizi step-0 nel cluster e costruisce gli URL di ingresso."""
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


# ===============================
# 🔹 DEFINIZIONE UTENTE LOCUST
# ===============================
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
                        # Estrai tempi degli step dagli header X-Step-*
                        step_times = {
                            k: float(v) for k, v in resp.headers.items() if k.startswith("X-Step-")
                        }
                        resp.success()

                        # Registra ogni step come metrica separata
                        for step, elapsed in step_times.items():
                            events.request.fire(
                                request_type="STEP",
                                name=f"{step}",
                                response_time=elapsed * 1000,  # ms
                                response_length=0,
                                exception=None,
                                context={},
                            )
                    else:
                        resp.failure(f"Errore {resp.status_code}")


# ===============================
# 🔹 CURVE CUSTOM (CLI)
# ===============================
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
            users = CURVE_USERS
            duration = CURVE_DURATION
            spawn_rate = CURVE_SPAWN_RATE
            curve = CURVE_TYPE

            if run_time > duration:
                return None

            if curve == "ramp":
                current_users = int(users * run_time / duration)
            elif curve == "step":
                step_time = duration / 5
                step_level = int(run_time // step_time)
                current_users = int((step_level + 1) * users / 5)
            elif curve == "spike":
                if run_time < duration / 4 or run_time > 3 * duration / 4:
                    current_users = int(users / 10)
                else:
                    current_users = users
            elif curve == "sinus":
                current_users = int((users / 2) * (1 + math.sin(run_time / duration * 2 * math.pi)))
            else:
                current_users = users

            return (current_users, spawn_rate)
