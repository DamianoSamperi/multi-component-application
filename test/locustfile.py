import subprocess
from locust import HttpUser, task, between

# -------- Ricerca entrypoint --------
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
                entrypoints.append((name, f"http://{ip}:{port}/process"))

    return entrypoints

ENTRYPOINTS = get_pipeline_entrypoints()
print("Entrypoints trovati:", ENTRYPOINTS)


# -------- Classe utente Locust --------
class PipelineUser(HttpUser):
    wait_time = between(1, 3)
    host = "http://pipelines"  # host fittizio richiesto da Locust

    @task
    def send_to_all_pipelines(self):
        if not ENTRYPOINTS:
            print("⚠️ Nessun entrypoint disponibile")
            return

        image_file = "your_image.jpg"

        for name, url in ENTRYPOINTS:
            with open(image_file, "rb") as f:
                files = {"image": (image_file, f, "image/jpeg")}
                # usiamo request_name=name per distinguere le pipeline
                with self.client.post(
                    url,
                    files=files,
                    name=name,  # così Locust raggruppa per pipeline
                    catch_response=True
                ) as response:
                    if response.status_code == 200:
                        output_filename = f"output_{name}.jpg"
                        with open(output_filename, "wb") as out:
                            out.write(response.content)
                        response.success()
                    else:
                        response.failure(f"Errore {response.status_code}: {response.text}")
