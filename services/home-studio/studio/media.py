"""The studio's ffmpeg: frames, stitching, mixing -- on the CPU.

None of this waits in the card's queue. A frame for continuity is taken the
moment a shot finishes; a film is stitched while the card is busy with the
next person's picture.

Every function raises MediaError with ffmpeg's own last lines, because "the
render failed" is not something anyone can act on and "Invalid data found
when processing input" is.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


class MediaError(RuntimeError):
    pass


# How every video this studio keeps is encoded: H.265, about half the size of
# the H.264 the generator writes at the same look. On the CPU on purpose -- the
# card belongs to the generator, and a five-second shot takes seconds here.
# `hvc1` is the tag Safari and iOS insist on; without it they show nothing.
# The catch, and the reason it is one constant: browsers without HEVC decoding
# (Firefox, most Linux desktops) cannot play these. Phones and the app can.
X265 = ["-c:v", "libx265", "-preset", "medium", "-crf", "23", "-tag:v", "hvc1",
        "-pix_fmt", "yuv420p", "-x265-params", "log-level=error"]


def _run(args: list[str], timeout: int = 1800) -> str:
    done = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        raise MediaError("\n".join((done.stderr or "").strip().splitlines()[-4:]) or "ffmpeg failed")
    return done.stdout


def probe(path: Path) -> dict:
    """{seconds, width, height, has_audio} of a media file."""
    done = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                           "-show_streams", str(path)], capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise MediaError(f"cannot read {Path(path).name}")
    info = json.loads(done.stdout or "{}")
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    return {"seconds": float((info.get("format") or {}).get("duration") or 0),
            "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
            "codec": str(video.get("codec_name") or ""),
            "has_audio": any(s.get("codec_type") == "audio" for s in info.get("streams", []))}


def frame(video: Path, out: Path, which: str = "last") -> Path:
    """The first or the last frame of *video*, as a PNG.

    The last frame is what the next shot starts from; the first frame of the
    shot after an edited one is what the edit must end on. Taken from the
    decoded stream, not by seeking to the duration, which lands a frame short
    on some files and gives a black one on others.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    if which == "first":
        _run(["-i", str(video), "-frames:v", "1", str(out)])
    else:
        _run(["-sseof", "-1", "-i", str(video), "-update", "1", "-frames:v", "10000", str(out)])
    if not out.is_file():
        raise MediaError(f"no {which} frame in {Path(video).name}")
    return out


# What the generators write for songs and voices, and what is kept instead.
LOSSLESS_AUDIO = (".wav", ".flac")


def compress_audio(src: Path) -> Path:
    """A song or a voice as MP3 beside *src*, which is then removed.

    ACE-Step and the voice models write WAV: ~10 MB a minute, on a disk that
    keeps every take ever made. VBR at ~190 kbps is a sixth of that and not
    something a phone speaker or a film's soundtrack can tell apart -- and MP3
    plays and downloads everywhere, which Opus still does not quite. The WAV
    is removed only once the MP3 reads back with a duration.
    """
    src = Path(src)
    out = src.with_suffix(".mp3")
    _run(["-i", str(src), "-vn", "-c:a", "libmp3lame", "-q:a", "2", str(out)], timeout=600)
    if probe(out)["seconds"] <= 0:
        out.unlink(missing_ok=True)
        raise MediaError(f"{src.name} did not compress")
    src.unlink()
    return out


def compress_video(src: Path) -> Path:
    """*src* re-encoded to H.265 in place: same name, same sound, so nothing
    that points at it has to change. Replaced only once the new file reads back
    with a picture; one that already is HEVC is left alone."""
    src = Path(src)
    info = probe(src)
    if info["codec"] == "hevc":
        return src
    tmp = src.with_name(src.stem + ".x265" + src.suffix)
    _run(["-i", str(src), "-map", "0:v:0", "-map", "0:a?", *X265, "-c:a", "copy",
          "-movflags", "+faststart", str(tmp)], timeout=1800)
    if probe(tmp)["codec"] != "hevc":
        tmp.unlink(missing_ok=True)
        raise MediaError(f"{src.name} did not compress")
    tmp.replace(src)
    return src


