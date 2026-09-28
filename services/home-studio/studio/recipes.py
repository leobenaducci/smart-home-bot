"""What each kind of job asks WanGP for.

A recipe turns the studio's own request -- a shot, a song, a picture -- into
the settings WanGP takes, on top of WanGP's defaults for that model (the
worker merges them: `get_default_settings`). Keeping the mapping here, and
nowhere else, is what lets the page and Alfred speak in shots and songs
rather than in `image_prompt_type` letters and frame counts.

Models and their knobs are the ones the prototype measured on this card
(docs/home-studio.md). Change one here, measure it there.
"""
from __future__ import annotations

import math

VIDEO_MODEL = "minimax_h3_fl2va_pruned"
IMAGE_MODEL = "z_image"
SONG_MODEL = "ace_step_v1_5_turbo_lm_1_7b"
INSTRUMENTAL_MODEL = "stable_audio3_medium"
VOICE_MODEL = "qwen3_tts_base"

# `analyze` is a song listened to (studio/analysis.py): not WanGP's, run by the
# manager itself on the Studio's audio.cpp -- in the same queue, so it never
# shares the card with a render.
# `board` is a storyboard frame: a picture made for a shot, on the image model.
# `portrait` is a picture of a character, on the image model, filed on it.
# `score` is a song written out as parts (studio/score.py), on audio.cpp too.
KINDS = ("image", "song", "instrumental", "voice", "video_shot", "edit", "analyze", "repaint", "board",
         "portrait", "score")
MODEL_OF = {"image": IMAGE_MODEL, "song": SONG_MODEL, "instrumental": INSTRUMENTAL_MODEL,
            "voice": VOICE_MODEL, "video_shot": VIDEO_MODEL, "edit": VIDEO_MODEL,
            "analyze": "audio.cpp", "repaint": "audio.cpp", "board": IMAGE_MODEL, "portrait": IMAGE_MODEL,
            "score": "audio.cpp"}

FPS = 24
# H3 takes 17n + 5 frames: 124 is ~5 s, 481 (its largest window) ~20 s.
H3_MIN_FRAMES, H3_STEP, H3_OFFSET, H3_MAX_FRAMES = 107, 17, 5, 481
# The 8.5 GB GGUF text encoder: the bf16 one is ~65 GB of RAM this house
# does not have to spare (docs/home-studio.md).
H3_CONFIG = "gguf_q2_k"
IMAGE_SIZES = ("1024x1024", "1216x832", "832x1216", "1344x768", "768x1344")
VIDEO_SIZES = ("832x480", "480x832", "608x352", "640x640")


class RecipeError(ValueError):
    pass


def h3_frames(seconds: float) -> int:
    """The H3 frame count nearest to *seconds*, within what one shot takes."""
    want = max(H3_MIN_FRAMES, min(H3_MAX_FRAMES, round(seconds * FPS)))
    n = round((want - H3_OFFSET) / H3_STEP)
    return max(H3_MIN_FRAMES, min(H3_MAX_FRAMES, n * H3_STEP + H3_OFFSET))


# The picture size a storyboard frame is drawn at for each video size: the
# image model's nearest shape, so the frame the shot starts from is framed
# the way the shot will be.
BOARD_SIZE = {"832x480": "1344x768", "480x832": "768x1344", "608x352": "1344x768", "640x640": "1024x1024"}


def h3_frames_at_least(seconds: float) -> int:
    """The smallest H3 frame count that lasts at least *seconds*: for a shot cut
    to the music, which is made a little long and trimmed to its cut."""
    want = max(H3_MIN_FRAMES, min(H3_MAX_FRAMES, math.ceil(seconds * FPS - 1e-6)))
    n = math.ceil((want - H3_OFFSET) / H3_STEP)
    return max(H3_MIN_FRAMES, min(H3_MAX_FRAMES, n * H3_STEP + H3_OFFSET))


def h3_prompt(shot: dict, language: str = "Spanish", index: int = 1) -> str:
    """H3's structured prompt, from the fields a person fills in.

    `integrated_multimodal_description` is the picture and the action, one
    shot; spoken lines go inside `<d>[Language] ...</d>` bound to a speaker
    id, which is how H3 keeps lips and words together.
    """
    desc = str(shot.get("prompt") or "").strip()
    if not desc:
        raise RecipeError("a shot needs a description")
    # Who is in it, as their characters say they look -- the same words in
    # every shot they are cast in, which is what keeps them recognisable.
    if str(shot.get("characters") or "").strip():
        desc += f" Characters: {str(shot['characters']).strip()}."
    seconds = shot.get("seconds") or 5
    lines = [f"integrated_multimodal_description: [Shot {index}] A {seconds:g}-second single take. {desc}"]
    dialogue = str(shot.get("dialogue") or "").strip()
    if dialogue:
        spoken = " ".join(f"(S1) <d>[{language}] {line.strip()}</d>"
                          for line in dialogue.splitlines() if line.strip())
        lines[0] += f" The character speaks clearly with precise lip synchronization: {spoken}"
    lines.append(f"overall_soundscape: {str(shot.get('soundscape') or 'natural ambient sound matching the scene').strip()}")
    lines.append(f"non_diegetic_music: {str(shot.get('music') or 'none').strip()}")
    return "\n".join(lines)


