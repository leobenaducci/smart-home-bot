"""A page being written, seen by the person writing it -- drawn, not generated.

An explainer's point can be its words and formulas appearing on a sheet of
paper as if handwritten, a pen at the tip of the line. The picture model cannot
do this: it draws text that looks like writing and reads as nothing, and a
formula has to be exactly right. So this is ordinary drawing on the CPU --
matplotlib's mathtext for the formulas (no TeX needed), a handwriting font for
the words -- revealed left to right, line by line, over the point's narration.

A line is plain text, with any formula between `$...$` in mathtext's LaTeX
subset (`$x^2 + 2$`, `$\\frac{7-2x}{5}$`, `$f^{-1}(x)$`). A formula mathtext
cannot read is written as the text it was, never dropped.
"""
from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path

from .media import MediaError
from .projects import clean_write

FPS = 24
# The hand: Comic Neue (Ubuntu's fonts-comic-neue, in the Dockerfile). Not
# Humor Sans, matplotlib's own xkcd hand: it has no accents, so "función" lost
# its ó, and no lower case, so f(x) was written F(x). DejaVu last, which every
# image has, so a missing font changes the look and never fails the film.
FONTS = ("/usr/share/fonts/opentype/comic-neue/ComicNeue-Bold.otf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
DESK = (86, 64, 46)
PAPER = (250, 247, 238)
RULE = (178, 202, 228)
MARGIN_RULE = (226, 128, 128)
INK = (24, 42, 112)
# A full line across the page in about this long: slower reads as a stall,
# faster as a printer.
LINE_SECONDS = 3.2
# Moving the pen back to the start of the next line.
RETURN_SECONDS = 0.35


def _font_path() -> str:
    return next((f for f in FONTS if Path(f).is_file()), FONTS[-1])


# LaTeX a writer reaches for that mathtext spells differently or not at all.
_MATH_SWAPS = (("\\text{", "\\mathrm{"), ("\\textrm{", "\\mathrm{"), ("\\operatorname{", "\\mathrm{"),
               ("\\dfrac", "\\frac"), ("\\tfrac", "\\frac"), ("\\displaystyle", ""), ("\\,", "\\ "),
               ("\\left.", ""), ("\\right.", ""))


_LOOSE_MATH = re.compile(r"[^\s$]*(?:\^|_\{|\\[A-Za-z]+)[^\s$]*")


def _mathtext(text: str) -> str:
    """*text* with its formulas in what mathtext reads. Outside `$...$` the
    words are left alone; an odd `$` is dropped rather than left open."""
    parts = text.split("$")
    if len(parts) % 2 == 0:
        parts = [text.replace("$", "")]
    for i in range(0, len(parts), 2):
        # Notation left among the words (`Dom f^{-1}`) is a formula all the same.
        parts[i] = _LOOSE_MATH.sub(lambda m: f"${m.group(0)}$", parts[i])
    for i in range(1, len(parts), 2):
        for a, b in _MATH_SWAPS:
            parts[i] = parts[i].replace(a, b)
    return "$".join(parts)


def _render_line(text: str, px: int, font: str):
    """The line as an RGBA array cropped to its ink: alpha only, colour later."""
    import numpy as np  # noqa: PLC0415
    from matplotlib import font_manager  # noqa: PLC0415
    from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: PLC0415
    from matplotlib.figure import Figure  # noqa: PLC0415
    import matplotlib  # noqa: PLC0415

    font_manager.fontManager.addfont(font)
    family = font_manager.FontProperties(fname=font).get_name()

    def draw(s: str):
        fig = Figure(figsize=(40, 4), dpi=100)
        FigureCanvasAgg(fig)
        fig.patch.set_alpha(0)
        with matplotlib.rc_context({"mathtext.fontset": "custom", "mathtext.rm": family,
                                    "mathtext.it": family, "mathtext.bf": family,
                                    "mathtext.fallback": "stixsans"}):
            fig.text(0.005, 0.5, s, fontsize=px * 72 / 100, family=family, va="center", color="black")
            fig.canvas.draw()
            return np.asarray(fig.canvas.buffer_rgba())[:, :, 3].copy()

    try:
        alpha = draw(_mathtext(text))
    except Exception:                                      # noqa: BLE001 -- mathtext's parse errors vary
        alpha = draw(text.replace("$", "").replace("\\", ""))
    rows, cols = np.where(alpha > 8)
    if not len(rows):
        return None
    return alpha[rows.min(): rows.max() + 1, cols.min(): cols.max() + 1]


def _pen(scale: float):
    """A pen held from the lower right, as the writer sees their own: RGBA,
    and where its tip is inside the sprite."""
    from PIL import Image, ImageDraw, ImageFilter  # noqa: PLC0415
    length, width = int(520 * scale), max(6, int(26 * scale))
    img = Image.new("RGBA", (length + 40, width * 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cy = width * 2
    tip, cone = 10, int(length * 0.09)
    d.polygon([(tip, cy), (tip + cone, cy - width // 2), (tip + cone, cy + width // 2)], fill=(196, 178, 140, 255))
    d.polygon([(tip, cy), (tip + cone // 3, cy - width // 6), (tip + cone // 3, cy + width // 6)], fill=(30, 30, 30, 255))
    d.rounded_rectangle([tip + cone, cy - width // 2, tip + length, cy + width // 2],
                        radius=width // 2, fill=(28, 52, 120, 255))
    d.rectangle([tip + cone, cy - width // 2, tip + cone + int(length * 0.05), cy + width // 2], fill=(180, 180, 190, 255))
    d.line([(tip + cone + 8, cy - width // 4), (tip + length - 8, cy - width // 4)], fill=(120, 150, 220, 160),
           width=max(1, width // 6))
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle([tip + cone + 18, cy + width // 2, tip + length + 30, cy + width + width // 2],
                         radius=width // 2, fill=(0, 0, 0, 70))
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(2, width // 2)))
    shadow.alpha_composite(img)
    angle = 34
    rotated = shadow.rotate(angle, expand=True, resample=Image.BICUBIC)
    # Where the tip went: rotate (tip, cy) about the old centre.
    ox, oy = shadow.size[0] / 2, shadow.size[1] / 2
    a = math.radians(angle)
    dx, dy = tip - ox, cy - oy
    tx = ox + dx * math.cos(a) + dy * math.sin(a) + (rotated.size[0] - shadow.size[0]) / 2
    ty = oy - dx * math.sin(a) + dy * math.cos(a) + (rotated.size[1] - shadow.size[1]) / 2
    return rotated, (int(round(tx)), int(round(ty)))


def page(lines: list[str], seconds: float, size: tuple[int, int], out: Path, lead: float = 0.4) -> Path:
    """A silent clip of *seconds*: a sheet on a desk, *lines* written onto it
    one after another from `lead` on, then held. The writing takes what a hand
    would, never more than the clip leaves; a short point is written briskly
    and a long one at the hand's own pace."""
    import numpy as np  # noqa: PLC0415
    from PIL import Image, ImageDraw  # noqa: PLC0415

    lines = clean_write(lines)
    w, h = size
    font = _font_path()
    # The sheet: from just below the top of the frame to past its bottom, the
    # way a page lies in front of you when you write on it.
    px0, px1, py0 = int(w * 0.11), int(w * 0.89), int(h * 0.06)
    rule = max(24, int(h * 0.094))
    margin = px0 + int((px1 - px0) * 0.1)
    base = Image.new("RGB", (w, h), DESK)
    d = ImageDraw.Draw(base)
    for i in range(0, h, 3):
        shade = int(8 * math.sin(i / 17.0) + 5 * math.sin(i / 5.3))
        d.line([(0, i), (w, i)], fill=tuple(max(0, min(255, c + shade)) for c in DESK))
    d.rectangle([px0 + 10, py0 + 12, px1 + 10, h], fill=(52, 38, 28))
    d.rectangle([px0, py0, px1, h], fill=PAPER)
    first_rule = py0 + int(rule * 1.6)
    y = first_rule
    while y < h:
        d.line([(px0, y), (px1, y)], fill=RULE, width=max(1, h // 540))
        y += rule
    d.line([(margin - 12, py0), (margin - 12, h)], fill=MARGIN_RULE, width=max(1, h // 400))
    paper = np.asarray(base, dtype=np.float32)

    # The ink, all of it, and where each line sits.
    ink_alpha = np.zeros((h, w), dtype=np.float32)
    spans = []                                             # (y0, y1, x0, x1) per line
    text_px = int(rule * 0.62)
    usable = px1 - margin - int((px1 - px0) * 0.05)
    for i, text in enumerate(lines):
        a = _render_line(text, text_px, font)
        if a is None:
            continue
        lh, lw = a.shape
        if lw > usable or lh > rule * 1.6:
            k = min(usable / lw, rule * 1.6 / lh)
            a = np.asarray(Image.fromarray(a).resize((max(1, int(lw * k)), max(1, int(lh * k))), Image.LANCZOS))
            lh, lw = a.shape
        line_y = first_rule + i * rule
        y1 = min(h, line_y - int(rule * 0.12))
        y0 = max(0, y1 - lh)
        if lh > rule * 0.9:                                 # a tall fraction: centre it on the line
            y0 = max(0, line_y - int(rule * 0.55) - lh // 2)
            y1 = min(h, y0 + lh)
        ink_alpha[y0:y1, margin: margin + lw] = a[: y1 - y0] / 255.0
        spans.append((y0, y1, margin, margin + lw))

    # When each line is written.
    writing = sum((x1 - x0) for _y0, _y1, x0, x1 in spans) / max(1, usable) * LINE_SECONDS
    returns = RETURN_SECONDS * max(0, len(spans) - 1)
    room = max(0.5, seconds - lead - 0.6 - returns)
    pace = min(1.0, room / writing) if writing else 1.0    # <1: written faster to fit
    timeline, t = [], lead
    for y0, y1, x0, x1 in spans:
        dur = (x1 - x0) / max(1, usable) * LINE_SECONDS * pace
        timeline.append((t, t + dur, (y0, y1, x0, x1)))
        t += dur + RETURN_SECONDS
    pen, (tip_x, tip_y) = _pen(h / 1080)
    pen_arr = np.asarray(pen, dtype=np.float32)
    pen_rgb, pen_a = pen_arr[:, :, :3], pen_arr[:, :, 3:] / 255.0
    ink = np.array(INK, dtype=np.float32)

    frames = max(1, int(round(seconds * FPS)))
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-",
           "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{seconds:.4f}",
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    # The page as written so far, kept as the bytes sent: each frame changes
    # only the strip the pen just crossed, so only that strip is mixed again.
    canvas = paper.astype(np.uint8)
    done_to = [x0 for _s, _e, (_y0, _y1, x0, _x1) in timeline]

    def reveal(i: int, y0: int, y1: int, upto: int) -> None:
        start = done_to[i]
        if upto <= start:
            return
        a = ink_alpha[y0:y1, start:upto, None]
        canvas[y0:y1, start:upto] = (paper[y0:y1, start:upto] * (1 - a) + ink * a).astype(np.uint8)
        done_to[i] = upto

    try:
        for f in range(frames):
            now = f / FPS
            pen_at, last_pen, last_end = None, None, 0.0
            for i, (start, end, (y0, y1, x0, x1)) in enumerate(timeline):
                mid = (y0 + y1) / 2
                if now >= end:
                    reveal(i, y0, y1, x1)
                    last_pen, last_end = (x1, mid), end
                    continue
                if now >= start:
                    edge = x0 + int((x1 - x0) * (now - start) / max(1e-6, end - start))
                    reveal(i, y0, y1, edge)
                    wob = math.sin(now * 38.0) * (y1 - y0) * 0.22 + math.sin(now * 11.0) * (y1 - y0) * 0.12
                    pen_at = (edge, mid + wob)
                elif last_pen is None:
                    # Before the first line: the pen waits just above it.
                    pen_at = (x0, mid - rule * 0.4)
                else:
                    # Between lines: back to where the next one starts.
                    k = max(0.0, min(1.0, 1 - (start - now) / RETURN_SECONDS))
                    pen_at = (last_pen[0] + (x0 - last_pen[0]) * k, last_pen[1] + (mid - last_pen[1]) * k)
                break
            else:
                # All written: the hand lifts away and leaves the page to be read.
                k = (now - last_end) / 0.7 if last_pen else 1.0
                if k < 1:
                    pen_at = (last_pen[0] + k * w * 0.35, last_pen[1] + k * h * 0.6)
            frame = canvas
            if pen_at is not None:
                frame = canvas.copy()
                pen_at = (int(pen_at[0]), int(pen_at[1]))
            if pen_at is not None:
                ox, oy = pen_at[0] - tip_x, pen_at[1] - tip_y
                sx0, sy0 = max(0, -ox), max(0, -oy)
                fx0, fy0 = max(0, ox), max(0, oy)
                fx1, fy1 = min(w, ox + pen_arr.shape[1]), min(h, oy + pen_arr.shape[0])
                if fx1 > fx0 and fy1 > fy0:
                    pa = pen_a[sy0: sy0 + fy1 - fy0, sx0: sx0 + fx1 - fx0]
                    region = frame[fy0:fy1, fx0:fx1].astype(np.float32)
                    mixed = region * (1 - pa) + pen_rgb[sy0: sy0 + fy1 - fy0, sx0: sx0 + fx1 - fx0] * pa
                    frame[fy0:fy1, fx0:fx1] = mixed.astype(np.uint8)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace")
        if proc.wait(timeout=600) != 0:
            raise MediaError("\n".join(err.strip().splitlines()[-4:]) or "ffmpeg failed")
    except BrokenPipeError:
        err = proc.stderr.read().decode(errors="replace")
        proc.wait(timeout=60)
        raise MediaError("\n".join(err.strip().splitlines()[-4:]) or "ffmpeg stopped") from None
    finally:
        if proc.poll() is None:
            proc.kill()
    return out
