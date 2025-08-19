import requests

image_file = "your_image.jpg"
f = open(image_file, "rb")
files = {"image": (image_file, f, "image/jpeg")}

url = "http://192.168.1.211:5000/process"

response = requests.post(url, files=files)

f.close()  # chiudi solo dopo la richiesta

print("Status:", response.status_code)
if response.status_code == 200:
    with open("output.jpg", "wb") as out:
        out.write(response.content)
    print("Immagine salvata come output.jpg")
else:
    print("Errore:", response.text)
