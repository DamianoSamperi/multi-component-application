import os
import yaml
from flask import Flask, request, jsonify
from PIL import Image
import io
from kubernetes import client, config as k8s_config
import requests

from steps.upscaler import Upscaler
from steps.classifier import Classifier

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
    "detection": Classifier
}

pipeline = []
for step_conf in config["steps"]:
    step_type = step_conf["type"]
    if step_type in available_steps:
        pipeline.append(available_steps[step_type](**step_conf.get("params", {})))

@app.route("/process", methods=["POST"])
def process():
    image_file = request.files["image"]
    image = Image.open(image_file).convert("RGB")

    # 1️⃣ Applica la pipeline di questo pod
    for step in pipeline:
        image = step.run(image)

    # 2️⃣ Carica config Kubernetes per sapere i pod disponibili
    try:
        k8s_config.load_incluster_config()
        v1 = client.CoreV1Api()
        label_selector = f"app={APP_LABEL}"
        pods = v1.list_namespaced_pod(namespace=NAMESPACE, label_selector=label_selector)
    except Exception as e:
        return jsonify({"error": f"Errore API Kubernetes: {e}"}), 500

    # 3️⃣ Trova tutti gli step registrati
    steps = []
    for pod in pods.items:
        step_label = pod.metadata.labels.get("step")
        if step_label is not None:
            try:
                steps.append(int(step_label))
            except ValueError:
                pass

    # 4️⃣ Determina se c'è un prossimo step
    if steps:
        max_step = max(steps)
        if STEP_ID < max_step:
            next_step = STEP_ID + 1
            next_url = f"http://nn-step-{next_step}.{NAMESPACE}.svc.cluster.local:{SERVICE_PORT}/process"

            buf = io.BytesIO()
            image.save(buf, format="JPEG")
            buf.seek(0)
            files = {'image': ('frame.jpg', buf, 'image/jpeg')}

            try:
                r = requests.post(next_url, files=files)
                return r.content, r.status_code, r.headers.items()
            except Exception as e:
                return jsonify({"error": f"Errore invio a step {next_step}: {e}"}), 500

    # 5️⃣ Nessun prossimo step → restituisci all’utente
    output = io.BytesIO()
    image.save(output, format="JPEG")
    output.seek(0)
    return output.read(), 200, {"Content-Type": "image/jpeg"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(SERVICE_PORT))
