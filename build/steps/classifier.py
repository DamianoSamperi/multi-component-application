from jetson_inference import detectNet
from jetson_utils import cudaFromNumpy
import numpy as np
import cv2
from PIL import Image

# 🔹 variabile globale condivisa
_global_net = None

class Classifier:
    def __init__(self, model_name="pednet", threshold=0.5, **kwargs):
        global _global_net

        if _global_net is None:
            try:
                print(f"[INFO] Caricamento modello Jetson: {model_name}")
                _global_net = detectNet(model_name, [f"--threshold={threshold}"])
            except Exception as e:
                print(f"[ERROR] Errore nell'inizializzazione del modello: {e}")
                _global_net = None
        else:
            print(f"[INFO] Riutilizzo modello Jetson già caricato: {model_name}")

        self.net = _global_net
        self.threshold = threshold

    def run(self, image):
        if self.net is None:
            print("[WARN] Modello non inizializzato, skipping detection.")
            return image

        try:
            # Converti PIL -> numpy e RGB->BGR per OpenCV
            np_img = np.array(image)
            np_img = cv2.cvtColor(np_img, cv2.COLOR_RGB2BGR)
        except Exception as e:
            print(f"[ERROR] Errore nella conversione dell'immagine: {e}")
            return image

        try:
            cuda_img = cudaFromNumpy(np_img)
        except Exception as e:
            print(f"[ERROR] Errore nella conversione CUDA: {e}")
            return image

        try:
            detections = self.net.Detect(cuda_img)
        except Exception as e:
            print(f"[ERROR] Errore durante la rilevazione: {e}")
            return image

        try:
            for det in detections:
                classID = det.ClassID
                confidence = det.Confidence
                left, top, right, bottom = int(det.Left), int(det.Top), int(det.Right), int(det.Bottom)
                label = self.net.GetClassDesc(classID)
                cv2.rectangle(np_img, (left, top), (right, bottom), (0, 255, 0), 2)
                cv2.putText(np_img, f"{label}: {confidence*100:.1f}%", (left, top-10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
        except Exception as e:
            print(f"[ERROR] Errore durante il disegno dei bounding box: {e}")

        try:
                    # Libera esplicitamente le risorse CUDA
            del cuda_img
            import gc
            gc.collect()
            return Image.fromarray(cv2.cvtColor(np_img, cv2.COLOR_BGR2RGB))
        except Exception as e:
            print(f"[ERROR] Errore nella conversione finale in PIL: {e}")
            return image
