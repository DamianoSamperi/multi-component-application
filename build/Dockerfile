FROM nvcr.io/nvidia/l4t-pytorch:r32.7.1-pth1.10-py3

WORKDIR /app

# Fix bad source (L4T-specific issue)
RUN sed -i '/kitware.com/d' /etc/apt/sources.list

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    python3-pip python3-dev libjpeg-dev libpng-dev \
    libglib2.0-0 libsm6 libxext6 libxrender1 netcat && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python deps
COPY requirements.txt ./
RUN python3 -m pip install --upgrade pip && \
    python3 -m pip install --no-cache-dir -r requirements.txt
ENV PYTHONUNBUFFERED=1
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
# Copy app code and pretrained model from builder stage
COPY app.py ./
COPY resnet50.pt ./


EXPOSE 5000

CMD ["python3", "app.py"]


