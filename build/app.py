import os
import yaml
from flask import Flask, request, jsonify
from PIL import Image
import io
from kubernetes import client, config as  k8s_config
import requests

from steps.upscaler import Upscaler
from steps.classifier import Classifier
from steps.grayscale import Grayscale
from steps.deblur import Deblur

app = Flask(__name__)
NAMESPACE = os.getenv("POD_NAMESPACE", "default")
SERVICE_PORT = os.getenv("SERVICE_PORT", "5000")
APP_LABEL = os.getenv("APP_LABEL", "nn-service")


# Lettura config pipeline da env (in YAML)
pipeline_yaml = os.getenv("PIPELINE_CONFIG", '{"steps":[]}')
config = yaml.safe_load(pipeline_yaml)

# STEP_ID e TOTAL_STEPS letti dalla pipeline
STEP_ID = int(config.get("step_id", 0))
TOTAL_STEPS = int(config.get("total_steps", 1))

# Registro i componenti
available_steps = {
    "upscaling": Upscaler,
    "detection": Classifier,
    "grayscale": Grayscale,
    "deblur": Deblur
}

# Recupero config di questo step
current_step_conf = None
for step_conf in config["steps"]:
    if int(step_conf.get("id", -1)) == STEP_ID:
        current_step_conf = step_conf
        break

#pipeline = []
#for step_conf in config["steps"]:
#    step_type = step_conf["type"]
#    if step_type in available_steps:
#        pipeline.append(available_steps[step_type](**step_conf.get("params", {})))
pipeline = []
if current_step_conf:
    step_type = current_step_conf["type"]
    if step_type in available_steps:
        pipeline.append(available_steps[step_type](**current_step_conf.get("params", {})))

@app.route("/process", methods=["POST"])
def process():
    image_file = request.files["image"]
    image = Image.open(image_file).convert("RGB")

    # 1️⃣ Applica la pipeline di questo step
    for step in pipeline:
        image = step.run(image)

    # 2️⃣ Determina prossimo step dalla config
    next_steps = current_step_conf.get("next_step", None)
    if not next_steps:
        # Ultimo step → restituisci al client
        output = io.BytesIO()
        image.save(output, format="JPEG")
        output.seek(0)
        return output.read(), 200, {"Content-Type": "image/jpeg"}

    if isinstance(next_steps, str):
        next_steps = [next_steps]

    # 3️⃣ Carica config Kubernetes e verifica pod attivi
    try:
        k8s_config.load_incluster_config()
        v1 = client.CoreV1Api()
        label_selector = f"app={APP_LABEL}"
        pods = v1.list_namespaced_pod(namespace=NAMESPACE, label_selector=label_selector)
    except Exception as e:
        return jsonify({"error": f"Errore API Kubernetes: {e}"}), 500

    active_steps = set()
    for pod in pods.items:
        step_label = pod.metadata.labels.get("step")
        if step_label:
            active_steps.add(str(step_label))

    # 4️⃣ Filtra next_steps in base ai pod attivi
    available_next = [s for s in next_steps if str(s) in active_steps]

    if not available_next:
        return jsonify({"error": "Nessun prossimo step attivo"}), 500

    # --- Logica di selezione ---
    preferred = current_step_conf.get("preferred_next")

    if preferred and preferred in available_next:
        chosen_next = preferred
    else:
        # TODO: qui posso inserire una qualunque logica per selezionare le biforcazione
        # scelgo inzialmente il primo disponibile
        chosen_next = sorted(available_next)[0]

    next_url = f"http://nn-step-{chosen_next}.{NAMESPACE}.svc.cluster.local:{SERVICE_PORT}/process"

    # 6️⃣ Invia immagine al prossimo step
    buf = io.BytesIO()
    image.save(buf, format="JPEG")
    buf.seek(0)
    files = {'image': ('frame.jpg', buf, 'image/jpeg')}

    try:
        r = requests.post(next_url, files=files)
        return r.content, r.status_code, r.headers.items()
    except Exception as e:
        return jsonify({"error": f"Errore invio a step {chosen_next}: {e}"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(SERVICE_PORT))
