# audio.cpp: TTS and ASR in one C++ runtime, on the CPU.
#
# Two stages, and the split is the whole point. The build stage carries cmake,
# gcc and the source tree; the runtime stage carries two binaries and their
# shared libraries. `services/home-voice/qwen-tts` next door needs torch and
# CUDA and is several gigabytes for it -- this is the alternative being
# measured, so an image that quietly grew a Python stack would be measuring
# the wrong thing.
#
# CPU by default. The card in this box is already three services deep
# (Ollama's models, the camera detectors, z-image), and Piper's real advantage
# is that it costs none of it.
#
# The GPU build is behind `docker-compose.gpu.yml`, the way the camera wall
# does it, and it is a *different image* rather than a runtime flag: a CUDA
# backend needs nvcc at build time and the CUDA runtime libraries at run time,
# neither of which belongs in the CPU image that this bench exists to defend.
# Nothing below changes for a CPU build -- same bases, same backend, same
# binaries -- so `docker compose --profile bench build` still produces what it
# produced before.
ARG BUILD_BASE=ubuntu:24.04
ARG RUNTIME_BASE=ubuntu:24.04
# cpu | cuda | vulkan | hip. Passed straight to scripts/build_linux.sh.
ARG AUDIOCPP_BACKEND=cpu
# Which cards to emit code for. 86 is this box's RTX 3060 (compute 8.6), and
# naming one costs minutes instead of the half-hour every-architecture build
# that ships a binary for cards nobody here has. Empty means the script
# decides.
ARG CUDA_ARCH=

FROM ${BUILD_BASE} AS build
ARG AUDIOCPP_BACKEND
ARG CUDA_ARCH

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates cmake ninja-build build-essential pkg-config \
        libcurl4-openssl-dev python3 \
    && rm -rf /var/lib/apt/lists/*

# Pinned to a commit, not a branch. This is 0.7.x and moving weekly -- 0.6 to
# 0.7 to 0.7.2 inside a month -- so a floating checkout would mean a different
# engine every rebuild, which is the one thing a bench must not have.
# v0.7.2 was a2a4d4f34c0960edf1bf78e392de1fbce5a855e6 when this was written.
# A tag can be moved; if a rebuild produces different numbers, check that first.
ARG AUDIOCPP_REF=v0.7.2
RUN git clone --recursive --depth 1 --branch "${AUDIOCPP_REF}" \
        https://github.com/0xShug0/audio.cpp.git /src

# The build directory is named after the backend -- `build/linux-cpu-release`,
# `build/linux-cuda-release` -- so the copy has to follow it rather than being
# spelled once. Getting that wrong produces an image with no binaries in it and
# a `docker run` that fails on exec, which reads like a broken entrypoint.
WORKDIR /src
RUN scripts/build_linux.sh --backend "${AUDIOCPP_BACKEND}" \
        ${CUDA_ARCH:+--cuda-arch "${CUDA_ARCH}"} \
        --target audiocpp_cli --target audiocpp_server \
    && mkdir -p /out \
    && cp -a "build/linux-${AUDIOCPP_BACKEND}-release/bin/." /out/ \
    && strip /out/* 2>/dev/null || true


FROM ${RUNTIME_BASE}

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates libgomp1 libcurl4 python3 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /out/ /usr/local/bin/
# The model fetcher and the specs it reads. A package here is not one file:
# the .gguf named in the HF listing is an *index* -- 1 KB of
# `audiocpp.model_spec.json` pointing at the real weights -- so fetching it
# with curl gets a valid GGUF that contains no model. That is how this was
# first written, and the container came up serving a 1058-byte "model".
COPY --from=build /src/tools/model_manager_v2.py /app/tools/model_manager_v2.py
COPY --from=build /src/model_specs/ /app/model_specs/

# Weights are NOT baked, the same call the TTS bench next door makes: they are
# a cache of somebody else's published file, /models is a volume, and baking
# them would make every rebuild a multi-hundred-megabyte pull.
ENV AUDIOCPP_MODELS=/models
WORKDIR /app
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
