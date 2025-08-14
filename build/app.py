import os
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models as models
from PIL import Image
import io
import torchvision.transforms as transforms
from flask import Flask, request, jsonify
import numpy as np
import requests

from kubernetes import client, config

app = Flask(__name__)

# Config via env
STEP_ID = int(os.getenv("STEP_ID", "0"))
LAYER_START = int(os.getenv("LAYER_START", "0"))
LAYER_COUNT = int(os.getenv("LAYER_COUNT", "5"))
SERVICE_PORT = os.getenv("SERVICE_PORT", "5000")
NAMESPACE = os.getenv("POD_NAMESPACE", "default")
APP_LABEL = os.getenv("APP_LABEL", "nn-service")

# Load full model
full_model = torch.load("resnet50.pt")
full_model.eval()

# Extract list of layers from the full model
layers = [
    full_model.conv1,
    full_model.bn1,
    full_model.relu,
    full_model.maxpool,
    full_model.layer1,
    full_model.layer2,
    full_model.layer3,
    full_model.layer4,
    full_model.avgpool,
    nn.Flatten(),
    full_model.fc
]

# Get the submodel layers
sub_layers = layers[LAYER_START : LAYER_START + LAYER_COUNT]
sub_model = nn.Sequential(*sub_layers)
sub_model.eval()

@app.route("/process", methods=["POST"])
def process():
    if STEP_ID == 1:
        # STEP 1: riceve immagine, elabora, passa il tensore al prossimo
        if 'image' not in request.files:
            return jsonify({"error": "Missing image file"}), 400

        image_file = request.files['image']
        image = Image.open(image_file).convert("RGB")

        transform = transforms.Compose([
            transforms.Resize((224, 224)),
            transforms.ToTensor()
        ])
        x_tensor = transform(image).unsqueeze(0)
    else:
        # STEP ≥2: riceve tensore JSON e lo converte in tensor
        data = request.get_json()
        if not data or 'x' not in data:
            return jsonify({"error": "Missing input tensor"}), 400
        x_tensor = torch.tensor(data['x'], dtype=torch.float32)

    with torch.no_grad():
        y = sub_model(x_tensor).numpy().tolist()

    # Se ci sono altri step, invia a loro
    config.load_incluster_config()
    v1 = client.CoreV1Api()
    label_selector = f"app={APP_LABEL}"
    pods = v1.list_namespaced_pod(namespace=NAMESPACE, label_selector=label_selector)

    steps = []
    for pod in pods.items:
        step = pod.metadata.labels.get("step")
        if step is not None:
            steps.append(int(step))

    if steps:
        max_step = max(steps)
        if STEP_ID < max_step:
            next_step = STEP_ID + 1
            next_url = f"http://nn-step-{next_step}.{NAMESPACE}.svc.cluster.local:{SERVICE_PORT}/process"
            try:
                response = requests.post(next_url, json={"x": y})
                return response.text
            except Exception as e:
                return jsonify({"error": str(e)}), 500

    return jsonify({"output": y, "final_step": STEP_ID})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(SERVICE_PORT))

