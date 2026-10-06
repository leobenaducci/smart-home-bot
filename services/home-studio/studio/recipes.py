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
import re

VIDEO_MODEL = "minimax_h3_fl2va_pruned"
IMAGE_MODEL = "z_image"
SONG_MODEL = "ace_step_v1_5_turbo_lm_1_7b"
INSTRUMENTAL_MODEL = "stable_audio3_medium"
VOICE_MODEL = "qwen3_tts_base"
# The memory profile a song, an instrumental or a voice asks for: WanGP's own
# audio default, each model whole on the card. The session's profile 5
# (worker.py) is H3's, and WanGP applies a command-line profile to *every*
# output, so without this a song streamed its weights from RAM per layer --
# the 1.7B LM decodes a token at a time, 2-3 GB/s over PCIe with the card 30%
# busy, and a 270 s song took 18-34 minutes.
AUDIO_PROFILE = 3.5
# Drawing *from pictures*: the cast's photos or portraits, and the pictures the
# person flagged as the film's style. Z-Image reads text only, so a character
# it draws is whoever its description makes them, different every frame.
# FLUX.2 klein takes ordered reference images and is told in the prompt what
# each one is. 4B, not 9B: it shares Z-Image's Qwen3 text encoder (already on
# disk), fits the card with room, and is Apache-licensed -- the 9B is not.
REF_IMAGE_MODEL = "flux2_klein_4b"
# The cast first (three at most, as the review compares), then up to two
# style pictures. Every reference is more for the model to hold and slower.
MAX_CAST_REFS, MAX_STYLE_REFS = 3, 2
# A shot's own reference pictures (its `refs`): what *that* shot is drawn
# from -- a place, an object, a composition -- beside the cast and the style.
MAX_SHOT_REFS = 2

# `analyze` is a song listened to (studio/analysis.py): not WanGP's, run by the
# manager itself on the Studio's audio.cpp -- in the same queue, so it never
# shares the card with a render.
# `board` is a storyboard frame: a picture made for a shot, on the image model.
# `portrait` is a picture of a character, on the image model, filed on it.
# `score` is a song written out as parts (studio/score.py), on audio.cpp too.
KINDS = ("image", "song", "instrumental", "voice", "video_shot", "edit", "analyze", "repaint", "board",
         "portrait", "score", "eyes")
MODEL_OF = {"image": IMAGE_MODEL, "song": SONG_MODEL, "instrumental": INSTRUMENTAL_MODEL,
            "voice": VOICE_MODEL, "video_shot": VIDEO_MODEL, "edit": VIDEO_MODEL,
            "analyze": "audio.cpp", "repaint": "audio.cpp", "board": IMAGE_MODEL, "portrait": IMAGE_MODEL,
            "score": "audio.cpp", "eyes": "liveportrait"}

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


def model_for(kind: str, params: dict) -> str:
    """The model a job runs on: its kind's, except a picture drawn from
    reference pictures (`with_refs`, set where the job is asked for)."""
    if kind in ("image", "board", "portrait") and params.get("with_refs"):
        return REF_IMAGE_MODEL
    return MODEL_OF[kind]


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


# "Name: words" -- a name of up to four words, then a colon. A sentence with
# a colon later in it ("Look: the sea") would be read as one, so the name is
# short and the words after it must not be empty.
SPEAKER_RE = re.compile(r"^([^\W\d_][\w'. -]{0,40}?):\s+(\S.*)$")


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
    # The film's look, in every shot alike -- the same words the storyboard
    # frames were drawn with -- so the style holds across cuts.
    if str(shot.get("look") or "").strip():
        desc += f" Visual style: {str(shot['look']).strip().rstrip('.')}."
    seconds = shot.get("seconds") or 5
    lines = [f"integrated_multimodal_description: [Shot {index}] A {seconds:g}-second single take. {desc}"]
    dialogue = str(shot.get("dialogue") or "").strip()
    if dialogue:
        # A line written "Name: words" (a script's), Name one of the shot's
        # cast, is said by that name's speaker -- S1 for the first name in the
        # shot, S2 for the next -- with the name itself not spoken. Any other
        # line is S1's, as before.
        # Only a name of the shot's cast counts: "Look: the sea" is a line.
        cast = {part.split(":", 1)[0].strip().lower()
                for part in str(shot.get("characters") or "").split("; ") if ":" in part}
        ids: dict[str, int] = {}
        said = []
        for line in dialogue.splitlines():
            line = line.strip()
            if not line:
                continue
            m = SPEAKER_RE.match(line)
            if m and m.group(1).strip().lower() in cast:
                who = m.group(1).strip().lower()
                ids.setdefault(who, len(ids) + 1)
                said.append((min(ids[who], 4), m.group(2).strip()))
            else:
                said.append((1, line))
        spoken = " ".join(f"(S{n}) <d>[{language}] {words}</d>" for n, words in said if words)
        lines[0] += (" The character speaks" if len(ids) < 2 else " The characters speak") + \
            f" clearly with precise lip synchronization: {spoken}"
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
        if p.get("with_refs"):
            # Pictures, in order, as people/objects ("I"); the prompt says
            # which is which (manager._resolve). None left -- deleted since --
            # is the same model from text alone.
            refs = [str(x) for x in p.get("image_refs") or []]
            return {"model_type": REF_IMAGE_MODEL, "image_mode": 1, "resolution": size,
                    "prompt": p["prompt"], "seed": seed, "video_prompt_type": "I" if refs else "",
                    **({"image_refs": refs} if refs else {})}
        return {"model_type": IMAGE_MODEL, "image_mode": 1, "resolution": size,
                "prompt": p["prompt"], "seed": seed}
    if kind == "song":
        lyrics = str(p.get("lyrics") or "").strip() or "[Instrumental]"
        out = {"model_type": SONG_MODEL, "prompt": lyrics,
               "alt_prompt": str(p.get("style") or "").strip(),
               "duration_seconds": max(10, min(600, int(p.get("seconds") or 60))),
               "seed": seed, "custom_settings": {"language": str(p.get("language") or "es")},
               "override_profile": AUDIO_PROFILE}
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
                "duration_seconds": max(5, min(380, int(p.get("seconds") or 30))), "seed": seed,
                "override_profile": AUDIO_PROFILE}
    if kind == "voice":
        if not p.get("voice_file"):
            raise RecipeError("a cloned voice needs a sample of that voice")
        if not str(p.get("text") or "").strip():
            raise RecipeError("nothing to say")
        return {"model_type": VOICE_MODEL, "prompt": p["text"], "audio_prompt_type": "A",
                "audio_guide": p["voice_file"], "alt_prompt": str(p.get("voice_text") or ""),
                "duration_seconds": max(5, min(300, int(p.get("seconds") or 60))), "seed": seed,
                "override_profile": AUDIO_PROFILE}
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
    if kind == "eyes":
        return "mirada a cámara"
    if kind == "portrait":
        return "retrato de un personaje"
    if kind == "repaint":
        return f"rehacer {max(1, round(float(p.get('end') or 0) - float(p.get('start') or 0)))} s de una canción"
    return "imagen"


def shots_needed(seconds: float) -> int:
    """How many shots a scene of *seconds* takes at H3's best length."""
    return max(1, math.ceil(seconds / 15))
