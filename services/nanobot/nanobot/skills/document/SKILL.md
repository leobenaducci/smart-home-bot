---
name: document
# Quoted, and the inner quotes escaped. Unquoted, the "script: exec" below made
# YAML read the line as a mapping, the frontmatter failed to parse, and the
# description silently became the skill's own name — so the only thing the model
# ever saw in its skill list was the word "document" and a file path. Any
# description here that contains ": " needs the same treatment.
description: "Create downloadable files — Word (.docx), PDF, Excel (.xlsx), PowerPoint (.pptx) and standalone web pages (.html). Stories, reports, letters, essays, quizzes, budgets, decks, landing pages. ALWAYS use the bundled script: exec python3 /app/nanobot/skills/document/create_doc.py '<json>'. NEVER write custom document-creation code. Triggers on \"create a document\", \"make a PDF\", \"export to Excel\", \"build a presentation\", \"make a web page\", \"generate a report\", or any request to produce a downloadable file."
---

# Document Generation

Creates downloadable files from one section list: **docx**, **pdf**, **xlsx**,
**pptx** and **html**.

**IMPORTANT: Always call `create_doc.py` via exec — never write your own
document-creation code** (no python-docx, openpyxl, python-pptx or reportlab by
hand).

## Usage

```bash
python3 /app/nanobot/skills/document/create_doc.py '<json>'
```

JSON format:

```json
{
  "format": "docx",
  "filename": "report.docx",
  "title": "Document Title",
  "sections": [
    {
      "heading": "Introduction",
      "text": "Paragraph text here. Use \\n to add more paragraphs."
    },
    {
      "heading": "Chapter 2",
      "text": "More content here.\nSecond paragraph."
    },
    {
      "heading": "Sales Chart",
      "chart_type": "bar",
      "data": {
        "labels": ["Jan", "Feb", "Mar", "Apr"],
        "values": [100, 200, 150, 300]
      }
    },
    {
      "heading": "Distribution",
      "chart_type": "pie",
      "data": {
        "labels": ["A", "B", "C"],
        "values": [40, 35, 25]
      }
    },
    {
      "heading": "Data Table",
      "table": {
        "headers": ["Column A", "Column B"],
        "rows": [["val1", "val2"], ["val3", "val4"]]
      }
    },
    {
      "heading": "Formula",
      "formula": "E=mc^2"
    },
    {
      "heading": "Questions",
      "questions": [
        {
          "question": "1. Simplify the fraction:",
          "items": ["a) 12/16 = ______", "b) 18/24 = ______"]
        }
      ]
    }
  ]
}
```

## Section types

- **text** (or **paragraph**): plain paragraph string; use `\n` to create multiple paragraphs within a section
- **chart_type** + **data.{labels, values}**: chart rendered via quickchart.io — type is `"bar"`, `"line"`, or `"pie"`. Use the longer `chart: {type, labels, datasets, title, x_label, y_label}` form when the chart needs a title or labelled axes (it should: a chart without units says nothing). In `xlsx` the chart is native and takes its title from the section heading.
- **questions**: list of `{question: "...", items: ["a) ...", "b) ..."]}` — for tests/quizzes. `items` are the printed options; there is no blank-answer-line field, so put the space to write in the question itself (`"3. Resuelve: ______________"`) or use `pdf` and leave room with a short `text` section.
- **items**: flat list of strings (bullet list)
- **table**: `{headers: [...], rows: [[...], ...]}`
- **formula**: LaTeX string rendered as image via codecogs.com
- **image**: workspace path to a picture already on disk, e.g. `"media/andes.jpg"` — what the `images` skill returns in its `file` field. Only paths inside the workspace are accepted; a URL is not.

## Choosing the format

`format` is `docx` · `pdf` · `xlsx` · `pptx` · `html`. Omit it and the
filename's extension decides; omit both and you get docx.

Pick by what the person will **do** with the file, not by the word they used:

| They want | `format` | Why |
|---|---|---|
| Something they will keep editing | `docx` | Word, collaborative — takes `html` too, see below |
| A report, a guide, a test, anything to print or send | `pdf` | Rendered from HTML by a browser, so it prints as designed |
| Numbers, a budget, movements, anything with columns | `xlsx` | They will sort and sum it |
| A presentation, a talk, a pitch | `pptx` | One slide per section |
| A web page, a landing, a portfolio | `html` | Standalone, opens in a browser |

"Send me this month's spending" is a spreadsheet even though nobody said
Excel. "I need this for school" is a PDF.

### Per-format notes

- **xlsx** — each `table` section becomes its own sheet, and each chart section
  becomes a sheet plus a **native Excel chart** that follows the cells. Prose,
  bullets and questions are collected into one `Notes` sheet. Numeric-looking
  cells are written as numbers so they can be summed.
- **pptx** — 16:9. One slide per section, title slide from `title` (+ optional
  `subtitle`). Long tables are truncated on the slide, visibly.
