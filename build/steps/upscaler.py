import subprocess
import tempfile
import os
import uuid
import cv2
from PIL import Image
import numpy as np

class Upscaler:
    def __init__(self, model_path, scale_factor=4, tta=False, **kwargs):
        self.model_path = model_path
        self.scale_factor = str(scale_factor)
        self.tta = tta
        self.cmd_base = ["/root/realsr-ncnn-vulkan/build/realsr-ncnn-vulkan", "-m", model_path, "-f", "jpg"]

    def run(self, image):
        tmp_dir = tempfile.gettempdir()
        uid = uuid.uuid4().hex
        input_path = os.path.join(tmp_dir, f"input_{uid}.jpg")
        output_path = os.path.join(tmp_dir, f"output_{uid}.jpg")
        image.save(input_path, format="JPEG")
        # with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as input_file:
        #     image.save(input_file.name, format="JPEG")
        #     input_path = input_file.name
        # output_path = tempfile.mktemp(suffix=".jpg")

        cmd = self.cmd_base + ["-i", input_path, "-o", output_path, "-s", self.scale_factor]
        if self.tta:
            cmd.append("-x")

        try:
            # ⏱️ subprocess.run con timeout per evitare blocchi indefiniti
            subprocess.run(cmd, check=True, timeout=1000)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Upscaler timeout: GPU bloccata o troppo lenta")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Errore durante l'upscaling: {e}")
        finally:
            # Pulisce i file in ogni caso
            if os.path.exists(input_path):
                os.remove(input_path)
                
        img = cv2.imread(output_path)
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        try:
            os.remove(output_path)
        except FileNotFoundError:
            pass

        return Image.fromarray(img_rgb)
