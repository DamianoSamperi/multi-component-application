import subprocess
import tempfile
import os
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
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as input_file:
            image.save(input_file.name, format="JPEG")
            input_path = input_file.name

        output_path = tempfile.mktemp(suffix=".jpg")

        cmd = self.cmd_base + ["-i", input_path, "-o", output_path, "-s", self.scale_factor]
        if self.tta:
            cmd.append("-x")

        subprocess.run(cmd, check=True)
        img = cv2.imread(output_path)
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        os.remove(input_path)
        os.remove(output_path)

        return Image.fromarray(img_rgb)
