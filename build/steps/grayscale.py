from PIL import ImageOps

class Grayscale:
    def __init__(self, **kwargs):
        pass

    def run(self, image, load_profile="light"):
        return ImageOps.grayscale(image).convert("RGB")
