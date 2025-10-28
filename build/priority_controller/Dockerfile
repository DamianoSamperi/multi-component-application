FROM python:3.10-slim

WORKDIR /app

# Installa dipendenze
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia il codice
COPY controller.py .

CMD ["python", "controller.py"]
