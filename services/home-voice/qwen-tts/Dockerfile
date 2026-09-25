# The Qwen3-TTS sidecar. Big, GPU-bound, and optional — which is exactly why it
# is not part of the voice gateway's image.
#
# The gateway is ~1 GB and starts in seconds because it holds piper (a 20 MB
# binary) and an ONNX wake model. Putting torch and CUDA next to it would make
# every voice-gateway deploy pull several gigabytes to change a timeout.
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/models

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Weights are NOT baked, unlike the piper voice next door, and the difference is
# deliberate. The piper voice is 60 MB and the house must keep talking with no
# internet, so it ships inside the image. This is gigabytes and it is a bench:
# baking it would make every rebuild of a comparison rig a multi-GB pull, and
# the house does not depend on it being there. The cache is a volume, so it is
# downloaded once and survives rebuilds.
COPY server.py .

EXPOSE 8082
CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8082"]
