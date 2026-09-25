FROM nvidia/cuda:12.2.0-runtime-ubuntu22.04
ARG DEBIAN_FRONTEND=noninteractive

# Install FFmpeg with NVENC support
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libavcodec-extra \
    libavformat-extra \
    libavfilter-extra \
    libavutil-dev \
    libswscale-dev \
    libswresample-dev \
    libavdevice-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python and dependencies
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    libgl1 \
    libglib2.0-0 \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app/web_server

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directories for persistent data
RUN mkdir -p /app/config /app/logs /app/data/recordings

# Expose port
EXPOSE 5000

# Run the application
CMD ["python3", "/app/web_server/main.py", "--host", "0.0.0.0", "--port", "5000"]