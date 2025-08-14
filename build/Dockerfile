###########################################
# Stage 1: Build environment
###########################################
FROM nvcr.io/nvidia/l4t-base:r32.7.1 AS builder

WORKDIR /root
ENV DEBIAN_FRONTEND=noninteractive

# Install build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    sudo ca-certificates build-essential wget m4 flex bison \
    libssl-dev libvulkan-dev git cmake ninja-build python3-dev python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Build and install GCC 10.1
RUN wget https://ftpmirror.gnu.org/gcc/gcc-10.1.0/gcc-10.1.0.tar.xz && \
    tar xf gcc-10.1.0.tar.xz && \
    cd gcc-10.1.0 && \
    sudo contrib/download_prerequisites && \
    mkdir gcc10build && cd gcc10build && \
    ../configure --prefix=/usr/local/gcc-10.1.0 --enable-languages=c,c++ --disable-multilib && \
    make -j$(nproc) && sudo make install && cd ../.. && rm -rf gcc-10.1.0 gcc-10.1.0.tar.xz

# Set GCC 10.1 as default
RUN sudo update-alternatives --install /usr/bin/gcc gcc /usr/local/gcc-10.1.0/bin/gcc 100 \
    --slave /usr/bin/g++ g++ /usr/local/gcc-10.1.0/bin/g++ \
    --slave /usr/bin/cpp cpp /usr/local/gcc-10.1.0/bin/cpp

# Build glslang
RUN git clone https://github.com/KhronosGroup/glslang.git && \
    cd glslang && git checkout tags/15.0.0 && \
    git clone https://github.com/google/googletest.git External/googletest && \
    ./update_glslang_sources.py && mkdir build && cd build && \
    cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$(pwd)/install" .. && \
    make -j$(nproc) install

# Build realsr-ncnn-vulkan (upscaler)
RUN git clone https://github.com/nihui/realsr-ncnn-vulkan.git && \
    cd realsr-ncnn-vulkan && git submodule update --init --recursive && \
    mkdir build && cd build && cmake ../src && cmake --build . -j$(nproc)

###########################################
# Stage 2: Runtime image
###########################################
FROM dustynv/jetson-inference:r32.7.1

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsm6 libxext6 libxrender-dev ffmpeg libvulkan-dev python3-pip python3-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy GCC toolchain from builder
COPY --from=builder /usr/local/gcc-10.1.0 /usr/local/gcc-10.1.0
COPY --from=builder /usr/bin/gcc /usr/bin/gcc
COPY --from=builder /usr/bin/g++ /usr/bin/g++

# Copy realsr-ncnn-vulkan built binaries
COPY --from=builder /root/realsr-ncnn-vulkan /root/realsr-ncnn-vulkan

# Copy glslang if needed
COPY --from=builder /root/glslang/build/install /root/glslang/install

# Set Python environment
ENV PYTHONIOENCODING=utf-8
ENV OPENBLAS_CORETYPE=ARMV8

# Install Python dependencies
COPY requirements.txt /app/
RUN pip3 install --upgrade pip setuptools wheel && \
    pip3 install --no-cache-dir -r requirements.txt && \
    pip3 install flask requests opencv-python-headless pika imageio==2.6.1 imageio-ffmpeg==0.3.0

# Copy application code
COPY app.py upscaler.py classifier.py /app/

# Expose Flask port
EXPOSE 5000

# Command to run Flask app
ENTRYPOINT ["/bin/bash", "-c", "ln -sf /usr/lib/aarch64-linux-gnu/liblapack.so /usr/lib/aarch64-linux-gnu/liblapack.so.3 && exec python3 app.py"]
