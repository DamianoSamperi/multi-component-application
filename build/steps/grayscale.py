from PIL import ImageOps

class Grayscale:
    def __init__(self, **kwargs):
        pass

    def run(self, image):
        return ImageOps.grayscale(image).convert("RGB")
