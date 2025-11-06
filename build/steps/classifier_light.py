import tensorflow as tf
import tensorflow_hub as hub
import numpy as np
import cv2
from PIL import Image

class Classifier:
    def __init__(self, model_name="pednet", threshold=0.5, **kwargs):
        self.model = hub.load("https://tfhub.dev/tensorflow/ssd_mobilenet_v2/2")
        self.threshold = threshold

    def run(self, image: Image.Image):
        np_img = np.array(image)
        input_tensor = tf.convert_to_tensor([np_img], dtype=tf.uint8)
        outputs = self.model(input_tensor)

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
