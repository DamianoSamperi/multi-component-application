import tensorflow as tf
import tensorflow_hub as hub
import numpy as np
import cv2
from PIL import Image
import threading

# --- Semaforo Globale ---
# Permette solo a 1 thread alla volta di eseguire l'inferenza sulla GPU
gpu_semaphore = threading.Semaphore(1)

tf.compat.v1.enable_eager_execution()

# --- Configura memory growth GPU ---
gpus = tf.config.experimental.list_physical_devices('GPU')
for gpu in gpus:
    tf.config.experimental.set_memory_growth(gpu, True)
    
_global_infer_fn = None
_model_ready = False
PROFILE_RESOLUTION = {
    "light": 320,
    "heavy": 640,
}

def load_model():
    global _global_infer_fn, _model_ready
    model_url="https://tfhub.dev/tensorflow/ssd_mobilenet_v2/fpnlite_320x320/1"
    print("[INFO] Loading TF model async...")
    try:
        model = hub.load(model_url)
        @tf.function
        def infer_fn(input_tensor):
            return model(input_tensor)

        _global_infer_fn = infer_fn
        _model_ready = True
        print("[INFO] Model loaded successfully.", flush=True)
    except Exception as e:
        print(f"[ERROR] Model load failed: {e}", flush=True)
        
threading.Thread(target=load_model, daemon=True).start()

class Classifier:
    def __init__(self, model_name="pednet", threshold=0.5, **kwargs):
        self.threshold = threshold

    @property
    def ready(self):
        return _model_ready 

    def run(self, image: Image.Image, load_profile="light"):
        global _global_infer_fn, _model_ready

        if not _model_ready or _global_infer_fn is None:
            raise RuntimeError("Model not ready yet")

        # 1. Pre-processing (Eseguito in parallelo, usa solo CPU/RAM)
        np_img = np.array(image, dtype=np.uint8)
        h_orig, w_orig = np_img.shape[:2]
        size = PROFILE_RESOLUTION.get(load_profile, 320)
        # target_w, target_h = 320, 320
        np_resized = cv2.resize(np_img, (size, size))
        input_tensor = tf.expand_dims(np_resized, 0)

        # 2. Sezione Critica: Accesso alla GPU
        # Il semaforo mette in coda le richieste extra senza farle crashare
        
        with gpu_semaphore:
            outputs = _global_infer_fn(input_tensor)
            
            # Estraiamo i risultati in numpy subito per liberare la memoria TF
            boxes = outputs["detection_boxes"][0].numpy()
            scores = outputs["detection_scores"][0].numpy()
            classes = outputs["detection_classes"][0].numpy().astype(np.int32)

        # 3. Post-processing (Disegno dei box)
        np_draw = cv2.cvtColor(np_img, cv2.COLOR_RGB2BGR)
        for box, score, label in zip(boxes, scores, classes):
            if score < self.threshold:
                continue
            y1, x1, y2, x2 = box
            x1 = int(x1 * w_orig)
            x2 = int(x2 * w_orig)
            y1 = int(y1 * h_orig)
            y2 = int(y2 * h_orig)

            cv2.rectangle(np_draw, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.putText(
                np_draw,
                f"{label}:{score:.2f}",
                (x1, max(y1 - 10, 0)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5, (0, 255, 0), 2,
            )

        np_rgb = cv2.cvtColor(np_draw, cv2.COLOR_BGR2RGB)
        return Image.fromarray(np_rgb)