- **pdf** — **is a printed web page.** The same builder as `html` produces the
  layout and a real browser prints it, so colour, tables and a designed piece
  all survive; there is no separate PDF layout engine any more. Add
  `"page_break": true` to a section to start it on a new page, and pass
  **`html`** instead of `sections` when you want to control the design
  completely — exactly like `html`. One call, one file, **one link**: the page
  it was rendered from is kept beside it but is not offered.
- **html** — a standalone, responsive page with no external assets (charts are
  embedded). **It also comes back as a PDF**: the page is rendered by a real
  browser (`--print-to-pdf`), so grid, flex and background colours survive and
  the print result is the page you designed. The reply carries both links —
  give both. Pass `"render_pdf": false` to skip it. Prefer `html` over `pdf` for
  a piece you expect to adjust: it hands back the page as well as the print, and
  editing that markup is cheaper than rebuilding. Both render identically.
  Pass **`html`** instead of `sections` to supply the full markup
  yourself, which is the only way to control the design properly (`pdf` takes
  it too, and prints it):

  ```json
  {"format": "html", "filename": "landing.html", "title": "…", "html": "<section>…</section>"}
  ```

### A design that also has to be editable

`docx` takes the **`html`** field just like `html` and `pdf` do: it is for when
you have already made a piece and they ask for it "in Word so I can edit it".
**The design does not cross over** — docx has no grid, no positions, no designed
sheet — so what survives is the content: headings, paragraphs, lists, tables,
images and bold/italic, all editable. The script returns that warning in `Note:`
and **you repeat it when you hand the file over**, in one line; somebody opening
the file expecting the poster needs to know first.

If what they want is the piece as it is, that is `html` (and its PDF), not
`docx`.

### Canva

There is no Canva API here. **Never claim to have uploaded something to Canva
and never invent a Canva link.** Produce `pptx` (presentations) or `pdf`
(graphic pieces) — Canva imports both via File → Import — and say so in one
line.

## Output

The script prints two lines: a download link and where the file was filed on the
family share. Include the download link **verbatim** so the user can download
the file, and mention the share path so they know where it lives:

```
[📥 Download report.pdf](download:user1/alfred/documents/report.pdf)
Saved to your shared folder: user1/alfred/documents/report.pdf
```

The link points at the file **where it was saved on the share**, not at a
temporary copy: it is the same one the person sees in their "My files" panel.

**If the script prints no download link, there is NO file.** When it fails it
prints a JSON with `error` and exits with code 1 — almost always because the
JSON you sent had no `sections` with content in it. Read it, fix it and call
again. Never announce a document that was not generated, and never write a link
by hand: the only valid link is the one the script printed, verbatim.

**One file = one call.** If you offer the guide and the mark scheme, or the PDF
and the spreadsheet, that is two calls and two links. Mentioning a second
format you did not generate leaves the household with a dead link.

**Copy each link exactly as it came out, and don't change its text.** The link
carries the filename inside it:
`[📥 Download homework.pdf](download:user1/alfred/documents/homework.pdf)`.
Rewriting it as `[📥 Geometry homework](download:…)` is how the same URL ends up
pasted twice — it happened: a piece of homework and its mark scheme, both links
pointing at the mark scheme. With two files, paste each link **right after**
generating it, not both together at the end from memory.

Every generated file is written to the user's own folder on the family SMB
share automatically (`<your-folder>/alfred/documents/<file>`) — you never
need to upload it yourself, and that folder is what their chat shows as "Mis
archivos". If the second line reports that the share save failed, say so
briefly; the download link still works (it falls back to the workspace copy,
which does not survive the container).

To let **another** family member see it, share it through the share store with
the `file-share` skill's `share_with` action (never copy it into their folder).

## Common document types

**Story / Fiction**: Use `text` sections for each chapter. Put the full chapter prose in `text`, separating paragraphs with `\n`.

**Report**: Mix text sections with charts and tables as needed.

**Letter**: Omit headings; use plain `text` sections for date, body, and signature.

**Quiz / Worksheet**: Use `questions` sections — `pdf` to print, `docx` to edit.

**Budget / movements / any data**: `xlsx`, one `table` section per sheet.

**Presentation**: `pptx`, one section per slide, few bullets each.

**Web page**: `html`, with the `html` field when the design matters.

## Images

To illustrate something, fetch it first with the `images` skill (Openverse /
Wikimedia Commons, free, no key), then put the path it returns in a section's
`image` field:

```json
{"heading": "La cordillera", "image": "media/andes.jpg",
 "text": "Photo by X (CC BY) via Wikimedia Commons"}
```

Put the attribution in the section's `text` (or a `text` section right under the
figure) — most of those licences require the credit. `xlsx` ignores `image`;
`pdf` cannot place an SVG and falls back to the caption, so ask the `images`
skill for a `photo` or `illustration` when the target is a PDF.
