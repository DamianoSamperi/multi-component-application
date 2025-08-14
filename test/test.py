import requests
from PIL import Image
import torchvision.transforms as transforms
import torch
import json

# Carica e trasforma immagine
img = Image.open("test.jpg").convert("RGB")

transform = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor()
])

tensor = transform(img).unsqueeze(0)  # shape: (1, 3, 224, 224)

# Converti in lista JSON serializzabile
x_input = tensor.numpy().tolist()

# Invia richiesta POST
url = "http://localhost:5000/process"
response = requests.post(url, json={"x": x_input})

# Stampa risposta
print("Status:", response.status_code)
print("Output:", response.text)
