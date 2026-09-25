import uvicorn
from fastapi import FastAPI, UploadFile, File, Form
from faster_whisper import WhisperModel
import tempfile
import os

app = FastAPI()

# Configurable via env (see docker-compose.yml). Defaults to large-v3-turbo:
# near large-v3 accuracy, ~8x faster, multilingual (Spanish + English).
MODEL = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
DEVICE = os.environ.get("WHISPER_DEVICE", "cuda")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "float16")

model = WhisperModel(MODEL, device=DEVICE, compute_type=COMPUTE_TYPE)


@app.get("/health")
async def health():
    """Liveness, and specifically liveness *after* the model has loaded.

    The model is loaded at import time and can take minutes on a cold cache, so
    a deploy needs something to poll that means "ready", not merely "listening".
    Reporting the model name makes a rebuild distinguishable from a container
    that never advanced -- the two look identical from outside otherwise.
    """
    return {"status": "ok", "model": MODEL, "device": DEVICE}


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    initial_prompt: str = Form(default=""),
    language: str = Form(default=""),
):
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    segments, info = model.transcribe(
        tmp_path,
        beam_size=5,
        # Bias language/vocabulary from the caller's hint (HomeCore sends one).
        initial_prompt=initial_prompt or None,
        # Optional explicit language; empty -> auto-detect.
        language=language or None,
        # Trim silence: cuts Whisper's hallucinated text on quiet/short clips.
        vad_filter=True,
        # Short mic utterances: avoid repetition loops from prior-text conditioning.
        condition_on_previous_text=False,
    )
    segments = list(segments)

    result = {
        "language": info.language,
        "text": "".join(s.text for s in segments),
        "segments": [
            {
                "start": s.start,
                "end": s.end,
                "text": s.text
            } for s in segments
        ]
    }

    os.remove(tmp_path)
    return result


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
