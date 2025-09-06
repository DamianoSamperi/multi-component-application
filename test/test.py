import subprocess
import sys
import requests

# ID pipeline passato come argomento
if len(sys.argv) < 2:
    print("Uso: python3 test.py <pipeline_id>")
    sys.exit(1)

pipeline_id = sys.argv[1]
target_ip = None

# Recupera la lista dei servizi
try:
    svc_list = subprocess.check_output(
        ["kubectl", "get", "svc", "-o", "jsonpath={range .items[*]}{.metadata.name} {.spec.clusterIP} {.status.loadBalancer.ingress[0].ip}\n{end}"],
        text=True
    ).splitlines()

    for svc in svc_list:
        parts = svc.split()
        name = parts[0]
        cluster_ip = parts[1]
        external_ip = parts[2] if len(parts) > 2 else ""

        if f"pipeline-{pipeline_id}-step-" in name:
            target_ip = external_ip if external_ip else cluster_ip
            print(f"Usando IP del servizio {name}: {target_ip}")
            break
    else:
        raise RuntimeError(f"Nessun servizio trovato per pipeline {pipeline_id}")

except Exception as e:
    print("Errore nel recuperare il servizio:", e)
    sys.exit(1)

# Invia immagine
image_file = "your_image.jpg"
with open(image_file, "rb") as f:
    files = {"image": (image_file, f, "image/jpeg")}
    url = f"http://{target_ip}:5000/process"
    print("Chiamata a:", url)

    try:
        response = requests.post(url, files=files)
        print("Status:", response.status_code)
        if response.status_code == 200:
            with open("output.jpg", "wb") as out:
                out.write(response.content)
            print("Immagine salvata come output.jpg")
        else:
            print("Errore:", response.text)
    except Exception as e:
        print("Errore nella richiesta HTTP:", e)
