from PIL import ImageFilter

class Deblur:
    def __init__(self, **kwargs):
        pass

    def run(self, image, load_profile="light"):
        return image.filter(ImageFilter.SHARPEN)