def settings_for(kind: str, p: dict) -> dict:
    """WanGP settings for one job. *p* is the job's params, with any file
    already resolved to an absolute path by the manager."""
    if kind not in KINDS:
        raise RecipeError(f"unknown kind {kind!r}")
    seed = int(p.get("seed", -1))
    if kind in ("image", "board", "portrait"):
        size = p.get("size") if p.get("size") in IMAGE_SIZES else IMAGE_SIZES[0]
        if not str(p.get("prompt") or "").strip():
            raise RecipeError("a picture needs a description")
        return {"model_type": IMAGE_MODEL, "image_mode": 1, "resolution": size,
                "prompt": p["prompt"], "seed": seed}
    if kind == "song":
        lyrics = str(p.get("lyrics") or "").strip() or "[Instrumental]"
        out = {"model_type": SONG_MODEL, "prompt": lyrics,
               "alt_prompt": str(p.get("style") or "").strip(),
               "duration_seconds": max(10, min(600, int(p.get("seconds") or 60))),
               "seed": seed, "custom_settings": {"language": str(p.get("language") or "es")}}
        if p.get("bpm"):
            out["custom_settings"]["bpm"] = int(p["bpm"])
        # A retouch of a song already made: ACE-Step's cover mode, the whole
        # song again held to the original by `strength` (WanGP's Source Audio
        # Strength), and its singer's timbre kept too when asked. Not a repaint
        # of one stretch -- that is the audio unit's (studio/analysis.py).
        if p.get("source_file"):
            keep_voice = bool(p.get("keep_voice"))
            out.update(audio_prompt_type="AB" if keep_voice else "A", audio_guide=p["source_file"],
                       audio_scale=max(0.1, min(1.0, float(p.get("strength") or 0.8))))
            if keep_voice:
                out["audio_guide2"] = p["source_file"]
        return out
    if kind == "instrumental":
        if not str(p.get("style") or "").strip():
            raise RecipeError("an instrumental needs a description of the sound")
        return {"model_type": INSTRUMENTAL_MODEL, "prompt": p["style"],
                "duration_seconds": max(5, min(380, int(p.get("seconds") or 30))), "seed": seed}
    if kind == "voice":
        if not p.get("voice_file"):
            raise RecipeError("a cloned voice needs a sample of that voice")
        if not str(p.get("text") or "").strip():
            raise RecipeError("nothing to say")
        return {"model_type": VOICE_MODEL, "prompt": p["text"], "audio_prompt_type": "A",
                "audio_guide": p["voice_file"], "alt_prompt": str(p.get("voice_text") or ""),
                "duration_seconds": max(5, min(300, int(p.get("seconds") or 60))), "seed": seed}
    # Video: a shot, or an edit of one.
    size = p.get("size") if p.get("size") in VIDEO_SIZES else VIDEO_SIZES[0]
    out = {"model_type": VIDEO_MODEL, "config": H3_CONFIG, "resolution": size,
           "video_length": (h3_frames_at_least if p.get("exact") else h3_frames)(float(p.get("seconds") or 5)),
           "num_inference_steps": int(p.get("steps") or 20), "seed": seed,
           "prompt": h3_prompt(p, p.get("language_name") or "Spanish", int(p.get("index") or 1))}
    flags = ""
    if p.get("start_image"):
        flags += "S"
        out["image_start"] = p["start_image"]
    if p.get("end_image"):
        flags += "E"
        out["image_end"] = p["end_image"]
    out["image_prompt_type"] = flags or "T"
    if kind == "edit":
        # The shot's own video as the control, re-denoised: lower strength
        # keeps more of it. A part of a shot is edited by giving the whole
        # shot and anchoring both ends to its own first and last frames.
        if not p.get("source_video"):
            raise RecipeError("nothing to edit")
        out["video_guide"] = p["source_video"]
        out["video_prompt_type"] = "V"
        out["denoising_strength"] = max(0.2, min(1.0, float(p.get("strength") or 0.6)))
    return out


def estimate_note(kind: str, p: dict) -> str:
    """A short human title for the queue: what, never the prompt itself."""
    if kind == "video_shot":
        return f"{h3_frames(float(p.get('seconds') or 5)) / FPS:.0f} s de video"
    if kind == "edit":
        return "retoque de video"
    if kind == "song":
        return f"canción de {int(p.get('seconds') or 60)} s"
    if kind == "instrumental":
        return f"música de {int(p.get('seconds') or 30)} s"
    if kind == "voice":
        return "voz"
    if kind == "analyze":
        return "escuchar una canción"
    if kind == "score":
        return "partituras de una canción"
    if kind == "board":
        return "storyboard"
    if kind == "portrait":
        return "retrato de un personaje"
    if kind == "repaint":
        return f"rehacer {max(1, round(float(p.get('end') or 0) - float(p.get('start') or 0)))} s de una canción"
    return "imagen"


def shots_needed(seconds: float) -> int:
    """How many shots a scene of *seconds* takes at H3's best length."""
    return max(1, math.ceil(seconds / 15))
