from jetson_inference import detectNet
from jetson_utils import cudaFromNumpy
import numpy as np
import cv2
from PIL import Image

class Classifier:
    def __init__(self, model_name="pednet", **kwargs):
        self.net = detectNet(model_name, ["--threshold=0.5"])

    def run(self, image):
        np_img = np.array(image)
        cuda_img = cudaFromNumpy(np_img)
        detections = self.net.Detect(cuda_img)

        for det in detections:
            classID = det.ClassID
            confidence = det.Confidence
            left, top, right, bottom = int(det.Left), int(det.Top), int(det.Right), int(det.Bottom)
            label = self.net.GetClassDesc(classID)
            cv2.rectangle(np_img, (left, top), (right, bottom), (0, 255, 0), 2)
            cv2.putText(np_img, f"{label}: {confidence*100:.1f}%", (left, top-10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

        return Image.fromarray(np_img)
