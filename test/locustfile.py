from locust import HttpUser, task, between, events
import subprocess
import time

def get_pipeline_entrypoints():
    result = subprocess.run(
        ["kubectl", "get", "svc", "-o","jsonpath={range .items[*]}{.metadata.name} {.spec.type} {.spec.ports[0].nodePort}{\"\\n\"}{end}"],
        stdout=subprocess.PIPE,
        text=True,
    )
    node_ip = result.stdout.strip()
    entrypoints = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3:
            nname, svc_type, node_port = parts
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
