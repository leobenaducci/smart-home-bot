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


def encode_recording(src: Path, out: Path) -> Path:
    """A recording made in the page (WebM from the browser, variable frame
    rate) as a kept clip: H.265 at a constant 30 fps with AAC sound."""
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(["-fflags", "+genpts", "-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-r", "30",
          *X265, "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)], timeout=7200)
    if probe(out)["seconds"] <= 0:
        raise MediaError("the recording could not be read")
    return out


def encode_camera(src: Path, out: Path, offset: float, seconds: float) -> Path:
    """The camera, recorded beside the screen as a track of its own, kept as a
    clip that lines up with the screen's frame for frame: *offset* is how much
    later than the screen it started (negative: earlier), so it is padded or
    cut by that much, and it is as long as the screen's clip. No sound -- the
    microphone is on the screen's track. Kept apart so whether it is shown,
    and in which corner, can be decided after (`stitch`'s `pips`)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    pre = ["-ss", f"{-offset:.3f}"] if offset < 0 else []
    pad = f"tpad=start_duration={offset:.3f}:start_mode=clone," if offset > 0 else ""
    _run(["-fflags", "+genpts", *pre, "-i", str(src), "-map", "0:v:0", "-an",
          "-vf", f"{pad}scale='min(1280,iw)':-2,fps=30", "-t", f"{seconds:.3f}",
          *X265, "-movflags", "+faststart", str(out)], timeout=7200)
    if probe(out)["seconds"] <= 0:
        raise MediaError("the camera could not be read")
    return out


def encode_sound(src: Path, out: Path, offset: float, seconds: float) -> Path:
    """The computer's sound, recorded beside the screen as a track of its own,
    kept lined up with the screen's clip (*offset*: how much later it started;
    negative, earlier) and as long as it: AAC, stereo. Apart from the
    microphone, so each can be turned up, down or off afterwards."""
    out.parent.mkdir(parents=True, exist_ok=True)
    pre = ["-ss", f"{-offset:.3f}"] if offset < 0 else []
    delay = f"adelay=delays={int(offset * 1000)}:all=1," if offset > 0 else ""
    _run(["-fflags", "+genpts", *pre, "-i", str(src), "-map", "0:a:0", "-vn",
          "-af", f"{delay}aresample=48000,apad", "-t", f"{seconds:.3f}",
          "-c:a", "aac", "-b:a", "160k", "-ac", "2", str(out)], timeout=7200)
    if probe(out)["seconds"] <= 0:
        raise MediaError("the computer sound could not be read")
    return out


def silences(src: Path, noise_db: float = -35.0, min_s: float = 1.2) -> list[tuple[float, float]]:
    """Stretches of *src* quieter than *noise_db* for at least *min_s*."""
    done = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(src), "-vn",
                           "-af", f"silencedetect=noise={noise_db}dB:d={min_s}", "-f", "null", "-"],
                          capture_output=True, text=True, timeout=1800)
    out, start = [], None
    for line in (done.stderr or "").splitlines():
        if "silence_start:" in line:
            start = float(line.split("silence_start:")[1].split()[0])
        elif "silence_end:" in line and start is not None:
            out.append((max(0.0, start), float(line.split("silence_end:")[1].split()[0])))
            start = None
    if start is not None:
        out.append((start, probe(src)["seconds"]))
    return out


def speaking_spans(src: Path, noise_db: float = -35.0, min_s: float = 1.2,
                   keep: float = 0.3) -> tuple[list[tuple[float, float]], float]:
    """The stretches of *src* to keep when its long silences are taken out --
    `keep` seconds of each silence left either side so a word is never
    clipped -- and its whole length."""
    total = probe(src)["seconds"]
    gaps = [(a + keep, b - keep) for a, b in silences(src, noise_db, min_s) if b - a - 2 * keep > 0.2]
    if not gaps:
        raise MediaError("no silence long enough to trim")
    spans, t = [], 0.0
    for a, b in gaps:
        if a > t:
            spans.append((t, a))
        t = b
    if total - t > 0.05:
        spans.append((t, total))
    return spans, total


def cut_spans(src: Path, out: Path, spans: list[tuple[float, float]], sound: bool = True,
              picture: bool = True) -> Path:
    """*src* with only *spans* kept, joined. The same spans on a recording's
    camera (no sound) and computer sound (no picture) keep them with the
    screen."""
    expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in spans)
    out.parent.mkdir(parents=True, exist_ok=True)
    audio = (["-af", f"aselect='{expr}',asetpts=N/SR/TB", "-c:a", "aac", "-b:a", "160k"] if sound else ["-an"])
    video = (["-vf", f"select='{expr}',setpts=N/FRAME_RATE/TB", *X265] if picture else ["-vn"])
    _run(["-i", str(src), *video, *audio, *(["-movflags", "+faststart"] if picture else []), str(out)],
         timeout=7200)
    return out


def cut_silences(src: Path, out: Path, noise_db: float = -35.0, min_s: float = 1.2,
                 keep: float = 0.3) -> tuple[Path, float]:
    """*src* without its long silences, `keep` seconds of each left either side
    so a word is never clipped. Returns the new clip and the seconds removed."""
    spans, total = speaking_spans(src, noise_db, min_s, keep)
    cut_spans(src, out, spans)
    return out, round(total - sum(b - a for a, b in spans), 2)


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


def cut_audio(src: Path, out: Path, start: float, seconds: float) -> Path:
    """*seconds* of *src* from *start*, same format."""
    _run(["-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(src), "-c", "copy", str(out)], timeout=300)
    return out


def srt(segments: list[dict]) -> str:
    """Subtitles in SubRip form from timed segments ({start, end, text})."""
    def stamp(t: float) -> str:
        ms = int(round(max(0.0, t) * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    blocks = []
    for n, seg in enumerate((s for s in segments if str(s.get("text") or "").strip()), 1):
        blocks.append(f"{n}\n{stamp(float(seg['start']))} --> {stamp(float(seg['end']))}\n{str(seg['text']).strip()}\n")
    return "\n".join(blocks)


def shift_srt(src: Path, out: Path, start: float, end: float) -> Path:
    """The subtitles of a stretch of a clip, timed from its start: the lines
    inside [start, end], moved back by `start` and cut to the stretch."""
    def secs(stamp: str) -> float:
        hms, ms = stamp.strip().split(",")
        h, m_, s_ = hms.split(":")
        return int(h) * 3600 + int(m_) * 60 + int(s_) + int(ms) / 1000
    lines = []
    for block in src.read_text(encoding="utf-8").strip().split("\n\n"):
        rows = block.strip().splitlines()
        if len(rows) < 3 or "-->" not in rows[1]:
            continue
        a, b = (secs(x) for x in rows[1].split("-->"))
        if b <= start or a >= end:
            continue
        lines.append({"start": max(0.0, a - start), "end": min(end, b) - start, "text": " ".join(rows[2:])})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(srt(lines), encoding="utf-8")
    return out


def mix_audio(srcs: list[Path], out: Path) -> Path:
    """Several stems of one song summed back into one track, as MP3 -- the
    song without a part (a minus-one to play along with), or the part alone.
    Summed, not averaged: the stems are pieces of one mix and add back to it."""
    if not srcs:
        raise MediaError("nothing to mix")
    out.parent.mkdir(parents=True, exist_ok=True)
    args = [a for s in srcs for a in ("-i", str(s))]
    graph = ("".join(f"[{i}:a]" for i in range(len(srcs))) + f"amix=inputs={len(srcs)}:normalize=0[m]") \
        if len(srcs) > 1 else "[0:a]anull[m]"
    _run([*args, "-filter_complex", graph, "-map", "[m]", "-c:a", "libmp3lame", "-q:a", "2", str(out)], timeout=600)
    if probe(out)["seconds"] <= 0:
        out.unlink(missing_ok=True)
        raise MediaError(f"{out.name} did not mix")
    return out


def to_wav(src: Path, out: Path, rate: int = 44100, channels: int = 2) -> Path:
    """*src* as a PCM WAV at *rate* and *channels*."""
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(["-i", str(src), "-vn", "-ac", str(channels), "-ar", str(rate), "-c:a", "pcm_s16le", str(out)], timeout=300)
    return out


def stitch(videos: list[Path], out: Path, crossfade: float = 0.0,
           lengths: list[float | None] | None = None, marks: list[Path | None] | None = None,
           fast: bool = False, subs: list[Path | None] | None = None,
           pips: list[dict | None] | None = None, sounds: list[dict | None] | None = None,
           starts: list[float | None] | None = None) -> Path:
    """One film from shots, in order, video and sound.

    Re-encoded through the concat filter rather than the concat demuxer: the
    shots come from one model but a retake can differ in a stream parameter,
    and the demuxer then produces a file that plays wrong without failing.
    A crossfade blends the join (video xfade, audio acrossfade).

    `pips`: a recording's camera, drawn in a corner of its clip -- `{"file",
    "corner": tl|tr|bl|br, "size": share of the width}` -- or None. The camera
    is kept as a track of its own (`encode_camera`), lined up with its clip,
    so whether and where it shows is decided here, at render time.

    `sounds`: a recording's volumes and computer sound -- `{"own": volume of
    the clip's own sound (the microphone), "file": the computer sound, or
    absent, "volume": its volume}` -- or None for the clip as it is.

    `starts`: where in each clip to begin, for a section of a recording (with
    `lengths` saying how much of it); the camera and the computer sound of a
    clip begin at the same place, so they stay with it.
    """
    if not videos:
        raise MediaError("nothing to stitch")
    out.parent.mkdir(parents=True, exist_ok=True)
    infos = [probe(v) for v in videos]
    # A shot planned to a cut on the music was generated a little long: it is
    # read only up to its cut, so the film lands on the song's beats and ends
    # with it. `-t` before the input limits what is read of it.
    lengths = list(lengths or [None] * len(videos))
    starts = list(starts or [None] * len(videos))
    args: list[str] = []
    for v, info, keep, begin in zip(videos, infos, lengths, starts):
        if begin:
            # Accurate, not to the keyframe: the clip is re-encoded anyway.
            args += ["-ss", f"{begin:.3f}"]
            info["seconds"] = max(0.04, info["seconds"] - begin)
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
    pips = list(pips or [None] * len(videos))
    pip_input = {}
    for i, pip in enumerate(pips):
        if pip and pip.get("file"):
            pip_input[i] = len(videos) + len(mark_input) + len(pip_input)
            args += (["-ss", f"{starts[i]:.3f}"] if starts[i] else []) + ["-i", str(pip["file"])]
    sounds = list(sounds or [None] * len(videos))
    sound_input = {}
    for i, snd in enumerate(sounds):
        if snd and snd.get("file"):
            sound_input[i] = len(videos) + len(mark_input) + len(pip_input) + len(sound_input)
            args += (["-ss", f"{starts[i]:.3f}"] if starts[i] else []) + ["-i", str(snd["file"])]
    parts, n = [], len(videos)
    for i, info in enumerate(infos):
        label = f"b{i}" if i in mark_input else f"v{i}"
        # A clip's subtitles, burnt in after it is sized, so the text is the
        # same size in every clip. Paths here are the studio's own (letters,
        # digits, `/_-.`), so nothing in them needs the filter's escaping.
        sub = (subs or [None] * len(videos))[i] if subs else None
        burn = (f",subtitles=filename={sub}:force_style='FontName=DejaVu Sans,FontSize=18,"
                f"Outline=2,Shadow=0,MarginV=22'") if sub else ""
        sized = (f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                 f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24")
        if i in pip_input:
            # The camera over the clip, before the subtitles so they are not
            # under it: sized to its share of the width, a margin from the
            # edges, and cut to the clip's length so the clip ends on time.
            pip = pips[i]
            pw = max(64, int(w * min(0.5, max(0.1, float(pip.get("size") or 0.28))) / 2) * 2)
            m = max(8, int(w * 0.02))
            corner = pip.get("corner") if pip.get("corner") in ("tl", "tr", "bl", "br") else "br"
            x = f"{m}" if corner in ("tl", "bl") else f"W-w-{m}"
            y = f"{m}" if corner in ("tl", "tr") else f"H-h-{m}"
            parts.append(sized + f"[s{i}]")
            parts.append(f"[{pip_input[i]}:v]trim=duration={info['seconds']:.3f},setpts=PTS-STARTPTS,"
                         f"scale={pw}:-2,setsar=1,fps=24[k{i}]")
            parts.append(f"[s{i}][k{i}]overlay=x={x}:y={y}:eof_action=pass{burn},format=yuv420p[{label}]")
        else:
            parts.append(sized + f"{burn},format=yuv420p[{label}]")
        if i in mark_input:
            parts.append(f"[{mark_input[i]}:v]scale={w}:{h},format=rgba[m{i}]")
            parts.append(f"[b{i}][m{i}]overlay=0:0:shortest=1,format=yuv420p[v{i}]")
        snd = sounds[i] or {}
        own = f",volume={float(snd['own']):.2f}" if "own" in snd else ""
        mine = f"a{i}" if i not in sound_input else f"o{i}"
        if info["has_audio"]:
            parts.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo{own}[{mine}]")
        else:
            parts.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{info['seconds']:.3f}[{mine}]")
        if i in sound_input:
            # The computer's sound under the microphone, each at its own
            # volume, cut to the clip's length.
            parts.append(f"[{sound_input[i]}:a]atrim=duration={info['seconds']:.3f},asetpts=PTS-STARTPTS,"
                         f"aresample=48000,aformat=channel_layouts=stereo,"
                         f"volume={float(snd.get('volume', 1.0)):.2f}[p{i}]")
            parts.append(f"[o{i}][p{i}]amix=inputs=2:duration=first:normalize=0[a{i}]")
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


def still(image: Path, seconds: float, size: tuple[int, int], out: Path) -> Path:
    """A picture held for *seconds* as a silent clip at *size* -- a storyboard
    frame standing in for its shot until the shot is made."""
    w, h = size
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(["-loop", "1", "-framerate", "24", "-i", str(image), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
          "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,format=yuv420p",
          "-t", f"{seconds:.4f}", "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-shortest", str(out)])
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
