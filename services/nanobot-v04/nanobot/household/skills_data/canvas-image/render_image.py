#!/usr/bin/env python3
"""Render HTML to a PNG or JPEG, for a piece that has to be an *image*.

WHY THIS EXISTS. `create_doc.py` makes html, pdf, docx, xlsx and pptx, and
nothing else -- PNG appears inside it only for a chart embedded in a document.
So anything that has to arrive as an image (a poster for a phone's lock screen,
a cover, a sheet somebody drops into a message) had no path at all, and a skill
that promised one would be promising something the house cannot deliver.

It renders rather than draws: the canvas is HTML and CSS, which is the one
format the Designer already chooses everything in -- the palette, the grid, the
type, the rhythm. This only decides what the camera sees.

Deliberately NOT an uploader. `file-share`'s `upload_file` already puts a file
in the person's folder and hands back the `download:` link that goes in the
chat, and that link is the thing that fails when it is written from memory. One
uploader, tested, rather than a second copy of it here.

    render_image.py '<json>'

    {"html": "<html>...</html>",     or "html_path": "/tmp/x.html"
     "out": "/tmp/poster.png",       optional; derived from format otherwise
     "format": "png" | "jpg",        default png
     "width": 1200, "height": 630,   the viewport; default 1200x630
     "full_page": false,             capture past the fold
     "quality": 90,                  jpg only
     "scale": 2}                     device pixel ratio, for a crisp result

Prints one JSON object: {"path": ..., "format": ..., "width": ..., "bytes": ...}
or {"error": ...}.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

# The same browsers create_doc.py looks for, in the same order, because it is
# the same image and a second opinion about which binary exists is a second
# thing to get wrong.
BROWSERS = ("chromium", "chromium-browser", "google-chrome-stable",
            "google-chrome", "headless_shell", "chrome")


def _browser() -> str | None:
    for name in BROWSERS:
        found = shutil.which(name)
        if found:
            return found
    return None


def render(spec: dict) -> dict:
    html, html_path = spec.get("html"), spec.get("html_path")
    if not html and not html_path:
        return {"error": "give either `html` or `html_path`"}

    fmt = str(spec.get("format") or "png").lower().lstrip(".")
    if fmt in ("jpeg",):
        fmt = "jpg"
    if fmt not in ("png", "jpg"):
        return {"error": f"format must be png or jpg, not {fmt!r}"}

    browser = _browser()
    if not browser:
        # Said rather than guessed at: without a browser there is no render,
        # and a caller should hear that instead of an empty file.
        return {"error": "no chromium in this image; cannot render"}

    width = int(spec.get("width") or 1200)
    height = int(spec.get("height") or 630)
    scale = float(spec.get("scale") or 2)
    out = spec.get("out") or os.path.join(
        tempfile.gettempdir(), f"canvas.{fmt}")
    out = os.path.abspath(out)

    tmp_html = None
    try:
        if html:
            fd, tmp_html = tempfile.mkstemp(suffix=".html")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(html)
            source = tmp_html
        else:
            source = os.path.abspath(html_path)
            if not os.path.isfile(source):
                return {"error": f"no such file: {source}"}

        # chromium screenshots PNG only, so a jpg is rendered as PNG and
        # converted below. Writing it straight to `out` when it is already a
        # png saves a copy.
        shot = out if fmt == "png" else out + ".png"
        argv = [browser, "--headless", "--no-sandbox", "--disable-gpu",
                "--hide-scrollbars", "--force-device-scale-factor=%g" % scale,
                f"--window-size={width},{height}", f"--screenshot={shot}"]
        if spec.get("full_page"):
            # Past the fold. Not the default: a poster has a size, and a
            # full-page capture of one is the same image with the page's
            # background stapled underneath.
            argv.append("--full-page-height")
        argv.append("file://" + source)

        proc = subprocess.run(argv, capture_output=True, timeout=120)
        if not os.path.isfile(shot) or os.path.getsize(shot) == 0:
            err = (proc.stderr or b"").decode(errors="replace")[-300:]
            return {"error": f"the browser produced no image: {err.strip()}"}

        if fmt == "jpg":
            try:
                from PIL import Image
            except ImportError:
                return {"error": "no Pillow in this image; cannot make a jpg. "
                                 "Ask for png."}
            with Image.open(shot) as im:
                # Flattened onto white: a JPEG has no alpha, and a transparent
                # background saved as one comes out black rather than absent.
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                    flat = Image.new("RGB", im.size, (255, 255, 255))
                    flat.paste(im, mask=im.split()[-1])
                    im = flat
                else:
                    im = im.convert("RGB")
                im.save(out, "JPEG", quality=int(spec.get("quality") or 90),
                        optimize=True, progressive=True)
            os.unlink(shot)

        return {"path": out, "format": fmt, "width": width, "height": height,
                "bytes": os.path.getsize(out)}
    except subprocess.TimeoutExpired:
        return {"error": "the browser took longer than 120s"}
    except Exception as exc:                                      # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        if tmp_html:
            try:
                os.unlink(tmp_html)
            except OSError:
                pass


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: render_image.py '<json>'"}))
        return 2
    try:
        spec = json.loads(sys.argv[1])
    except ValueError as exc:
        print(json.dumps({"error": f"invalid JSON: {exc}"}))
        return 2
    result = render(spec)
    print(json.dumps(result))
    return 0 if "path" in result else 1


if __name__ == "__main__":
    sys.exit(main())
