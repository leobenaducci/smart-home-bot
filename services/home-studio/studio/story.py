"""An audio story put together: its voices and songs one after another, its
instrumentals as music under what follows (docs/home-studio.md).

`plan` is the whole decision and touches no file, so it is tested as it is;
`media.story_mix` plays what it decides.
"""
from __future__ import annotations

GAP = 0.6       # silence between one line and the next
LEAD = 2.0      # music alone before the first line it plays under
TAIL = 2.5      # music after the last line, fading
BED = 0.22      # music under the voices
MUSIC = 0.9     # music on its own (an interlude)
FADE = 1.5


def plan(items: list[tuple[str, str, float]]) -> tuple[list[dict], float]:
    """*items* in the story's order as (kind, file, seconds). Placements --
    {file, start, length, volume, loop, fade_in, fade_out} -- and the length
    of the whole.

    A voice or a song plays whole, after what came before. An instrumental
    followed by a voice before the next piece of music is laid under those
    voices -- looped if it is shorter, faded out when the next music starts or
    the story ends; one followed by nothing it could go under plays on its
    own, whole. A song ends the music under it: it is music itself."""
    out: list[dict] = []
    pos, bed = 0.0, None

    def close(end: float) -> None:
        nonlocal bed
        if bed is not None:
            out.append({**bed, "length": round(max(0.5, end - bed["start"]), 3), "volume": BED,
                        "loop": True, "fade_in": 1.0, "fade_out": FADE})
            bed = None

    for i, (kind, file, seconds) in enumerate(items):
        seconds = max(0.0, float(seconds or 0))
        if seconds <= 0:
            continue
        if kind == "instrumental":
            rest = items[i + 1:]
            nxt = next((k for k, (kk, _f, _s) in enumerate(rest) if kk in ("instrumental", "song")), len(rest))
            close(pos + 1.0)
            if any(kk == "voice" for kk, _f, _s in rest[:nxt]):
                bed = {"file": file, "start": round(pos, 3)}
                pos += LEAD
            else:
                out.append({"file": file, "start": round(pos, 3), "length": round(seconds, 3), "volume": MUSIC,
                            "loop": False, "fade_in": 0.3, "fade_out": min(FADE, seconds / 3)})
                pos += seconds + GAP
            continue
        if kind == "song":
            close(pos + 1.0)
        out.append({"file": file, "start": round(pos, 3), "length": round(seconds, 3), "volume": 1.0,
                    "loop": False, "fade_in": 0.0, "fade_out": 0.0})
        pos += seconds + GAP
    if bed is not None:
        close(max(pos - GAP, bed["start"]) + TAIL)
    total = max((p["start"] + p["length"] for p in out), default=0.0)
    return out, round(total, 3)
