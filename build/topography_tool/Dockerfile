FROM python:3.10-slim

# Installazione dipendenze di sistema
RUN apt-get update && apt-get install -y \
    curl \
    bash \
    && rm -rf /var/lib/apt/lists/*

# Install kubectl
ENV KUBECTL_VERSION=v1.30.0
RUN curl -LO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" && \
    install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl && \
    rm kubectl

# Copia il codice nel container
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Flask server sulla porta 8080
EXPOSE 8080
CMD ["python", "pipeline_server.py"]
