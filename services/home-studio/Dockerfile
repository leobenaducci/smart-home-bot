# home-studio: the manager (API + queue) and the WanGP worker it starts.
#
# A plain Ubuntu base, not nvidia/cuda: PyTorch's cu130 wheels bring their own
# CUDA libraries, and a CUDA base image puts a second libcublasLt on the path
# -- the prototype's first matrix multiply failed with
# CUBLAS_STATUS_INVALID_VALUE until the two stopped mixing (docs/home-studio.md).
# The driver comes from the host through the NVIDIA runtime (580+, CUDA 13).
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1 HF_HUB_DISABLE_XET=1 \
    UV_PYTHON_INSTALL_DIR=/opt/python UV_LINK_MODE=copy PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates ffmpeg libgl1 libglib2.0-0 build-essential cmake ninja-build \
        libsndfile1 portaudio19-dev && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
RUN uv venv --python 3.11 /opt/venv
ENV PATH=/opt/venv/bin:$PATH VIRTUAL_ENV=/opt/venv
# torch 2.10 on CUDA 13: WanGP's INSTALLATION.md for RTX 20xx-50xx. cu128
# silently runs H3's int8 kernels eagerly, ~3x slower (the 3060 write-up).
RUN uv pip install torch==2.10.0 torchvision==0.25.0 torchaudio==2.10.0 \
        --index-url https://download.pytorch.org/whl/cu130
# WanGP at the commit the prototype measured. Bump deliberately and measure
# again: its model defaults and settings move between releases.
ARG WANGP_REF=91301f0e
RUN git clone https://github.com/deepbeepmeep/Wan2GP.git /opt/wangp && \
    git -C /opt/wangp checkout -q ${WANGP_REF} && rm -rf /opt/wangp/.git
RUN uv pip install -r /opt/wangp/requirements.txt --index-strategy unsafe-best-match && \
    uv pip install "fastapi>=0.115" "uvicorn>=0.30" requests python-multipart
# WanGP's settings for this house: Triton INT8 kernels (Comfy Kitchen's refuse
# ACE-Step's layer shapes on this card), checkpoints on the mounted volume.
COPY wgp_config.json /opt/wangp/wgp_config.json
COPY studio /app/studio
WORKDIR /app
ENV WANGP_ROOT=/opt/wangp STUDIO_DATA=/data
EXPOSE 8090
CMD ["uvicorn", "studio.app:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
