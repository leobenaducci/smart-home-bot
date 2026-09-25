---
name: canvas-design
description: "Design a visual piece from a stated aesthetic philosophy rather than from a template — posters, covers, sheets, single-page art. Two steps: name and articulate a design movement, then express it on a canvas. Use when somebody asks for a poster, a piece of art, a designed page, or any static visual piece where how it *looks* is the point. Deliver it with the `document` skill as `html` or `pdf`."
license: "Apache-2.0 (LICENSE.txt). Adapted from the canvas-design skill in github.com/anthropics/skills, copyright Anthropic, PBC; modified for this stack."
---

# Canvas design

Instructions for creating design philosophies — aesthetic movements that are
then EXPRESSED VISUALLY.

Complete this in two steps:

1. Design philosophy creation
2. Express it on a canvas

## DESIGN PHILOSOPHY CREATION

Create a VISUAL PHILOSOPHY (not layouts or templates) that will be interpreted
through:

- Form, space, color, composition
- Images, graphics, shapes, patterns
- Minimal text as visual accent

**Name the movement** (1-2 words): "Brutalist Joy" / "Chromatic Silence" /
"Metabolist Dreams"

**Articulate the philosophy** (4-6 paragraphs):

- Space and form
- Color and material
- Scale and rhythm
- Composition and balance
- Visual hierarchy

**CRITICAL GUIDELINES:**

- Avoid redundancy: Each design aspect should be mentioned once
- Emphasize craftsmanship REPEATEDLY: The philosophy MUST stress that the final
  work should appear as though it took countless hours to create
- Leave creative space: Remain specific about the aesthetic direction, but
  concise enough for interpretive choices

### PHILOSOPHY EXAMPLES

**"Concrete Poetry"**
Philosophy: Communication through monumental form and bold geometry.
Visual expression: Massive color blocks, sculptural typography, Brutalist
spatial divisions, Polish poster energy meets Le Corbusier.

**"Chromatic Language"**
Philosophy: Color as the primary information system.
Visual expression: Geometric precision where color zones create meaning. Think
Josef Albers' interaction meets data visualization.

**"Analog Meditation"**
Philosophy: Quiet visual contemplation through texture and breathing room.
Visual expression: Paper grain, ink bleeds, vast negative space. Japanese
photobook aesthetic.

## CANVAS CREATION

Use the design philosophy to craft a masterpiece. Create museum or magazine
quality work:

- Generally use repeating patterns and perfect shapes
- Treat the abstract philosophical design as if it were a scientific bible
- Add sparse, clinical typography and systematic reference markers
- Anchor with simple phrase(s) positioned subtly
- Use a limited color palette that feels intentional and cohesive

**CRITICAL**: Create work that looks like it took countless hours. Make it
appear as though someone at the absolute top of their field labored over every
detail with painstaking care.

## HOW THE CANVAS REACHES THE PERSON, HERE

The two steps above are the method. This section is the part that is specific
to this house, and skipping it produces a masterpiece nobody can open.

**The canvas is `html`, made with the `document` skill.** Not a file you write
yourself, and not a path in this container — a path here is one the person
cannot reach, and it fails when they click it rather than when you make it.

```
exec python3 /app/nanobot/skills/document/create_doc.py '<json>'
```

`format: "html"` is the canvas. It is the only format where you choose the
colour, the type and the spacing, which is the whole of this skill — the other
formats apply a template and hand you back somebody else's design decisions.
Write the philosophy into the page's own CSS: the palette, the grid, the scale,
the rhythm.

`format: "pdf"` when it is going to be *printed* and the page size is fixed. It
renders the same HTML, so the design survives; you lose the screen and gain a
sheet.

**There is no `png` here.** `create_doc.py` produces `html`, `pdf`, `docx`,
`xlsx` and `pptx`, and nothing else — PNG appears inside it only for a chart
embedded in a document. Do not promise one, do not write one to disk, and do
not describe the piece as an image: say what it is, which is a page.

**Answer with the link the script returned, verbatim.** A `download:` link
written from memory does not fail when you write it, it fails when they click
it — and from here that looks exactly like it worked.

**Write the philosophy down.** Name the movement and state it in the reply, in
a few lines, before the link. It is what makes the piece legible as a decision
rather than as a style, and it is what somebody asks you to vary next time.
