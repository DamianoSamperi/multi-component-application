import os
import yaml
import io
import requests
import threading
import time
import socket
from flask import Flask, request, jsonify, g
from PIL import Image
from kubernetes import client, config as k8s_config
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST, Histogram
import traceback
import signal
import sys

MAX_SHUTDOWN_WAIT = 4500  # secondi (scelgo in base al worst-case)
USE_LIGHT = os.getenv("USE_LIGHT", "false").lower() == "true"
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"


app = Flask(__name__)
DEFAULT_LOAD_PROFILE = os.getenv("DEFAULT_LOAD_PROFILE", "light")
accepting_requests = True
shutdown_event = threading.Event()



@app.route("/readyz")
def readyz():
    # se lo step corrente ha l'attributo `ready`, controllalo
    step_ready = True
    if pipeline and hasattr(pipeline[0], "ready"):
        step_ready = pipeline[0].ready

    if accepting_requests and step_ready:
        return "ok", 200
    elif not step_ready:
        return "loading", 503
    else:
        return "draining", 503
# @app.route("/drain", methods=["POST"])
# def drain():
#     global accepting_requests
#     accepting_requests = False
#     print("[INFO] Ricevuto comando di draining")
#     return jsonify({"status": "draining"}), 200

http_requests_total = Counter(
    'http_requests_total',
    'Numero totale di richieste HTTP ricevute per step',
    ['method', 'endpoint', 'pipeline_id', 'step_id', 'pod_name']
)

http_request_in_progress = Gauge(
    'http_requests_in_progress',
    'Numero di richieste attualmente in elaborazione per step',
    ['pipeline_id', 'step_id', 'pod_name']
)
POD_NAME = os.getenv("POD_NAME", socket.gethostname())

step_latency = Histogram(
    "step_processing_time_seconds",
    "Tempo di elaborazione per step",
    ["pipeline_id", "step_id", "pod_name", "test_id"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1200)
)

def handle_sigterm(signum, frame):
    global accepting_requests
    print("[SIGTERM] Received, starting graceful shutdown")

    accepting_requests = False
    shutdown_event.set()

    start = time.time()

    while True:
        try:
            inflight = http_request_in_progress.labels(
                PIPELINE_ID, STEP_ID, POD_NAME
            )._value.get()
        except Exception:
            inflight = 0  # se non inizializzato, usciamo

        if inflight <= 0:
            print("[SIGTERM] All requests completed, exiting")
            break

        if time.time() - start > MAX_SHUTDOWN_WAIT:
            print("[SIGTERM] Timeout reached, forcing exit")
            break

        print(f"[SIGTERM] Waiting inflight={inflight}")
        time.sleep(1)

    sys.exit(0)


signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)

@app.route("/drain", methods=["POST"])
def drain():
    global accepting_requests
    accepting_requests = False

    inflight = http_request_in_progress.labels(
        PIPELINE_ID, STEP_ID, POD_NAME
    )._value.get()

    print(f"[DRAIN] draining enabled, inflight={inflight}")
    return jsonify({"status": "draining", "inflight": inflight}), 200
    
@app.before_request
def before_request():
    g.start_time = time.time()
    g.test_id = request.headers.get("X-Test-ID", "unknown")

    # 🔹 NUOVO: profilo di carico
    g.load_profile = request.headers.get(
        "X-Load-Profile",
        DEFAULT_LOAD_PROFILE
    )
    # conta SOLO /process
    g.count_inflight = (request.path == "/process")
    # se stai drainando, rifiuta nuove /process subito
    if g.count_inflight and not accepting_requests:
        return jsonify({"error": "draining"}), 503

    if g.count_inflight:
        http_request_in_progress.labels(PIPELINE_ID, STEP_ID, POD_NAME).inc()
        http_requests_total.labels(
            request.method, request.path, PIPELINE_ID, STEP_ID, POD_NAME
        ).inc()

    # http_request_in_progress.labels(PIPELINE_ID, STEP_ID, POD_NAME).inc()
    # http_requests_total.labels(
    #     request.method,
    #     request.path,
    #     PIPELINE_ID,
    #     STEP_ID,
    #     POD_NAME
    # ).inc()
@app.after_request
def after_request(response):
    elapsed = time.time() - g.start_time
    # http_request_in_progress.labels(PIPELINE_ID, STEP_ID, POD_NAME).dec()
    if getattr(g, "count_inflight", False):
        http_request_in_progress.labels(PIPELINE_ID, STEP_ID, POD_NAME).dec()
    response.headers["X-Elapsed-Time"] = str(elapsed)
    return response

@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {'Content-Type': CONTENT_TYPE_LATEST}

# Cache globale protetta da un Lock per evitare problemi di concorrenza
active_steps_cache = set()
cache_lock = threading.Lock()

def update_kubernetes_config():
    """Aggiorna la cache degli step attivi ogni 10 secondi."""
    global active_steps_cache
    print("[INFO] Thread di aggiornamento configurazione K8s avviato.")
    
    # Carica la config una volta sola per il thread
    try:
        k8s_config.load_incluster_config()
        v1 = client.CoreV1Api()
    except Exception as e:
        print(f"[ERROR] Impossibile caricare config K8s: {e}")
        return

    while True:
        try:
            # Recupera le ConfigMap
            configmaps = v1.list_namespaced_config_map(
                namespace=NAMESPACE,
                label_selector=f"pipeline_id={PIPELINE_ID}"
            )
            
            new_active_steps = set()
            for cm in configmaps.items:
                try:
                    cm_data = yaml.safe_load(cm.data.get("PIPELINE_CONFIG", "{}"))
                    steps = cm_data.get("steps", [])
                    for step in steps:
                        s_id = step.get("id")
                        if s_id is not None:
                            new_active_steps.add(str(s_id))
                except Exception as e:
                    print(f"[ERROR] Parsing ConfigMap {cm.metadata.name}: {e}")

            # Aggiorna la cache in modo sicuro
            with cache_lock:
                active_steps_cache = new_active_steps
            
            # print(f"[DEBUG] Cache aggiornata: {active_steps_cache}")
            
        except Exception as e:
            print(f"[ERROR] Durante l'aggiornamento cache K8s: {e}")
        
        time.sleep(10) # Controlla i cambiamenti ogni 10 secondi