def for_generator(src: Path, out: Path) -> Path:
    """An H.264 copy of *src* for the generator to read. What the studio keeps
    is H.265, and whether every reader inside WanGP decodes that is not
    something worth finding out on somebody's retouch."""
    if probe(src)["codec"] != "hevc":
        return Path(src)
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(["-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
          "-crf", "14", "-pix_fmt", "yuv420p", "-c:a", "copy", str(out)])
    return out


def stitch(videos: list[Path], out: Path, crossfade: float = 0.0) -> Path:
    """One film from shots, in order, video and sound.

    Re-encoded through the concat filter rather than the concat demuxer: the
    shots come from one model but a retake can differ in a stream parameter,
    and the demuxer then produces a file that plays wrong without failing.
    A crossfade blends the join (video xfade, audio acrossfade).
    """
    if not videos:
        raise MediaError("nothing to stitch")
    out.parent.mkdir(parents=True, exist_ok=True)
    infos = [probe(v) for v in videos]
    w, h = infos[0]["width"] or 832, infos[0]["height"] or 480
    args: list[str] = []
    for v in videos:
        args += ["-i", str(v)]
    parts, n = [], len(videos)
    for i, info in enumerate(infos):
        parts.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,format=yuv420p[v{i}]")
        if info["has_audio"]:
            parts.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        else:
            parts.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{info['seconds']:.3f}[a{i}]")
    if crossfade > 0 and n > 1:
        cf = min(crossfade, min(i["seconds"] for i in infos) / 2)
        vlast, alast, offset = "v0", "a0", infos[0]["seconds"] - cf
        for i in range(1, n):
            parts.append(f"[{vlast}][v{i}]xfade=transition=fade:duration={cf:.3f}:offset={offset:.3f}[vx{i}]")
            parts.append(f"[{alast}][a{i}]acrossfade=d={cf:.3f}[ax{i}]")
            vlast, alast = f"vx{i}", f"ax{i}"
            offset += infos[i]["seconds"] - cf
        maps = [f"[{vlast}]", f"[{alast}]"]
    else:
        parts.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]")
        maps = ["[v]", "[a]"]
    _run([*args, "-filter_complex", ";".join(parts), "-map", maps[0], "-map", maps[1],
          *X265, "-c:a", "aac", "-b:a", "192k",
          "-movflags", "+faststart", str(out)])
    return out


def mix(video: Path, tracks: list[dict], out: Path) -> Path:
    """Audio tracks under a film: each {file, start, volume}. The film's own
    sound stays; a song or a narration is laid over it from *start*."""
    if not tracks:
        raise MediaError("no track to mix")
    info = probe(video)
    args = ["-i", str(video)]
    parts, labels = [], []
    if info["has_audio"]:
        parts.append("[0:a]aresample=48000[a0]")
        labels.append("[a0]")
    for i, t in enumerate(tracks, 1):
        args += ["-i", str(t["file"])]
        delay = int(max(0.0, float(t.get("start") or 0)) * 1000)
        vol = max(0.0, min(2.0, float(t.get("volume") if t.get("volume") is not None else 1.0)))
        parts.append(f"[{i}:a]aresample=48000,volume={vol:.2f},adelay={delay}|{delay}[a{i}]")
        labels.append(f"[a{i}]")
    parts.append("".join(labels) + f"amix=inputs={len(labels)}:duration=first:normalize=0[mix]")
    _run([*args, "-filter_complex", ";".join(parts), "-map", "0:v", "-map", "[mix]",
          "-c:v", "copy", *(["-tag:v", "hvc1"] if info["codec"] == "hevc" else []),
          "-c:a", "aac", "-b:a", "192k", "-t", f"{info['seconds']:.3f}",
          "-movflags", "+faststart", str(out)])
    return out


def thumbnail(src: Path, out: Path, width: int = 320) -> Path:
    """A small JPEG of a picture or a video's first frame, for lists."""
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(["-i", str(src), "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(out)])
    return out
