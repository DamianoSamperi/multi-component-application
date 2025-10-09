import subprocess
import sys
import requests
import json

# ID pipeline passato come argomento
if len(sys.argv) < 2:
    print("Uso: python3 test.py <pipeline_id>")
    sys.exit(1)

pipeline_id = sys.argv[1]
target_ip = None
node_port = None

# Recupera la lista dei servizi
try:
    svc_json = subprocess.check_output(
        ["kubectl", "get", "svc", "-o", "json"],
        text=True
    )
    svc_data = json.loads(svc_json)

    for item in svc_data["items"]:
        name = item["metadata"]["name"]
        if f"pipeline-{pipeline_id}-step-" in name:
            ports = item["spec"].get("ports", [])
            if not ports:
                continue
            node_port = ports[0].get("nodePort")
            print(f"Trovato servizio {name} con NodePort {node_port}")
            break
    else:
        raise RuntimeError(f"Nessun servizio trovato per pipeline {pipeline_id}")

except Exception as e:
    print("Errore nel recuperare il servizio:", e)
    sys.exit(1)

# Recupera IP di un nodo (es. il primo nodo Ready)
try:
    node_ip = subprocess.check_output(
        ["kubectl", "get", "nodes", "-o", "jsonpath={.items[0].status.addresses[?(@.type=='InternalIP')].address}"],
        text=True
    ).strip()
    print(f"Usando IP nodo: {node_ip}")
except Exception as e:
    print("Errore nel recuperare l'IP del nodo:", e)
    sys.exit(1)

# Invia immagine
image_file = "your_image.jpg"
with open(image_file, "rb") as f:
    files = {"image": (image_file, f, "image/jpeg")}
    #url = f"http://{target_ip}:5000/process"
    url = f"http://{node_ip}:{node_port}/process"
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
