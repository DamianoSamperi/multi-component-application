from locust import HttpUser, task, between, events, LoadTestShape
import subprocess
import time
import math
import sys

def get_pipeline_entrypoints():
    # 🔹 1. Recupera l'IP di un nodo del cluster
    node_ip_result = subprocess.run(
        ["kubectl", "get", "nodes", "-o", "jsonpath={.items[0].status.addresses[?(@.type=='InternalIP')].address}"],
        stdout=subprocess.PIPE,
        text=True,
        check=True
    )
    node_ip = node_ip_result.stdout.strip()

    # 🔹 2. Recupera nome servizio, tipo e nodePort
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
                start_time = time.time()
                with self.client.post(
                    "/process",
                    files=files,
                    name=name,
                    catch_response=True
                ) as resp:
                    if resp.status_code == 200:
                        # 🔹 Estrai tempi degli step dagli header
                        step_times = {
                            k: float(v) for k, v in resp.headers.items() if k.startswith("X-Step-")
                        }
                        resp.success()

                        # 🔹 Registra ogni step come metrica personalizzata
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
# 🔹 Scelta curva da CLI
# ===============================
# Controlla se sono passati i parametri CLI custom
USE_CUSTOM_SHAPE = any(arg.startswith("--curve") for arg in sys.argv)

if USE_CUSTOM_SHAPE:
    # Definisci CustomShape solo se --curve viene passato
    import math

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
