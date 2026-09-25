---
name: canvas-image
description: "Produce a real image file — .png or .jpg — from HTML you design: posters, covers, social cards, lock-screen pieces, anything that has to BE an image rather than a page. Use when somebody asks for a png, a jpg, an image, a wallpaper or a card. ALWAYS use the bundled script: exec python3 /app/nanobot/skills/canvas-image/render_image.py '<json>'. Then upload it with the file-share skill and answer with the link that returned."
---

# Canvas image

The `document` skill makes html, pdf, docx, xlsx and pptx. It does **not** make
images. This does, and it is the only thing here that does — so if somebody
asked for a png or a jpg, this is the path.

## How

Design the piece as HTML and CSS, exactly as you would for `format: "html"` —
the palette, the grid, the type, the rhythm are all yours there. This only
decides what the camera sees.

```
exec python3 /app/nanobot/skills/canvas-image/render_image.py '<json>'
```

```json
{"html": "<html>…</html>",
 "out": "/tmp/poster.png",
 "format": "png",
 "width": 1200, "height": 630,
 "scale": 2}
```

| | |
|---|---|
| `html` or `html_path` | one of the two, required |
| `format` | `png` (default) or `jpg` |
| `width` / `height` | the viewport, and therefore the image. Default 1200×630 |
| `scale` | device pixel ratio, default 2 — this is what makes it crisp |
| `full_page` | capture past the fold. A poster has a size; leave this off for one |
| `quality` | jpg only, default 90 |

It prints one JSON object: `{"path", "format", "width", "height", "bytes"}`, or
`{"error"}`. Read it. An error is an error — do not describe a piece you did not
make.

**Sizes worth knowing.** 1200×630 is the link-preview and social card. 1080×1920
is a phone's lock screen. 2480×3508 is A4 at 300dpi, when the image is going to
be printed rather than looked at.

## Then hand it over

The script does not upload. A file at `/tmp/poster.png` is a file nobody else
can open — including the person who asked for it.

Upload it with the `file-share` skill, and **answer with the `download:` link
that call returned, verbatim.** A link written from memory does not fail when
you write it; it fails when they click it, and from here that looks exactly like
it worked.

## When this is the wrong skill

- **A page to read** — a report, a guide, a long text: `document` with
  `format: "html"`. An image of text cannot be selected, searched or resized.
- **Something to print** — `document` with `format: "pdf"`. It has real page
  sizes and real margins; an image has neither.
- **The design itself** — if the question is *what should this look like*, that
  is `canvas-design`. This is only the render at the end of it.
