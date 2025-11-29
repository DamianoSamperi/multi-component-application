import tensorflow as tf
import tensorflow_hub as hub
import numpy as np
import cv2
from PIL import Image
import threading
tf.compat.v1.enable_eager_execution()
# --- Configura memory growth GPU ---
gpus = tf.config.experimental.list_physical_devices('GPU')
for gpu in gpus:
    tf.config.experimental.set_memory_growth(gpu, True)
    
# 🔹 variabile globale condivisa
_global_infer_fn = None
_model_ready = False

def load_model():
    """Load model async and store a callable inference function."""
    global _global_infer_fn, _model_ready
    model_url="https://tfhub.dev/tensorflow/ssd_mobilenet_v1/fpn_640x640/1"
    print("[INFO] Loading TF model async...")
    try:
        resolved_path = hub.resolve(model_url)
        print(f"URL valido, modello scaricabile in: {resolved_path}", flush=True)
    except Exception as e:
        print(f"URL non valido: {e}", flush=True)
    try:
        model = hub.load(model_url)
        # Wrap inference in a tf.function to safely call from any thread
        # @tf.function
        # def infer_fn(input_tensor):
        #     return model(input_tensor)
        infer = model.signatures["default"]

        _global_infer_fn = infer_fn
        _model_ready = True
        print("[INFO] Model loaded successfully.", flush=True)
    except Exception as e:
        print(f"[ERROR] Model load failed: {e}", flush=True)
        
# Start background ing so Flask doesn't block startup
threading.Thread(target=load_model, daemon=True).start()

class Classifier:
    def __init__(self, model_name="pednet",threshold=0.5, **kwargs):
        self.threshold = threshold
    @property
    def ready(self):
        return _model_ready 
    def run(self, image: Image.Image):
        global _global_infer_fn, _model_ready

        if not _model_ready or _global_infer_fn is None:
            raise RuntimeError("Model not ready yet")

        np_img = np.array(image, dtype=np.uint8)
        np_img = cv2.cvtColor(np_img, cv2.COLOR_RGB2BGR)
        input_tensor = tf.expand_dims(np_img, 0)  # (1,H,W,3)

        # Use the pre-built tf.function for thread-safe inference
        outputs = _global_infer_fn(input_tensor)
        print(outputs.keys())

        boxes = outputs["detection_boxes"][0].numpy()
        scores = outputs["detection_scores"][0].numpy()
        classes = outputs["detection_classes"][0].numpy().astype(np.int32)

        h, w, _ = np_img.shape
        for box, score, label in zip(boxes, scores, classes):
            if score >= self.threshold:
                y1, x1, y2, x2 = box
                x1, y1, x2, y2 = int(x1*w), int(y1*h), int(x2*w), int(y2*h)
                cv2.rectangle(np_img, (x1, y1), (x2, y2), (0,255,0), 2)
                cv2.putText(np_img, f"{label}:{score:.2f}", (x1, y1-10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,255,0), 2)

        np_img = cv2.cvtColor(np_img, cv2.COLOR_BGR2RGB)
        return Image.fromarray(np_img)
