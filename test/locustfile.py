import subprocess
from locust import HttpUser, task, between

def discover_entrypoints():
    """Trova tutti i servizi pipeline-*-step-0 e ritorna i loro endpoint HTTP"""
    entrypoints = []

    try:
        # Estrai nome svc, clusterIP, externalIP e porta
        svc_list = subprocess.check_output(
            [
                "kubectl", "get", "svc",
                "-o", "jsonpath={range .items[*]}{.metadata.name} {.spec.clusterIP} {.status.loadBalancer.ingress[0].ip} {.spec.ports[0].port}\n{end}"
            ],
            text=True
        ).splitlines()

        for svc in svc_list:
            parts = svc.split()
            if len(parts) < 4:
                continue
            name, cluster_ip, external_ip, port = parts[0], parts[1], parts[2], parts[3]

            if name.endswith("step-0"):  # solo entrypoint pipeline
                target_ip = external_ip if external_ip and external_ip != "<none>" else cluster_ip
                url = f"http://{target_ip}:{port}/process"
                entrypoints.append((name, url))

    except Exception as e:
        print("Errore nel recupero dei servizi:", e)

    return entrypoints


ENTRYPOINTS = discover_entrypoints()
print("Entrypoints trovati:", ENTRYPOINTS)


class PipelineUser(HttpUser):
    wait_time = between(1, 3)

    @task
    def send_to_all_pipelines(self):
        if not ENTRYPOINTS:
            print("⚠️ Nessun entrypoint disponibile")
            return

        image_file = "your_image.jpg"

        for name, url in ENTRYPOINTS:
            with open(image_file, "rb") as f:
                files = {"image": (image_file, f, "image/jpeg")}
                response = self.client.post(url, files=files, name=name)  # `name` serve a separare le stats in Locust

                if response.status_code == 200:
                    print(f"[OK] {name} ({url})")
                else:
                    print(f"[ERR {response.status_code}] {name} ({url}): {response.text}")
