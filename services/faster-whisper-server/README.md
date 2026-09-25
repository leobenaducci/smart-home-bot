# faster-whisper-server

Minimal speech-to-text API server built with [FastAPI](https://fastapi.tiangolo.com/) wrapping [faster-whisper](https://github.com/guillaumekln/faster-whisper) — a reimplementation of OpenAI's Whisper model using CTranslate2 for GPU-accelerated inference.

Runs on the GPU box (`compute.home:8000`). HomeCore's `/chat/transcribe` and the
audio branch of `/chat/voice` are the callers; on-device speech recognition on
the phone is the primary path now, and this is the fallback for it and for older
APKs.

## Endpoints

| Method | Path            | Description                   |
|--------|-----------------|-------------------------------|
| POST   | `/transcribe`   | Upload audio for transcription |

### `POST /transcribe`

`multipart/form-data`:

| Field | Required | Meaning |
|---|---|---|
| `file` | yes | The audio to transcribe |
| `language` | no | ISO language code. Empty → auto-detect. **Must be a bare subtag** — faster-whisper rejects a region tag like `es-ES`, so callers strip it and send `es`. |
| `initial_prompt` | no | Vocabulary/language bias. Empty → none. |

> `initial_prompt` biases the decoder, and whatever you put in it **can leak into
> the transcript**. HomeCore keeps its `WHISPER_PROMPT` empty on purpose for that
> reason and strips known echoes on the way out.

**Response:**
```json
{
  "language": "en",
  "text": "The full transcribed text...",
  "segments": [
    { "start": 0.0, "end": 2.5, "text": "First segment" }
  ]
}
```

Interactive docs at `/docs` (Swagger) and `/redoc`.

## Requirements

- NVIDIA GPU with CUDA 12.2+
- Docker (recommended) **or** Python 3.10+
- ffmpeg (system dependency)

## Quick start

### Docker (recommended)

```bash
docker compose up -d --build
```

### Python

```bash
pip install -r whisper/requirements.txt
python whisper/server.py
```

The server starts on `http://0.0.0.0:8000`.

### Usage

```bash
curl -X POST http://localhost:8000/transcribe -F "file=@audio.mp3" -F "language=es"
```

## Configuration

Model selection is environment-driven, so it can be retuned without a code edit
(`whisper/server.py` reads these at import; `docker-compose.yml` passes them
through in `${VAR:-default}` form):

| Env var | Default | Notes |
|---|---|---|
| `WHISPER_MODEL` | `large-v3-turbo` | Near large-v3 accuracy, ~8× faster, multilingual. Use `large-v3` for maximum accuracy. |
| `WHISPER_DEVICE` | `cuda` | |
| `WHISPER_COMPUTE_TYPE` | `float16` | |

Still hardcoded in `whisper/server.py`, change them there if needed:

| Setting | Value | Why |
|---|---|---|
| Beam size | `5` | |
| `vad_filter` | `True` | Trims silence — this is what stops Whisper hallucinating text into quiet or very short clips. |
| `condition_on_previous_text` | `False` | Short mic utterances otherwise fall into repetition loops from prior-text conditioning. |
| Host / port | `0.0.0.0:8000` | |

The Hugging Face model cache is a mounted volume (`./hf_cache` →
`/cache/huggingface`, via `HF_HOME`). Keep it: without it every rebuild
re-downloads the weights, and `large-v3-turbo` is not small.

## Deploying

A **manual** Jenkins run (`Jenkinsfile`, agent `compute`) — pushing does not
deploy. It does `docker compose down --remove-orphans`, then `docker rm -f
faster-whisper` (the `container_name` is pinned, and a container created under a
different compose project is invisible to `down`, so `up` would hit a "name
already in use" conflict), then `docker compose up -d --build`.

`restart: unless-stopped` keeps it up across reboots of the GPU box.

## Project structure

```
.
├── whisper/
│   ├── server.py          # FastAPI application
│   ├── Dockerfile         # CUDA 12.2 Docker image
│   └── requirements.txt   # Python dependencies
├── docker-compose.yml
└── Jenkinsfile
```
