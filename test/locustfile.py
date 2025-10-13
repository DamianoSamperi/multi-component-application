from locust import HttpUser, task, between, events, LoadTestShape
import subprocess
import time

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
@events.init_command_line_parser.add_listener
def _(parser):
    parser.add_argument("--curve", type=str, default="ramp",
                        help="Tipo di curva: ramp, step, spike, sinus, flat")
    parser.add_argument("--users", type=int, default=20, help="Numero massimo utenti")
    parser.add_argument("--duration", type=int, default=60, help="Durata test in secondi")
    parser.add_argument("--spawn-rate", type=float, default=2, help="Tasso di spawn utenti/sec")


# ===============================
# 🔹 Definizione curve di carico
# ===============================
class CustomShape(LoadTestShape):

    def __init__(self):
        super().__init__()
        self.curve = self.get_env().parsed_options.curve
        self.users = self.get_env().parsed_options.users
        self.duration = self.get_env().parsed_options.duration
        self.spawn_rate = self.get_env().parsed_options.spawn_rate

    def tick(self):
        run_time = self.get_run_time()

        if run_time > self.duration:
            return None

        # --- Diversi tipi di curve ---
        if self.curve == "ramp":
            users = int(self.users * (run_time / self.duration))
        elif self.curve == "step":
            step_time = self.duration / 5
            step_level = int(run_time // step_time)
            users = int((step_level + 1) * (self.users / 5))
        elif self.curve == "spike":
            if run_time < self.duration / 4 or run_time > 3 * self.duration / 4:
                users = int(self.users / 10)
            else:
                users = self.users
        elif self.curve == "sinus":
            users = int((self.users / 2) * (1 + math.sin(run_time / self.duration * 2 * math.pi)))
        elif self.curve == "flat":
            users = self.users
        else:
            users = 1

        return (users, self.spawn_rate)


shape = CustomShape()
