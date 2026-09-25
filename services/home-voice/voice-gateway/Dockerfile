FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-test.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -r requirements-test.txt

# openWakeWord, deliberately with --no-deps.
#
# Its metadata hard-requires tflite-runtime on Linux, which publishes no wheel
# for python 3.12 and cannot build here — a plain install fails outright. That
# dependency is for its *tflite* inference path; the gateway asks for
# `inference_framework="onnx"` and never touches it. Its genuine runtime
# dependencies are pinned in requirements.txt above.
#
# The same trade as piper: one package's wheel matrix does not get to choose the
# interpreter for the whole service.
RUN pip install --no-cache-dir --no-deps openwakeword==0.6.0 \
    && python -c "from openwakeword.model import Model; print('openwakeword imports ok')"

# Piper comes from its release tarball, NOT from pip.
#
# `pip install piper-tts` pulls piper-phonemize, which publishes no wheel for
# python 3.12 and cannot build from source here — the install fails outright.
# Pinning python to 3.11 to satisfy it would mean choosing the interpreter for
# the whole service based on one dependency's wheel matrix.
#
# The tarball is self-contained: the binary, its ONNX runtime, and espeak-ng-data
# beside it. The CLI contract (`--model X --output_raw` → raw int16 on stdout) has
# been stable across releases, which is why gateway.py shells out rather than
# importing anything.
#
# x86_64 because this runs on compute. A pi would need piper_linux_aarch64.
ARG PIPER_RELEASE=2023.11.14-2
RUN curl -fsSL "https://github.com/rhasspy/piper/releases/download/${PIPER_RELEASE}/piper_linux_x86_64.tar.gz" \
        -o /tmp/piper.tgz \
    && tar -xzf /tmp/piper.tgz -C /opt \
    && rm /tmp/piper.tgz \
    # The binary needs its own .so files, which are not on the default loader
    # path. A one-line wrapper keeps that detail out of gateway.py, which just
    # calls `piper`.
    && printf '#!/bin/sh\nexec env LD_LIBRARY_PATH=/opt/piper /opt/piper/piper "$@"\n' > /usr/local/bin/piper \
    && chmod +x /usr/local/bin/piper

# The voice is baked in rather than downloaded at boot: a container that pulls
# ~60 MB from HuggingFace on every start stops working the day that host is
# unreachable, and the house's own machines are meant to keep talking when the
# internet does not.
#
# Chosen by listening, 2026-08-13, which is the only way this can be chosen.
#
# The previous voice was es_MX-claude-high, and the argument for it was written
# down here: it phonemizes as es-419, so a Chilean household gets Latin American
# Spanish. That argument is sound and it lost anyway. Piper's entire Latin
# American catalogue is three voices — es_MX-ald, es_MX-claude, es_AR-daniela —
# and none of the two male ones carries a butler. A bake-off of all four
# plausible candidates against the same line picked es_ES-davefx: Castilian, and
# the formal register that makes it sound foreign in casual speech is the same
# one that makes it sound right coming from Alfred.
#
# What it costs, and it is worth listening for: Chilean vocabulary in a
# Peninsular mouth. In the bake-off the house's own whisper heard davefx say
# "living" and transcribed "línea". One transcription proves nothing, but that
# class of word — living, palta, micro — is where this voice will grate, and it
# will grate on the twentieth hearing rather than the first.
#
# Changing it is these two args plus a rebuild. Same 22050 Hz as before, so no
# device hears about it; the rate travels per clip regardless.
ARG PIPER_VOICE=es_ES-davefx-medium
ARG PIPER_BASE=https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_ES/davefx/medium
RUN mkdir -p /models \
    && curl -fsSL "${PIPER_BASE}/${PIPER_VOICE}.onnx" -o /models/voice.onnx \
    && curl -fsSL "${PIPER_BASE}/${PIPER_VOICE}.onnx.json" -o /models/voice.onnx.json \
    # Fail the build, not the first request, if either file is wrong: prove the
    # model loads and actually emits audio.
    #
    # With the SAME synthesis flags gateway.py passes at runtime. The C++ piper
    # throws on an argument it does not recognise, so a flag spelled wrong here
    # is not a slightly different voice — it is every reply, every announcement
    # and every turn failing, on an image whose build went green. These four are
    # the only part of the runtime command line the probe cannot infer, and they
    # arrived after the probe was written.
    && echo "probando la voz" | piper --model /models/voice.onnx --output_raw \
        --length_scale 1.18 --noise_scale 0.55 --noise_w 0.65 --sentence_silence 0.45 \
        > /tmp/probe.pcm \
    && python -c "import json,os; \
        rate=json.load(open('/models/voice.onnx.json'))['audio']['sample_rate']; \
        n=os.path.getsize('/tmp/probe.pcm'); \
        assert n > rate, f'piper produced only {n} bytes'; \
        print(f'voice ok: {n} bytes at {rate} Hz')" \
    && rm /tmp/probe.pcm

# openWakeWord's shared feature extractors — a melspectrogram model and a speech
# embedding model that every wake word sits on top of. Downloaded at build so a
# container start does not depend on GitHub being reachable, for the same reason
# the piper voice is baked in.
#
# The *wake word itself* is deliberately NOT here: it lives in /config beside
# devices.json, because tuning a wake word is iterative and swapping the file
# should be a restart rather than a rebuild.
RUN python -c "import openwakeword.utils as u; u.download_models()" \
    && python -c "import openwakeword, os; \
        p=os.path.dirname(openwakeword.__file__)+'/resources/models'; \
        print('oww base models:', sorted(os.listdir(p))[:6])"

# Both modules, named rather than a wildcard. `gateway.py` alone is how the
# usage reporter shipped as an import of a file that was not in the image:
# the build succeeded, and every test collected against it failed on
# ModuleNotFoundError -- which the deployer caught before replacing the
# running container, because it runs the suite against the built image first.
COPY gateway.py local_usage.py ./

# This runs with `network_mode: host`, so it binds the host's port directly and
# cannot dodge a collision with a published mapping. It was 8083 because 8081 on
# that box was cAdvisor's: the first deploy crash-looped on "address already in
# use" while the health check went green against cAdvisor's redirect. Both
# numbers are now inside the stack's own 21000-21499 band, which is the general
# fix for the same problem.
#
# Env rather than a literal so a move is one variable and not a sweep of fifteen
# files, which is what the last one cost. The compose file supplies it from
# `services.home-voice.port`; this is only the fallback for a bare `docker run`.
ENV PORT=21011
EXPOSE 21011
CMD ["sh", "-c", "exec uvicorn gateway:app --host 0.0.0.0 --port ${PORT}"]
