import tensorflow as tf
import tensorflow_hub as hub
import numpy as np
import cv2
from PIL import Image
import threading
tf.config.run_functions_eagerly(True)

# --- Configura memory growth GPU ---
gpus = tf.config.experimental.list_physical_devices('GPU')
for gpu in gpus:
    tf.config.experimental.set_memory_growth(gpu, True)
    
# 🔹 variabile globale condivisa
_global_net = None
_model_ready = False

def load_model(model_url="https://tfhub.dev/tensorflow/ssd_mobilenet_v2/fpnlite_320x320/1"):
    """Carica il modello in background."""
    global _global_net, _model_ready
    try:
        print(f"[INFO] Avvio caricamento modello da {model_url}",flush=True)
        _global_net = hub.load(model_url)
        _model_ready = True
        print("[INFO] Modello pronto", flush=True)
    except Exception as e:
        print(f"[ERROR] Errore caricamento modello: {e}")
        _global_net = None
        _model_ready = False

# Avvio thread al modulo import
threading.Thread(target=load_model, daemon=True).start()

class Classifier:
    def __init__(self, model_name="pednet", threshold=0.5, **kwargs):
    #     global _global_net
    #     if _global_net is None:
    #         try:
    #             print(f"[INFO] Caricamento modello Jetson: {model_name}")
    #             _global_net = hub.load("https://tfhub.dev/tensorflow/ssd_mobilenet_v2/fpnlite_320x320/1")
    #         except Exception as e:
    #             print(f"[ERROR] Errore nell'inizializzazione del modello: {e}")
    #             _global_net = None
    #     else:
    #         print(f"[INFO] Riutilizzo modello Jetson già caricato: {model_name}")
    #     self.net = _global_net
        self.threshold = threshold

    def run(self, image: Image.Image):
        global _global_net, _model_ready

        if not _model_ready or _global_net is None:
            raise RuntimeError("Modello non ancora pronto")
        np_img = np.array(image, dtype=np.uint8)
        input_tensor = tf.expand_dims(np_img, axis=0)  
        input_tensor = tf.convert_to_tensor(input_tensor, dtype=tf.uint8)
        
        outputs = _global_net(input_tensor)

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
    
        return Image.fromarray(np_img)
