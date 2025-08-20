from PIL import ImageFilter

class Deblur:
    def __init__(self, **kwargs):
        pass

    def run(self, image):
        return image.filter(ImageFilter.SHARPEN)
