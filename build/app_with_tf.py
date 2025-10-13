import os
import yaml
import io
import requests
import time
from flask import Flask, request, jsonify
from PIL import Image
from kubernetes import client, config as k8s_config

# Importa i tuoi step
from steps.upscaler import Upscaler
from steps.classifier_light import Classifier
from steps.grayscale import Grayscale
from steps.deblur import Deblur

app = Flask(__name__)

# --- Lettura config pipeline da env ---
pipeline_yaml = os.getenv("PIPELINE_CONFIG", '{"steps":[]}')
config = yaml.safe_load(pipeline_yaml)

# --- Config ---
NAMESPACE = os.getenv("POD_NAMESPACE", "default")
SERVICE_PORT = os.getenv("SERVICE_PORT", "5000")
APP_LABEL = os.getenv("APP_LABEL", "nn-service")
PIPELINE_ID = config.get("pipeline_id")  # viene letto dalla ConfigMap

# --- Step corrente ---
STEP_ID = int(config.get("step_id", 0))

# Registro i componenti disponibili
available_steps = {
    "upscaling": Upscaler,
    "detection": Classifier,
    "grayscale": Grayscale,
    "deblur": Deblur,
}

# Recupero configurazione di questo step
current_step_conf = None
for step_conf in config["steps"]:
    if int(step_conf.get("id", -1)) == STEP_ID:
        current_step_conf = step_conf
        break

# Inizializza lo step corrente
pipeline = []
if current_step_conf:
    step_type = current_step_conf["type"]
    if step_type in available_steps:
        pipeline.append(available_steps[step_type](**current_step_conf.get("params", {})))


@app.route("/process", methods=["POST"])
def process():
    image_file = request.files["image"]
    image = Image.open(image_file).convert("RGB")
    
    # ⏱️ misura tempo step
    start_time = time.time()
    for step in pipeline:
        image = step.run(image)
    elapsed = time.time() - start_time
    
    # Header custom con il tempo di questo step
    step_header = {f"X-Step-{STEP_ID}-Time": str(elapsed)}

    # 2️⃣ Determina il prossimo step dalla config
    next_steps = current_step_conf.get("next_step", None)
    if not next_steps:
        # Ultimo step → restituisci output al client
        output = io.BytesIO()
        image.save(output, format="JPEG")
        output.seek(0)
        headers = {"Content-Type": "image/jpeg", **step_header}
        return output.read(), 200, headers

    if isinstance(next_steps, (str, int)):
        next_steps = [next_steps]

    # 3️⃣ Carica config Kubernetes e verifica ConfigMap della pipeline
    try:
        k8s_config.load_incluster_config()
        v1 = client.CoreV1Api()
        # Lista tutte le ConfigMap della pipeline
        configmaps = v1.list_namespaced_config_map(
            namespace=NAMESPACE,
            label_selector=f"pipeline_id={PIPELINE_ID}"
        )
    except Exception as e:
        return jsonify({"error": f"Errore API Kubernetes: {e}"}), 500

    active_steps = set()
    for cm in configmaps.items:
        try:
            cm_data = yaml.safe_load(cm.data.get("PIPELINE_CONFIG", "{}"))
            steps = cm_data.get("steps", [])
            for step in steps:
                step_id = step.get("id")
                if step_id is not None:
                    active_steps.add(str(step_id))
        except Exception as e:
            print(f"Errore nel processare ConfigMap {cm.metadata.name}: {e}")

    # 4️⃣ Filtra i prossimi step in base alle ConfigMap attive
    available_next = [s for s in next_steps if str(s) in active_steps]

    if not available_next:
        return jsonify({"error": "Nessun prossimo step attivo per questa pipeline"}), 500

    # --- Logica di selezione ---
    preferred = current_step_conf.get("preferred_next")
    if preferred and str(preferred) in available_next:
        chosen_next = preferred
    else:
        chosen_next = sorted(available_next)[0]

    # 5️⃣ Costruisci URL per il prossimo step (scoped alla pipeline)
    next_url = (
        f"http://{PIPELINE_ID}-step-{chosen_next}.{NAMESPACE}.svc.cluster.local:{SERVICE_PORT}/process"
    )

    # 6️⃣ Invia immagine al prossimo step
    buf = io.BytesIO()
    image.save(buf, format="JPEG")
    buf.seek(0)
    files = {"image": ("frame.jpg", buf, "image/jpeg")}

    try:
        r = requests.post(next_url, files=files)
        # Unisci gli header di risposta con il tempo dello step corrente
        combined_headers = dict(r.headers)
        combined_headers.update(step_header)
        
        #return r.content, r.status_code, r.headers.items()
        return r.content, r.status_code, combined_headers.items()
    except Exception as e:
        return jsonify({"error": f"Errore invio a step {chosen_next}: {e}"}), 500    



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(SERVICE_PORT))
