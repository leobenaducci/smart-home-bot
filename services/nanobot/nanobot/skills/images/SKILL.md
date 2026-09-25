---
name: images
description: "Get a picture: find one in the open catalogues (Openverse, Wikimedia Commons) or DRAW one that does not exist yet with the household's image model. Use when a photo, an illustration, an icon, a diagram, a map, an SVG, a background or any decorative artwork is needed for a document, an invitation, a poster, a presentation or a web page — instead of describing one, leaving a placeholder, or settling for type on a flat background."
metadata: {"nanobot":{"emoji":"🖼️"}}
---

# Free images

Search and download images from two open catalogues, with no API key:

- **Openverse** — photographs and illustrations (Flickr, museums, free banks).
- **Wikimedia Commons** — diagrams, maps, schematics and **SVG**.

`type` decides which catalogue is searched first; `source` lets you force one
of the two.

## Search

```bash
python3 /app/nanobot/skills/images/find_image.py '{"action":"search","query":"andes mountain range","type":"photo","limit":5}'
```

- `query` — **English** does considerably better: the catalogues are tagged
  that way. Translate the search even when the answer will be in another
  language.
- `type` — `photo` · `illustration` · `drawing` · `svg` · `diagram` · `any`
- `limit` — 1 to 20 (5 is almost always enough)
- `source` — `openverse` or `commons` to force one; by default it goes to
  whichever suits the type.

Returns `results[]` with `title`, `creator`, `license`, `url`, `thumbnail`,
`page`, `width`/`height` and **`attribution`**.

## Download

```bash
python3 /app/nanobot/skills/images/find_image.py '{"action":"get","url":"<url from a result>","filename":"andes.jpg","attribution":"<the attribution from the result>"}'
```

It lands in the workspace's `media/` and returns the path in `file` (for
example `media/andes.jpg`). That path is what goes in the `image` field of a
section of the `document` skill, or in the message.

**It only downloads from the sources `search` returned.** A URL from anywhere
else is refused on purpose — it is the only defence against somebody else's
text making you fetch something inside the house. If a URL is refused, try the
`thumbnail` of the same result or pick another; don't insist with the same one.

## When you use it

- A document, a presentation or a page that would be better with a photo.
- A study guide that needs the diagram of a cell, a map, the solar system.
- An icon or an SVG for a design piece.
- Any case where you were about to write "[an image of X here]".

Don't use it when the person sent **their own** photo (you already have that
one) or for the house cameras (that is `camera-feed`).

## The attribution is not optional

Almost all of these licences require credit. When you use an image:

- In a document or a page, put the `attribution` under the image, in small
  type. In a presentation, in a footnote on the slide.
- In the chat one line is enough: "photo by \<author\> (CC BY)".
- If a result comes back `by-nc` and what they are making is commercial, pick
  another and say so in one line.

**Never invent an author or a licence**, and never use a URL that did not come
from a search: if you didn't find it, you don't have it.

## Flow

1. `search` with the query in English and the right `type`.
2. Pick **one** — the one that looks good and has a usable licence. Don't show
   all five for the person to choose from unless they ask.
3. `get` that one, with a descriptive `filename`.
4. Use it, with its attribution.

## Draw one that does not exist

The catalogues hold what somebody has already photographed. An invitation that
needs balloons *in this palette, on this ground* will not be found by searching
— so draw it:

```bash
python3 /app/nanobot/skills/images/find_image.py '{"action":"draw","prompt":"watercolour balloons in dusty rose and cream, soft confetti, on an off-white ground, empty centre, no text","shape":"portrait","filename":"globos.png"}'
```

- `prompt` — describe the **picture**, in English, the way you would describe a
  photograph: subject, palette, medium, ground. It is a picture, not an
  instruction to a designer.
- `shape` — `portrait`, `landscape` or `square` (the default).
- `quality` — **`normal` by default, and leave it there.** That is the
  household's own GPU: no bill, no page leaving the network, about half a
  minute a picture. `high` is a paid API, so it is not a dial you turn because
  the picture matters — it costs money every time.

  Reach for `high` in exactly two situations, and a person is in both:

  1. **They asked for it.** "hazla en alta calidad", "necesito imprimirla",
     "para el afiche" — anything that says this one is going somewhere.
  2. **They liked the draft and you offered.** Draw it with `normal` first,
     show it, and *if they are happy with it*, offer the better version:
     "¿La quieres en alta calidad para imprimir?" Then, and only then, redraw
     the same prompt with `high`.

  Never spend `high` on the first attempt of something nobody has seen yet.
  The draft is what decides whether the prompt was right, and getting that
  wrong at the dear price is the waste this rule exists to prevent.

  **Say which one drew it, every time, in the reply.** Not a footnote and not
  only when asked. **Read `paid` in the result, never the model name** — a
  model id is renamed on the admin page whenever the household feels like it,
  and a rule that depends on recognising one is a rule that goes quietly wrong
  the first time it changes. `paid: false` (equivalently `local: true`) is the
  house's own GPU; `paid: true` is a service being billed, whatever it is
  called:

  - `normal` → *"La dibujé con el modelo de la casa."* And when the picture
    looks finished — a poster, an invitation, anything with words on it —
    follow it with the offer: *"Si te gusta, la puedo repetir en alta calidad
    (cuesta, es un servicio de pago)."*
  - `high` → say that too, and why: *"Esta la hice en alta calidad, como
    pediste."*
  - fell back → the `fell_back` flag in the result means the house's GPU was
    busy and a **paid** model drew it instead. Say so plainly: the household
    did not choose to spend that, and finding out from a bill is the wrong way
    round.

  The reason is not politeness. Which model drew a picture is a spending
  decision and a privacy one — `high` sends the prompt to a company — and a
  person who is not told cannot object to either. Silence here is the whole
  failure: it looks identical whether the house drew it for nothing or a
  service was billed for it.
- `filename` — where it lands under `media/`.

Three things worth knowing before you use it:

- **Ask for artwork, not a finished piece.** These models set type badly:
  words come out misspelled, in the wrong language, or as letter-shaped noise.
  Draw the *background*, the *ornament*, the *object* — say "no text" — and set
  every word yourself in the HTML on top. That way the type is real, correct
  and editable, and the picture is the part a page cannot draw.

  When a word genuinely has to be *in* the picture — a sign, a chalkboard —
  **put it in quotes**: `a wooden sign that reads “MAÑANA”`. Measured on the
  house's own model, the bare form misspelled the same word in two of three
  samples (`MAÑAJA`, `MÁNADA`) and the quoted form in none of two. Then look at
  the result before anybody prints it: the accents survive and a neighbouring
  letter is what goes wrong, which is exactly the kind of error a glance
  catches and a spellcheck never will.
- **Leave it room.** Say where the picture should be empty ("empty centre",
  "clear lower third") so the words have somewhere to sit.
- **It costs money and takes seconds, unlike a search.** One or two good ones
  beat six tries.

The model is the household's own choice, from the admin page under Models →
pictures the assistant draws. If nothing is configured, this says so and you
should search instead.
