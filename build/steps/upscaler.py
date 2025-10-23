import subprocess
import tempfile
import os
import uuid
import cv2
from PIL import Image
import numpy as np
import threading
import time

gpu_lock = threading.Semaphore(1)

class Upscaler:
    def __init__(self, model_path, scale_factor=4, tta=False, max_retries=2, retry_delay=2, **kwargs):
        self.model_path = model_path
        self.scale_factor = str(scale_factor)
        self.tta = tta
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.cmd_base = [
            "/root/realsr-ncnn-vulkan/build/realsr-ncnn-vulkan",
            "-m", model_path,
            "-f", "jpg"
        ]

    def run(self, image):
        tmp_dir = tempfile.gettempdir()
        uid = uuid.uuid4().hex
        input_path = os.path.join(tmp_dir, f"input_{uid}.jpg")
        output_path = os.path.join(tmp_dir, f"output_{uid}.jpg")
        image.save(input_path, format="JPEG")

        cmd = self.cmd_base + ["-i", input_path, "-o", output_path, "-s", self.scale_factor]
        if self.tta:
            cmd.append("-x")

        attempt = 0
        while attempt < self.max_retries:
            with gpu_lock:
                try:
                    subprocess.run(cmd, check=True, timeout=1000, capture_output=True)
                    break  # success, esce dal ciclo
                except subprocess.CalledProcessError as e:
                    stderr = e.stderr.decode(errors="ignore") if e.stderr else ""
                    if "vkCreateDevice failed" in stderr:
                        attempt += 1
                        print(f"GPU saturata (tentativo {attempt}/{self.max_retries}), attendo {self.retry_delay}s...")
                        time.sleep(self.retry_delay)
                        continue  # riprova
                    else:
                        raise RuntimeError(f"Errore durante l'upscaling: {stderr or e}")
                except subprocess.TimeoutExpired:
                    raise RuntimeError("Timeout: GPU bloccata o troppo lenta durante l’upscaling.")
                finally:
                    if os.path.exists(input_path):
                        os.remove(input_path)

        if attempt == self.max_retries:
            raise RuntimeError("GPU sempre occupata dopo vari tentativi, richiesta annullata.")

        if not os.path.exists(output_path):
            raise RuntimeError("Nessun file di output generato da realsr-ncnn-vulkan.")

        img = cv2.imread(output_path)
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        try:
            os.remove(output_path)
        except FileNotFoundError:
            pass

        return Image.fromarray(img_rgb)
