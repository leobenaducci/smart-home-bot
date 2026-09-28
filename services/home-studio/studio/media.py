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
# A draft, not something kept: the preview's download. H.264 at a fast
# preset takes seconds where H.265 takes a minute, plays in every browser,
# and its larger file costs nothing for a file that is looked at and dropped.
FAST = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]


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


def to_wav(src: Path, out: Path, rate: int = 44100, channels: int = 2) -> Path:
    """*src* as a PCM WAV at *rate* and *channels*."""
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(["-i", str(src), "-vn", "-ac", str(channels), "-ar", str(rate), "-c:a", "pcm_s16le", str(out)], timeout=300)
    return out


def stitch(videos: list[Path], out: Path, crossfade: float = 0.0,
           lengths: list[float | None] | None = None, marks: list[Path | None] | None = None,
           fast: bool = False) -> Path:
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
    # A shot planned to a cut on the music was generated a little long: it is
    # read only up to its cut, so the film lands on the song's beats and ends
    # with it. `-t` before the input limits what is read of it.
    lengths = list(lengths or [None] * len(videos))
    args: list[str] = []
    for v, info, keep in zip(videos, infos, lengths):
        if keep and keep < info["seconds"] - 0.5 / 24:
            # A quarter frame short of the cut: `-t` keeps every frame that
            # starts before it, and the frame that starts *on* the cut is the
            # next shot's.
            args += ["-t", f"{keep - 0.25 / 24:.4f}"]
            info["seconds"] = keep
        args += ["-i", str(v)]
    w, h = infos[0]["width"] or 832, infos[0]["height"] or 480
    # A transparent picture laid over a shot (a preview's watermark), each an
    # input of its own after the shots, looped for as long as its shot lasts.
    marks = list(marks or [None] * len(videos))
    mark_input = {}
    for i, mark in enumerate(marks):
        if mark:
            mark_input[i] = len(videos) + len(mark_input)
            args += ["-loop", "1", "-framerate", "24", "-i", str(mark)]
    parts, n = [], len(videos)
    for i, info in enumerate(infos):
        label = f"b{i}" if i in mark_input else f"v{i}"
        parts.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,format=yuv420p[{label}]")
        if i in mark_input:
            parts.append(f"[{mark_input[i]}:v]scale={w}:{h},format=rgba[m{i}]")
            parts.append(f"[b{i}][m{i}]overlay=0:0:shortest=1,format=yuv420p[v{i}]")
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
          *(FAST if fast else X265), "-c:a", "aac", "-b:a", "192k",
          "-movflags", "+faststart", str(out)])
    return out


FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")


def placeholder(title: str, text: str, seconds: float, size: tuple[int, int], out: Path) -> Path:
    """A still, silent clip standing in for a shot not made yet: its title and
    its description on a dark card, for as long as the shot will last -- what
    the page's preview shows in its place, so a preview downloaded half-way
    keeps the song running under the whole video."""
    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415 -- only this needs it
    w, h = size
    img = Image.new("RGB", (w, h), (34, 34, 34))
    draw = ImageDraw.Draw(img)

    def font(name: str, px: int):
        try:
            return ImageFont.truetype(str(FONT_DIR / name), px)
        except OSError:
            return ImageFont.load_default()
    big, small = font("DejaVuSans-Bold.ttf", max(14, h // 14)), font("DejaVuSans.ttf", max(12, h // 24))
    margin, y = w // 12, h // 5
    draw.text((margin, y), title, font=big, fill=(236, 236, 236))
    y += int(h / 14 * 1.8)
    line, lines = "", []
    for word in (text or "").split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=small) > w - 2 * margin and line:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    for text_line in lines[: max(1, (h - y - margin) // int(h / 24 * 1.4))]:
        draw.text((margin, y), text_line, font=small, fill=(190, 190, 190))
        y += int(h / 24 * 1.4)
    still = out.with_suffix(".png")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(still)
    _run(["-loop", "1", "-framerate", "24", "-i", str(still), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
          "-t", f"{seconds:.4f}", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-shortest", str(out)])
    still.unlink(missing_ok=True)
    return out


def watermark(badge: str, info: str, size: tuple[int, int], out: Path) -> Path:
    """A transparent overlay marking a frame as a preview: `badge` in a corner,
    `info` (the shot, its time in the song, its version) along the bottom.
    Drawn here rather than by ffmpeg's drawtext, whose text needs escaping
    for every colon and quote a shot's description might hold."""
    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415
    w, h = size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    def font(name: str, px: int):
        try:
            return ImageFont.truetype(str(FONT_DIR / name), px)
        except OSError:
            return ImageFont.load_default()
    bold, plain = font("DejaVuSans-Bold.ttf", max(12, h // 22)), font("DejaVuSans.ttf", max(11, h // 26))
    pad = max(6, h // 60)
    bw = int(draw.textlength(badge, font=bold)) + 2 * pad
    bh = max(12, h // 22) + 2 * pad
    draw.rectangle([pad, pad, pad + bw, pad + bh], fill=(198, 137, 43, 215))
    draw.text((2 * pad, int(1.6 * pad)), badge, font=bold, fill=(255, 255, 255, 255))
    strip = max(11, h // 26) + 2 * pad
    draw.rectangle([0, h - strip - pad, w, h], fill=(0, 0, 0, 150))
    draw.text((2 * pad, h - strip), info, font=plain, fill=(240, 240, 240, 255))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def follow(song: Path, segments: list[tuple[float, float]], out: Path, delay: float = 0.0) -> Path:
    """The song cut to a film's shots: for each (position, length), the song
    from that position in the whole video for that long, one after another.

    A film of the shots made so far skips the ones that are not: laying the
    song from 0 under it put every shot after a gap out of time with the
    words it was made for. `delay` is the song starting that late into the
    video; before it, and past the song's end, is silence.
    """
    if not segments:
        raise MediaError("no shots to follow")
    out.parent.mkdir(parents=True, exist_ok=True)
    n = len(segments)
    parts = [f"[0:a]aresample=48000,aformat=channel_layouts=stereo,asplit={n}" + "".join(f"[i{k}]" for k in range(n))]
    for k, (pos, length) in enumerate(segments):
        a = pos - delay
        tail = f"apad=whole_dur={length:.4f},atrim=duration={length:.4f},asetpts=PTS-STARTPTS[s{k}]"
        if a >= 0:
            parts.append(f"[i{k}]atrim=start={a:.4f}:duration={length:.4f},asetpts=PTS-STARTPTS,{tail}")
        elif a + length <= 0:
            parts.append(f"[i{k}]atrim=duration=0.001,volume=0,{tail}")
        else:
            ms = int(round(-a * 1000))
            parts.append(f"[i{k}]atrim=start=0:duration={length + a:.4f},asetpts=PTS-STARTPTS,"
                         f"adelay={ms}|{ms},{tail}")
    parts.append("".join(f"[s{k}]" for k in range(n)) + f"concat=n={n}:v=0:a=1[out]")
    _run(["-i", str(song), "-filter_complex", ";".join(parts), "-map", "[out]",
          "-c:a", "pcm_s16le", str(out)])
    return out


def mix(video: Path, tracks: list[dict], out: Path, keep_own: bool = True) -> Path:
    """Audio tracks under a film: each {file, start, volume}. The film's own
    sound stays unless *keep_own* is false -- a music video wants the song and
    not the sound each shot was generated with."""
    if not tracks:
        raise MediaError("no track to mix")
    info = probe(video)
    args = ["-i", str(video)]
    parts, labels = [], []
    if info["has_audio"] and keep_own:
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
