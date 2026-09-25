---
name: theme
description: "Change the colours of the house pages for this person — the portal, the chat, Tasks, Files, the Menu and the Shopping list — and generate a matching background image. Use it when somebody asks for another colour or theme, for something darker or lighter, or to go back to the usual colours."
metadata: {"nanobot":{"emoji":"🎨"}}
---

# The house theme

The house pages share one style, **The House**: plaster, ink, olive wall, honey
and clay. A theme is one person's version of it, and it changes three things:
the **colours**, the **shape** of buttons and panels, and the **backdrop**.
Type, spacing and layout are never touched — which is why you can propose a
theme without worrying: the worst that happens is that it looks ugly, never
that a button moves or disappears.

Themes are per person: yours changes nobody else's in the house.

## See the current theme

```bash
python3 /app/nanobot/skills/theme/scripts/theme.py '{"action":"show"}'
```

Returns the theme in use (or `null` for The House) and `roles`: the fourteen
colours and what each one is for. Read them before inventing a palette.

## Set a theme

**You choose the colours.** Don't take them from an image: decide the palette
first, and that palette is what gets passed to the image generator, so the
backdrop matches the house rather than the other way round.

```bash
python3 /app/nanobot/skills/theme/scripts/theme.py '{
  "action":"set",
  "name":"Plum",
  "colors":{
    "plaster":"#EFE4EC","paper":"#FCF7FA","paper-2":"#FFFFFF",
    "ink":"#2A2028","ink-soft":"#6B5F68",
    "wall":"#40263C","wall-fg":"#E6D8E2","wall-dim":"#9A8795",
    "olive":"#4F6B4A","olive-2":"#7B9475",
    "honey":"#B5477E","honey-lt":"#F0C9DE",
    "clay":"#A33B3B","line":"#E6D5E0"
  },
  "image":"cotton paper dyed violet, soft ink blooms"
}'
```

- `name` — what the theme is called, so the person recognises it.
- `colors` — all fourteen, in `#RRGGBB`. See `show` for what each one is.
- `shape` — **optional**, the shape of buttons and panels. One of four:
  `rounded` (the usual one), `soft`, `square` (almost sharp) or `pill`
  (fully rounded controls). Numbers aren't accepted: a free radius is a
  button that eats its own label.
- `image` — **optional**. A short description of the backdrop. The palette is
  appended to the prompt automatically; you describe the texture or the mood,
  not the colours. Without `image` the theme is colours only, which is plenty.

  **It can have drawings in it.** What it can't have is one *large drawing in
  the middle*, behind a form. All the prompt adds is that it must be
  wallpaper: a small motif, repeated and spread evenly across the frame, with
  nothing dominating the centre. So "a background with kittens" means actual
  kittens, drawn and repeated, not a pink smudge.

- `image_strength` — **optional**, how far that backdrop shows through the
  plaster: `faint`, `normal` (the usual one), `visible` or `strong`.

  **If the backdrop is a drawing, ask for `strong`.** The usual strength is
  meant for washes and gradients, and at that level kittens read as grey
  smudges — which is exactly the disappointment worth avoiding. For textures,
  `normal`. `strong` is the ceiling, and not arbitrarily: any higher and text
  over the backdrop stops meeting the minimum contrast.
- `drop_image: true` — remove the backdrop and keep the colours.

Without `image` the page ground is still painted in the theme's colours: you
don't get The House's cream glow sitting on top of a plum palette.

Generating the image takes a while; say so before starting, and don't do it if
the person only asked for a colour change.

## How to choose the colours

Think about the house, not about a palette in isolation:

- **`wall`** is the top bar and sets the character of the theme. Choose it
  first; everything else follows it.
- **`plaster` and `paper`** are nearly the same tone, `paper` a step lighter:
  sheets have to lift off the ground without jumping.
- **`honey`** is what can be touched. Make it show.
- **`olive` = money coming in, `clay` = money going out.** Keep them reading
  as "good" and "bad" whatever colour the theme is. Don't swap them.
- The derived dark tones (`--ink-deep` and company) are computed by HomeCore
  itself. Don't send them.

## If it gets rejected

Contrast is checked before anything is saved, by the same rule screen readers
use. If something wouldn't be legible, the answer says **which pair failed and
by how much**:

> text on sheets wouldn't be legible: "ink" on "paper" is 3.1:1 and needs
> 4.5:1. Darken "ink" or lighten "paper" and try again.

Fix that pair and send it again. Don't lower the bar, and don't tell the person
it "looks fine anyway": there are people in this house reading these pages on a
phone, in the sun.

## Nothing needs reloading

Open pages check the theme every 20 seconds and repaint themselves. If somebody
is looking at the chat when you change their colours, they see it change
without touching anything.

That goes for the backdrop too: if the image is slow, save the colours first
and the backdrop appears when it's ready. Don't ask anyone to reload.

## If the backdrop never appears

```bash
python3 /app/nanobot/skills/theme/scripts/theme.py '{"action":"check"}'
```

This answers everything that can be broken, in one run: whether the key reached
this script, whether Together accepts it (`key_works`, asked at a free
endpoint, before paying for an image), which model and endpoint are in use, and
what Together says when asked for a test image. What `diagnosis` reports is
Together's own wording, not a summary.

**Don't say the key has expired without having seen `key_works: false`.** Two
failures look identical from outside and are fixed differently:

- **401 with a JSON body** — that one really is the credential.
- **403 with `error code: 1010`** — that number is Cloudflare's, not
  Together's: it rejects the *client signature* and the request never reaches
  the API. With the same key, the same endpoint answers 403 under `urllib`'s
  default agent and 200 under any other. That's why the script sends its own
  `User-Agent`. Trying two models and seeing the same thing proves nothing
  here: both are stopped at the door, before the model exists.

And if the key never arrived at all: it is in the secrets file but not in
`tools.exec.allowedEnvKeys`, and a skill only sees the variables on that list.

## Back to The House

```bash
python3 /app/nanobot/skills/theme/scripts/theme.py '{"action":"reset"}'
```

Deletes the theme and the backdrop image. The pages go back to the usual olive.

## Where it shows

Everywhere HomeCore serves: the portal, the chat and its Professions, Tasks,
Files, the Menu and the Shopping list.

**The cameras too**, since HomeCore serves them itself at `/camaras/`. Cameras
live in the dark on purpose — reviewing recordings on a cream page is worse —
so they get the theme translated into their night register: the hue is yours,
the darkness is the page's. A light theme and a dark one look equally good
there; what changes is the colour, not how much light there is.

You have to come in through the house to see it. Opened directly on their own
address there is no HomeCore session, so nobody knows who is looking and they
keep the house colours.

The MQTT panel does **not** change: that is a different server with its own
entrance. If somebody asks why the broker is still green, that's why.
