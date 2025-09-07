from locust import HttpUser, task, between
import subprocess

def get_pipeline_entrypoints():
    result = subprocess.run(
        ["kubectl", "get", "svc", "-o", "jsonpath={range .items[*]}{.metadata.name} {.status.loadBalancer.ingress[0].ip} {.spec.ports[0].port}{\"\\n\"}{end}"],
        stdout=subprocess.PIPE,
        text=True,
    )
    entrypoints = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3:
            name, ip, port = parts
            if name.endswith("step-0") and ip != "<none>":
                entrypoints.append((name, f"http://{ip}:{port}"))
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
                with self.client.post(
                    "/process",
                    files=files,
                    name=name,
                    catch_response=True
                ) as resp:
                    if resp.status_code == 200:
                        # 🔹 Estrai tempi degli step dagli header
                        step_times = {
                            k: v for k, v in resp.headers.items() if k.startswith("X-Step-")
                        }
                        print(f"[{name}] Tempi per step:", step_times)
                        resp.success()
                    else:
                        resp.failure(f"Errore {resp.status_code}")