# --- Lettura config pipeline da env ---
pipeline_yaml = os.getenv("PIPELINE_CONFIG", '{"steps":[]}')
config = yaml.safe_load(pipeline_yaml)

# --- Config ---
NAMESPACE = os.getenv("POD_NAMESPACE", "default")
#SERVICE_PORT = os.getenv("SERVICE_PORT", "5000")
SERVICE_PORT = os.getenv("SERVICE_PORT") or "5000"
APP_LABEL = os.getenv("APP_LABEL", "nn-service")
PIPELINE_ID = config.get("pipeline_id")  # viene letto dalla ConfigMap

# --- Step corrente ---
STEP_ID = int(config.get("step_id", 0))

# Registro i componenti disponibili
#available_steps = {
#    "upscaling": Upscaler,
#    "detection": Classifier,
#    "grayscale": Grayscale,
#    "deblur": Deblur,
#}

# --- Registro dinamico dei componenti ---
available_steps = {}

# Scansiona la configurazione e importa solo gli step usati in questa pipeline
for step_conf in config.get("steps", []):
    step_type = step_conf.get("type")

    # Evita duplicati
    if step_type in available_steps:
        continue

    if step_type == "upscaling":
        from steps.upscaler import Upscaler
        available_steps["upscaling"] = Upscaler

    elif step_type == "grayscale":
        from steps.grayscale import Grayscale
        available_steps["grayscale"] = Grayscale

    elif step_type == "deblur":
        from steps.deblur import Deblur
        available_steps["deblur"] = Deblur

    elif step_type == "detection":
        USE_LIGHT = os.getenv("USE_LIGHT", "false").lower() == "true"
        if USE_LIGHT:
            from steps.classifier_light import Classifier
        else:
            from steps.classifier import Classifier
        available_steps["detection"] = Classifier

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
        

def send_to_next_step_async(url, files, headers):
    try:
        requests.post(url, files=files, headers=headers, timeout=300)
    except Exception as e:
        print(f"[WARN] Async send failed: {e}")
        
ram_semaphore = threading.Semaphore(1) 

@app.route("/process", methods=["POST"])
def process():
    # Usiamo il semaforo per assicurarci che solo N richieste alla volta carichino immagini
    with ram_semaphore:
        try:
            image_file = request.files["image"]
            image = Image.open(image_file).convert("RGB")
            
            # Esecuzione della pipeline (con il tempo misurato per Prometheus)
            with step_latency.labels(PIPELINE_ID, STEP_ID, POD_NAME, g.test_id).time():
                for step in pipeline:
                    image = step.run(image, load_profile=g.load_profile)


            # Determina il prossimo step dalla config locale
            next_steps = current_step_conf.get("next_step", None)
            
            # Se è l'ultimo step della catena
            if not next_steps:
                output = io.BytesIO()
                image.save(output, format="JPEG")
                output.seek(0)
                return output.read(), 200, {"Content-Type": "image/jpeg"}

            if isinstance(next_steps, (str, int)):
                next_steps = [next_steps]

            # Leggi dalla CACHE aggiornata dal thread di background
            with cache_lock:
                current_active = list(active_steps_cache)

            # Filtra e seleziona il prossimo step
            available_next = [s for s in next_steps if str(s) in current_active]
            
            if not available_next:
                return jsonify({"error": "Nessun prossimo step attivo"}), 500

            # Logica di selezione (Preferito o il primo disponibile)
            preferred = current_step_conf.get("preferred_next")
            if preferred and str(preferred) in available_next:
                chosen_next = preferred
            else:
                chosen_next = sorted(available_next)[0]

            # Costruisci URL
            next_url = f"http://{PIPELINE_ID}-step-{chosen_next}.{NAMESPACE}.svc.cluster.local:{SERVICE_PORT}/process"

            # Invia immagine in modo asincrono per non tenere bloccato Locust
            buf = io.BytesIO()
            image.save(buf, format="JPEG")
            buf.seek(0)
            files = {"image": ("frame.jpg", buf, "image/jpeg")}
            
            fwd_headers = {
                "X-Test-ID": g.test_id,
                "X-Load-Profile": g.load_profile,  # 🔹 PROPAGAZIONE
            }
            threading.Thread(
                target=send_to_next_step_async,
                args=(next_url, files, fwd_headers),
                daemon=True
            ).start()

            headers = {
                "X-Step-ID": str(STEP_ID),
                "X-Pod-Name": POD_NAME,
                "X-In-Flight": str(http_request_in_progress.labels(PIPELINE_ID, STEP_ID, POD_NAME)._value.get())
            }
            return jsonify({"status": "forwarded", "next": chosen_next}), 202, headers

        except Exception as e:
            print(f"[ERROR] /process: {e}")
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    
    # Avvio thread per configurazione K8s
    threading.Thread(target=update_kubernetes_config, daemon=True).start()
    
    # threaded=True serve per far rispondere il pod alle metriche 
    # mentre sta elaborando un'immagine (altrimenti Prometheus va in timeout)
    app.run(host="0.0.0.0", port=int(SERVICE_PORT), threaded=True)
