#!/usr/bin/env python3
"""
Document creation script for Alfred.
Usage: python3 create_doc.py '<json>'
JSON schema: {"format": "docx|pdf|xlsx|pptx|html",
              "filename": "...", "title": "...", "sections": [...]}

One section list, five renderers. The section vocabulary (heading / text /
items / questions / table / chart / formula) was already format-agnostic, so
the same JSON that made a .docx now makes a report, a spreadsheet, a deck or a
web page — which matters more for the prompt than for the code: one skill the
model has to learn instead of five it has to choose between.

`format` is optional; the filename's extension decides when it is absent, and
docx remains the default so every existing caller behaves exactly as before.
"""
import base64, json, mimetypes, os, re, shutil, subprocess, sys, unicodedata, urllib.parse
from html import escape as _html_escape
from html.parser import HTMLParser as _HTMLParser
from pathlib import Path

import httpx

from nanobot.security.network import validate_resolved_url, validate_url_target

WORKSPACE = Path(os.environ.get("NANOBOT_WORKSPACE", os.path.expanduser("~/.nanobot/workspace")))
DOCS_DIR = WORKSPACE / "media"
DOCS_DIR.mkdir(parents=True, exist_ok=True)

# Every generated document is filed on the family SMB share, inside the user's
# own folder (FILE_SHARE_FOLDER, one per nanobot container — see
# docker-compose.multiuser.yml). That copy is the document: it survives, it is
# in HomeCore's /files browser and in the chat's "My files" panel, it is
# backed up, and it can be shared with other family members through the share
# store (see the `file-share` skill). The chat link points at it directly.
#
# The workspace copy is a build artefact — this is where the renderers write —
# and is only used for the link when the share is unreachable, which is a
# degraded answer rather than the normal one.
SHARE_HOST = os.environ.get("FILE_SHARE_HOST", "")
SHARE_NAME = os.environ.get("FILE_SHARE_NAME", "share")
SHARE_FOLDER = os.environ.get("FILE_SHARE_FOLDER", "")
# Under `alfred/` because that is the sub-folder HomeCore's chat lists as "Mis
# archivos": a document that landed anywhere else was made by Alfred and then
# not shown by him.
SHARE_SUBDIR = os.environ.get("FILE_SHARE_DOCS_SUBDIR", "alfred/documents")

FORMATS = ("docx", "pdf", "xlsx", "pptx", "html")
_EXT_FORMAT = {".docx": "docx", ".pdf": "pdf", ".xlsx": "xlsx", ".xls": "xlsx",
               ".pptx": "pptx", ".ppt": "pptx", ".html": "html", ".htm": "html"}

# "The House" — the palette HomeCore and the MQTT dashboard already use, so an
# HTML page Alfred generates for the family looks like it belongs to the house.
# One docx column, used when an HTML table widens as it is read.
_DOCX_COL_WIDTH = 914400 * 6 // 4      # 1.5in in EMU

PALETTE = {"ink": "#2b2621", "soft": "#6e665a", "paper": "#fbf7ee",
           "paper2": "#f3ecdd", "line": "#e0d5bf", "honey": "#c6892b",
           "olive": "#5b6a41"}


def _save_to_share(local_path, filename):
    """Copy the generated file into the user's folder on the SMB share.

    Returns (share_relative_path, error). Best-effort by design: a share outage
    must never fail document creation — the caller still gets its download link.
    """
    if not SHARE_FOLDER:
        return None, "FILE_SHARE_FOLDER not set"
    # The filename comes from the model — keep it to a bare name so it can't
    # walk out of the user's folder.
    filename = os.path.basename(filename.replace("\\", "/")).strip() or "documento.docx"
    try:
        import smbclient
    except ImportError:
        return None, "smbprotocol not installed"
    try:
        smbclient.ClientConfig(
            username=os.environ.get("FILE_SHARE_USERNAME", "share"),
            password=os.environ.get("FILE_SHARE_PASSWORD", ""),
        )
        # Split on "/": the subdir is a path now, not one name, and joining it
        # whole would produce `user1\alfred/documentos\x.docx`.
        parts = ([SHARE_FOLDER] + [p for p in SHARE_SUBDIR.split("/") if p]
                 + [filename])
        unc = rf"\\{SHARE_HOST}\{SHARE_NAME}" + "\\" + "\\".join(parts)
        smbclient.makedirs(unc.rsplit("\\", 1)[0], exist_ok=True)
        with open(local_path, "rb") as src, smbclient.open_file(unc, mode="wb") as dst:
            shutil.copyfileobj(src, dst, 256 * 1024)
        return "/".join(parts), None
    except Exception as e:
        return None, str(e)


# quickchart.io and latex.codecogs.com are on the public internet, and a
# document build blocks the agent turn that asked for it. urllib has no
# timeout by default, so an unresponsive host hung the whole skill indefinitely.
_IMAGE_FETCH_TIMEOUT_S = 20


def _fetch_image(url, dest):
    ok, err = validate_url_target(url)
    if not ok:
        return False
    try:
        with httpx.Client(
            timeout=_IMAGE_FETCH_TIMEOUT_S,
            follow_redirects=True,
            max_redirects=5,
        ) as client:
            r = client.get(url)
            r.raise_for_status()
            redir_ok, redir_err = validate_resolved_url(str(r.url))
            if not redir_ok:
                return False
            Path(dest).write_bytes(r.content)
        return Path(dest).exists() and Path(dest).stat().st_size > 100
    except Exception:
        return False


def _chart_png(chart_cfg, dest):
    # `title` and the axis labels are carried through rather than dropped: the
    # personas require them ("Always with a title, labelled axes and units"),
    # and rebuilding `options` from scratch silently discarded every one, so
    # the model had no way to obey a rule it is given.
    options = {"plugins": {"legend": {"position": "top"}}}
    title = chart_cfg.get("title")
    if title:
        options["plugins"]["title"] = {"display": True, "text": str(title)}
    x_label, y_label = chart_cfg.get("x_label"), chart_cfg.get("y_label")
    if x_label or y_label:
        options["scales"] = {}
        if x_label:
            options["scales"]["x"] = {"title": {"display": True, "text": str(x_label)}}
        if y_label:
            options["scales"]["y"] = {"title": {"display": True, "text": str(y_label)}}
    cfg = {
        "type": chart_cfg.get("type", "bar"),
        "data": {
            "labels": chart_cfg.get("labels", []),
            "datasets": chart_cfg.get("datasets", []),
        },
        "options": options,
    }
    q = urllib.parse.quote(json.dumps(cfg))
    url = f"https://quickchart.io/chart?c={q}&w=500&h=300&bkg=white"
    return _fetch_image(url, dest)


def _formula_png(latex, dest):
    q = urllib.parse.quote(latex)
    url = f"https://latex.codecogs.com/png.download?\\large%20{q}"
    return _fetch_image(url, dest)


def _normalize_chart(sec):
    if "chart" in sec and isinstance(sec["chart"], dict):
        c = dict(sec["chart"])
        if "datasets" not in c and "data" in c and isinstance(c["data"], list):
            rows = c["data"]
            if rows and isinstance(rows[0], dict):
                c["labels"] = [r.get("label", "") for r in rows]
                c["datasets"] = [{"label": "Datos", "data": [r.get("value", 0) for r in rows]}]
                del c["data"]
        elif "datasets" not in c and "data" in c and isinstance(c["data"], dict):
            d = c["data"]
            c["labels"] = d.get("labels", [])
            c["datasets"] = [{"label": "Datos", "data": d.get("values", d.get("data", []))}]
            del c["data"]
        return c
    if "chart_type" in sec:
        d = sec.get("data", {})
        return {
            "type": sec["chart_type"],
            "labels": d.get("labels", []),
            "datasets": [{"label": sec.get("heading", "Datos"), "data": d.get("values", d.get("data", []))}],
        }
    return None


def _resolve_format(data):
    """(format, filename) — the two have to agree, whichever one was given.

    A model that says format "pdf" and filename "informe.docx" means a PDF; the
    extension is renamed to match rather than producing a PDF that Windows
    opens in Word and reports as corrupt.
    """
    fmt = str(data.get("format") or "").strip().lower().lstrip(".")
    # Bare name only — the same reason _save_to_share strips it again.
    filename = os.path.basename(str(data.get("filename") or "").replace("\\", "/")).strip()
    ext = os.path.splitext(filename)[1].lower()
    if fmt not in FORMATS:
        fmt = _EXT_FORMAT.get(ext, "docx")
    if _EXT_FORMAT.get(ext) != fmt:
        stem = (os.path.splitext(filename)[0]
                or _name_from_content(data)
                or "documento")
        filename = f"{stem}.{fmt}"
    return fmt, filename


def _slug(text, limit=60):
    """A filename stem out of a piece of the document.

    Accents are folded rather than kept: the share carries them fine and
    `_stream_share_file` has the RFC 5987 header for it, but these names are
    typed, said out loud and pasted into chats, and `presupuesto-cocina.pdf`
    survives all three better than `presupuesto-cocína.pdf`.
    """
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    if len(text) > limit:                      # cut on a word, not mid-word
        text = text[:limit].rsplit("-", 1)[0] or text[:limit]
    return text.strip("-")


def _name_from_content(data):
    """What to call a document the model did not name. '' if nothing fits.

    Every document used to be `documento.<ext>`, so the second one overwrote
    the first and a household's whole folder read as one file saved repeatedly.
    The name is derived from the document itself instead, in the order the
    thing is most likely to be *about*: its title, then the headings, then the
    page title or first h1 when raw `html` was sent instead of sections, then
    the opening words of the first paragraph.

    Deliberately not the model's job. It already had a `filename` field and
    left it empty, which is exactly the kind of optional field a prompt cannot
    make reliable -- and the fallback has to work on the turn where it is
    forgotten, not on the turn where it is remembered.
    """
    candidates = [data.get("title")]

    sections = [s for s in (data.get("sections") or []) if isinstance(s, dict)]
    candidates += [s.get("heading") for s in sections[:3]]

    html = data.get("html")
    if html:
        for pattern in (r"<title[^>]*>(.*?)</title>", r"<h1[^>]*>(.*?)</h1>"):
            m = re.search(pattern, str(html), re.I | re.S)
            if m:
                candidates.append(re.sub(r"<[^>]+>", " ", m.group(1)))

    candidates += [s.get("text") or s.get("paragraph") for s in sections[:3]]

    for candidate in candidates:
        if not candidate:
            continue
        slug = _slug(candidate)
        if slug:
            return slug
    return ""


def _free_path(directory, filename):
    """*filename* under *directory*, renamed if something is already there.

    Two files in one turn is the normal case — a test and its mark scheme, a guide
    and its solucionario — and nothing stopped the second call landing on the
    first one's name. The first file was replaced, both calls printed the same
    download link, and the reply offered two documents that were one: whichever
    was written last, twice. Reported as "ambos links apuntan a la pauta".

    Renaming rather than refusing: the caller cannot see the directory, the
    collision is not its mistake, and failing here would cost a document that
    was correctly built.
    """
    stem, ext = os.path.splitext(filename)
    candidate = Path(directory) / filename
    n = 2
    while candidate.exists():
        candidate = Path(directory) / f"{stem}-{n}{ext}"
        n += 1
    return candidate


def _paragraphs(body):
    """Section text is one string with \\n between paragraphs (documented in
    SKILL.md), and every renderer but docx needs them split."""
    return [p for p in str(body).split("\n") if p.strip()]


def _workspace_image(raw):
    """A picture the `images` skill already downloaded, resolved inside the
    workspace and nowhere else.

    The path comes from a model, so it is input: `media/andes.jpg` is what the
    images skill hands back, and anything that resolves outside the workspace —
    or does not exist — is refused rather than opened. Returns None when the
    section names no image.
    """
    if not raw or not isinstance(raw, str):
        return None
    root = WORKSPACE.resolve()
    try:
        p = (root / raw.lstrip("/")).resolve() if not os.path.isabs(raw) else Path(raw).resolve()
        p.relative_to(root)
    except (OSError, ValueError):
        return None
    return p if p.is_file() else None


def _section_images(sec, idx, tmp_imgs):
    """The pictures for one section: its own image, then chart and formula PNGs.

    Shared by every renderer that can place a picture. Returns a list of
    (kind, path_or_none, fallback_text) so each one decides what to do when the
    fetch failed — quickchart and codecogs are on the internet, and a document
    must still be produced when the house is offline. Only the fetched PNGs go
    into *tmp_imgs*: those are deleted after the build, and an image the user
    downloaded is theirs to keep.
    """
    out = []
    if sec.get("image"):
        p = _workspace_image(sec["image"])
        out.append(("image", str(p) if p else None,
                    f"[Imagen: {sec['image']}]"))
    chart = _normalize_chart(sec)
    if chart:
        p = f"/tmp/_chart_{idx}.png"
        tmp_imgs.append(p)
        ok = _chart_png(chart, p)
        out.append(("chart", p if ok else None, f"[Chart: {chart.get('type', 'chart')}]"))
    if "formula" in sec:
        p = f"/tmp/_formula_{idx}.png"
        tmp_imgs.append(p)
        ok = _formula_png(sec["formula"], p)
        out.append(("formula", p if ok else None, f"Formula: {sec['formula']}"))
    return out


# --- docx ---------------------------------------------------------------------

# --- html -> docx -------------------------------------------------------------
# A designed piece is written as HTML, and sometimes what is wanted afterwards is
# not a print but something to keep editing. Nothing here can carry the *design*
# across: docx is a flow-text format with no grid, no absolute positioning and no
# page-sized artwork, so a poster becomes its own text. That is the honest
# outcome and it is what `docx` means — the alternative was refusing the request.
#
# Written against the stdlib rather than adding pandoc or LibreOffice to the
# image: what has to be converted is either this file's own `_build_html` output
# or the markup a model wrote, both of which stay inside a small, known set of
# tags. A 25 MB apt package on the Pi to handle tags nobody emits is a bad trade,
# and one that also has to be kept installed for the one case it serves.
_DOCX_HEADINGS = {"h1": 0, "h2": 1, "h3": 2, "h4": 3, "h5": 4, "h6": 5}
# Elements whose *content* is not text on the page. Only tags that genuinely
# nest belong here: `html.parser` never emits an end tag for a void element, so
# a void tag in this set would raise the skip counter and never lower it — and
# every word after it would silently vanish. That is not hypothetical; `meta`
# and `link` were in this set, this file's own `_build_html` emits two `<meta>`
# tags in its head, and converting a page it had just written produced an empty
# document that reported success.
_DOCX_SKIP = {"script", "style", "head", "title", "noscript", "template"}
# Void elements: no end tag ever arrives, so nothing may be pushed on their name.
# Checked in `handle_endtag` as well, which today changes nothing — none of these
# is handled there — but stops the next tag added to `_DOCX_BREAK` from silently
# flushing a paragraph on an end tag that never comes. Cheap insurance against
# the exact shape of the bug that made this rewrite necessary.
_DOCX_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
              "meta", "param", "source", "track", "wbr"}
_DOCX_BREAK = {"p", "div", "section", "article", "header", "footer", "figcaption",
               "blockquote", "pre", "figure", "main", "aside", "nav", "tr", "caption"}


def _para_text(para):
    return "".join(r.text for r in para.runs)


class _HtmlToDocx(_HTMLParser):
    """Flatten HTML into a python-docx document.

    Deliberately shallow. Headings, paragraphs, lists, tables, images and
    inline bold/italic are what the section builder produces and what a model
    writes by hand; everything else contributes its text and nothing more.
    Unknown tags therefore degrade to prose rather than disappearing, which is
    the behaviour that matters when the input is written by a model.

    Tables are kept on a stack rather than in one slot, because a nested table
    is an ordinary layout idiom in the markup this is fed — and with a single
    slot the inner `</table>` closed the outer one, spilling every remaining
    cell into the body as one run-on paragraph.
    """

    def __init__(self, doc, tmp_imgs):
        super().__init__(convert_charrefs=True)
        self.doc, self.tmp_imgs = doc, tmp_imgs
        self._skip = 0
        self._bold = self._italic = 0
        self._para = None            # the paragraph being filled, if any
        self._pending_style = None   # list style for the next paragraph
        self._heading = None
        self._frames = []            # nested tables, innermost last
        self._ordered = []
        self._th = []
        self._space_pending = False
        self.saw_heading = False

    # -- where text goes --------------------------------------------------
    def _flush(self):
        self._para = None
        self._space_pending = False

    def _cell(self):
        return self._frames[-1]["cell"] if self._frames else None

    def _style_named(self, name):
        try:
            return self.doc.styles[name]
        except Exception:
            return None

    def _target(self):
        """The paragraph text should go into, created on demand."""
        if self._para is not None:
            return self._para
        cell = self._cell()
        if cell is not None:
            first = cell.paragraphs[0]
            para = first if not first.runs and not first.text else cell.add_paragraph()
            # A cell's paragraph is made by the cell, so the style that
            # `add_heading`/`add_paragraph` would have applied is set here
            # instead — otherwise a heading or a bullet inside a table quietly
            # came out as body text.
            name = ("Title" if self._heading == 0 else f"Heading {self._heading}") \
                if self._heading is not None else self._pending_style
            if name and (style := self._style_named(name)):
                para.style = style
        elif self._heading is not None:
            para = self.doc.add_heading("", self._heading)
        elif self._pending_style:
            para = self.doc.add_paragraph("", style=self._pending_style)
        else:
            para = self.doc.add_paragraph()
        self._para = para
        return para

    def _image(self, src):
        from docx.shared import Inches
        path = _image_from_src(src, self.tmp_imgs)
        if not path:
            return
        try:
            if self._cell() is not None:
                # Into the cell, not the body: `doc.add_picture` appends to the
                # document, so a picture in a table used to be relocated out of
                # its cell and out of order.
                self._target().add_run().add_picture(path, width=Inches(2.5))
            else:
                self.doc.add_picture(path, width=Inches(5.5))
                self._flush()
        except Exception:
            pass                      # a broken picture must not lose the text

    # -- parser -----------------------------------------------------------
    def handle_startendtag(self, tag, attrs):
        # `<br/>`, `<img … />`. Handled as a start tag only — the default
        # implementation would also fire handle_endtag and unbalance the state.
        self.handle_starttag(tag, attrs)

    def handle_starttag(self, tag, attrs):
        if tag in _DOCX_SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        attrs = dict(attrs)
        if tag in _DOCX_HEADINGS:
            self._flush()
            self._heading = _DOCX_HEADINGS[tag]
            self.saw_heading = True
        elif tag in ("b", "strong"):
            self._bold += 1
        elif tag in ("i", "em"):
            self._italic += 1
        elif tag == "br":
            self._target().add_run().add_break()
            self._space_pending = False
        elif tag in ("ul", "ol"):
            self._flush()
            self._ordered.append(tag == "ol")
        elif tag == "li":
            self._flush()
            self._pending_style = "List Number" if (
                self._ordered and self._ordered[-1]) else "List Bullet"
        elif tag == "table":
            self._flush()
            table = self.doc.add_table(rows=0, cols=0)
            if style := self._style_named("Table Grid"):
                table.style = style
            self._frames.append({"table": table, "row": None, "used": 0, "cell": None})
        elif tag == "tr" and self._frames:
            self._frames[-1]["row"] = None      # created lazily, see _next_cell
            self._frames[-1]["used"] = 0
            self._flush()
        elif tag in ("td", "th") and self._frames:
            self._frames[-1]["cell"] = _next_cell(self._frames[-1])
            self._flush()
            self._th.append(tag == "th")
            if tag == "th":
                self._bold += 1
        elif tag == "img":
            self._image(attrs.get("src", ""))
        elif tag == "hr":
            self._flush()
            self.doc.add_paragraph("─" * 30)
        elif tag in _DOCX_BREAK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in _DOCX_SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip or tag in _DOCX_VOID:
            return
        if tag in _DOCX_HEADINGS:
            self._flush()
            self._heading = None
        elif tag in ("b", "strong"):
            self._bold = max(0, self._bold - 1)
        elif tag in ("i", "em"):
            self._italic = max(0, self._italic - 1)
        elif tag in ("ul", "ol"):
            if self._ordered:
                self._ordered.pop()
            self._pending_style = None
            self._flush()
        elif tag == "li":
            self._flush()
            self._pending_style = None
        elif tag in ("td", "th"):
            if self._th and self._th.pop():
                self._bold = max(0, self._bold - 1)
            if self._frames:
                self._frames[-1]["cell"] = None
            self._flush()
        elif tag == "tr":
            if self._frames:
                self._frames[-1]["row"] = None
            self._flush()
        elif tag == "table":
            if self._frames:
                self._frames.pop()
            self._flush()
        elif tag in _DOCX_BREAK:
            self._flush()

    def handle_data(self, text):
        if self._skip or not text:
            return
        collapsed = re.sub(r"\s+", " ", text)
        body = collapsed.strip()
        if not body:
            # Whitespace between two inline elements is a word gap, not noise.
            # Dropping it welded "<strong>Hola</strong> <em>mundo</em>" into
            # "Holamundo" — and the runs were individually correct, so nothing
            # that inspected them could see it.
            if self._para is not None and _para_text(self._para):
                self._space_pending = True
            return
        para = self._target()
        # The gap goes in its own plain run rather than onto the front of this
        # one: a word separator is not part of the bold word, and Word renders
        # a bold space slightly wider than a plain one.
        if (collapsed[0] == " " or self._space_pending) and _para_text(para):
            para.add_run(" ")
        run = para.add_run(body)
        self._space_pending = collapsed[-1] == " "
        if self._bold:
            run.bold = True
        if self._italic:
            run.italic = True


def _next_cell(frame):
    """The next cell of the current row, widening the table as needed.

    HTML gives no column count up front and docx wants one, so the table grows a
    column the first time a row needs it — which also means a ragged table (a
    row with an extra cell) widens rather than raising. The row is created here
    rather than on `<tr>`, so `<table><td>…` — no `<tr>` at all, and `html.parser`
    inserts none — is a table instead of a lost document.
    """
    if frame["row"] is None:
        if not frame["table"].columns:
            frame["table"].add_column(_DOCX_COL_WIDTH)
        frame["row"] = frame["table"].add_row()
        frame["used"] = 0
    while frame["used"] >= len(frame["table"].columns):
        frame["table"].add_column(_DOCX_COL_WIDTH)
    cell = frame["row"].cells[frame["used"]]
    frame["used"] += 1
    return cell


def _image_from_src(src, tmp_imgs):
    """A local file for an <img src>: a data: URI decoded, or a workspace path."""
    src = (src or "").strip()
    if src.startswith("data:"):
        try:
            head, b64 = src.split(",", 1)
            ext = mimetypes.guess_extension(head[5:].split(";")[0]) or ".png"
            path = str(DOCS_DIR / f"_htmlimg{len(tmp_imgs)}{ext}")
            Path(path).write_bytes(base64.b64decode(b64))
            tmp_imgs.append(path)
            return path
        except Exception:
            return None
    if src.startswith(("http://", "https://")):
        path = str(DOCS_DIR / f"_htmlimg{len(tmp_imgs)}.png")
        if _fetch_image(src, path):
            tmp_imgs.append(path)
            return path
        return None
    return _workspace_image(src)


def _html_into_docx(html, doc, tmp_imgs):
    """Returns True if the markup carried its own top-level heading."""
    parser = _HtmlToDocx(doc, tmp_imgs)
    parser.feed(html)
    parser.close()
    return parser.saw_heading


def _build_docx(data, out_path, tmp_imgs):
    from docx import Document
    from docx.shared import Inches

    doc = Document()

    # The designer's markup, asked for as something editable.
    raw = data.get("html")
    if raw and str(raw).strip():
        saw_heading = _html_into_docx(str(raw), doc, tmp_imgs)
        # The page's own <h1> is the title when it has one. A bare fragment has
        # none, and dropping the `title` the caller did send left the document
        # untitled — the comment here used to claim otherwise while the early
        # return skipped the title block below it.
        if data.get("title") and not saw_heading:
            doc.paragraphs[0].insert_paragraph_before(
                str(data["title"]), style=doc.styles["Title"]) \
                if doc.paragraphs else doc.add_heading(str(data["title"]), 0)
        note = ("the design is not preserved in docx: it is the content of "
                "the page, editable")
        if data.get("sections"):
            # Both were sent and only one can be used. Saying so beats handing
            # back a document that is quietly half of what was asked for.
            note += ". `html` was used and `sections` was ignored"
        doc.save(str(out_path))
        return {"from_html": True, "note": note}

    title = data.get("title")
    if title:
        doc.add_heading(title, 0)

    for i, sec in enumerate(data.get("sections", [])):
        heading = sec.get("heading")
        if heading:
            doc.add_heading(heading, sec.get("level", 1))

        body = sec.get("text") or sec.get("paragraph")
        if body:
            doc.add_paragraph(str(body))

        if "questions" in sec:
            for q in sec["questions"]:
                q_text = q.get("question", "")
                if q_text:
                    p = doc.add_paragraph()
                    p.add_run(q_text).bold = True
                for item in q.get("items", []):
                    doc.add_paragraph(str(item), style="List Bullet")
                doc.add_paragraph("")

        if "items" in sec and "questions" not in sec:
            for item in sec["items"]:
                doc.add_paragraph(str(item), style="List Bullet")

        if "table" in sec:
            tbl = sec["table"]
            headers = tbl.get("headers", [])
            rows = tbl.get("rows", [])
            if headers or rows:
                cols = max(len(headers), len(rows[0]) if rows else 1)
                table = doc.add_table(rows=1 + len(rows), cols=cols)
                table.style = "Table Grid"
                for j, h in enumerate(headers):
                    table.rows[0].cells[j].text = str(h)
                for r_idx, row in enumerate(rows):
                    for c_idx, val in enumerate(row):
                        table.rows[r_idx + 1].cells[c_idx].text = str(val)

        for kind, path, fallback in _section_images(sec, i, tmp_imgs):
            if path and kind == "formula":
                doc.add_picture(path)          # inline size: a formula is text
            elif path:
                doc.add_picture(path, width=Inches(5))
            else:
                doc.add_paragraph(fallback)

    doc.save(str(out_path))


# --- pdf ----------------------------------------------------------------------

def _build_pdf(data, out_path, tmp_imgs):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (Image, ListFlowable, ListItem, PageBreak,
                                    Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)
    from xml.sax.saxutils import escape as xml_escape

    styles = getSampleStyleSheet()
    ink = colors.HexColor(PALETTE["ink"])
    honey = colors.HexColor(PALETTE["honey"])
    body_style = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10.5,
                                leading=15, textColor=ink, alignment=TA_LEFT,
                                spaceAfter=6)
    h_styles = {
        0: ParagraphStyle("h0", parent=styles["Title"], textColor=ink, spaceAfter=18),
        1: ParagraphStyle("h1", parent=styles["Heading1"], fontSize=15, leading=19,
                          textColor=honey, spaceBefore=14, spaceAfter=8),
        2: ParagraphStyle("h2", parent=styles["Heading2"], fontSize=12.5, leading=16,
                          textColor=ink, spaceBefore=10, spaceAfter=6),
    }

    def head_style(level):
        return h_styles.get(min(int(level or 1), 2), h_styles[2])

    story = []
    title = data.get("title")
    if title:
        story.append(Paragraph(xml_escape(str(title)), h_styles[0]))

    for i, sec in enumerate(data.get("sections", [])):
        if sec.get("page_break"):
            story.append(PageBreak())
        heading = sec.get("heading")
        if heading:
            story.append(Paragraph(xml_escape(str(heading)), head_style(sec.get("level", 1))))

        body = sec.get("text") or sec.get("paragraph")
        if body:
            for para in _paragraphs(body):
                story.append(Paragraph(xml_escape(para), body_style))

        if "questions" in sec:
            for q in sec["questions"]:
                q_text = q.get("question", "")
                if q_text:
                    story.append(Paragraph(f"<b>{xml_escape(str(q_text))}</b>", body_style))
                for item in q.get("items", []):
                    story.append(Paragraph(xml_escape(str(item)), body_style))
                story.append(Spacer(1, 8))

        if "items" in sec and "questions" not in sec:
            story.append(ListFlowable(
                [ListItem(Paragraph(xml_escape(str(it)), body_style)) for it in sec["items"]],
                bulletType="bullet", leftIndent=14))

        if "table" in sec:
            tbl = sec["table"]
            headers = [str(h) for h in tbl.get("headers", [])]
            rows = [[str(c) for c in r] for r in tbl.get("rows", [])]
            if headers or rows:
                grid = ([headers] if headers else []) + rows
                cell = ParagraphStyle("cell", parent=body_style, fontSize=9, leading=12,
                                      spaceAfter=0)
                grid = [[Paragraph(xml_escape(c), cell) for c in row] for row in grid]
                t = Table(grid, repeatRows=1 if headers else 0, hAlign="LEFT")
                style = [
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor(PALETTE["line"])),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
                if headers:
                    style += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(PALETTE["paper2"]))]
                t.setStyle(TableStyle(style))
                story.append(t)
                story.append(Spacer(1, 10))

        for kind, path, fallback in _section_images(sec, i, tmp_imgs):
            placed = False
            if path:
                # Fixed width, height derived so the aspect ratio survives.
                # A format reportlab cannot read (SVG, most likely, since the
                # images skill returns those) falls back to the caption rather
                # than failing the whole document at build time.
                try:
                    from reportlab.lib.utils import ImageReader
                    iw, ih = ImageReader(path).getSize()
                    w = min(13 * cm, iw * 0.75)
                    story.append(Image(path, width=w, height=w * ih / iw))
                    story.append(Spacer(1, 10))
                    placed = True
                except Exception:
                    placed = False
            if not placed:
                story.append(Paragraph(xml_escape(fallback), body_style))

    # Nothing rendered. `create` refuses a contentless payload before we get
    # here, so reaching this means a section shape it accepted produced no
    # output — a bug, not a document. Raising sends the caller an error it
    # can act on; the old fallback wrote a one-slide file titled
    # "Presentation" and reported success, which is the report that got us
    # here: an empty deck, announced with a working download link.
    if not story:
        raise ValueError("no content was generated for the PDF")
    SimpleDocTemplate(str(out_path), pagesize=A4,
                      leftMargin=2.2 * cm, rightMargin=2.2 * cm,
                      topMargin=2 * cm, bottomMargin=2 * cm,
                      title=str(title or out_path.stem)).build(story)


# --- xlsx ---------------------------------------------------------------------

def _sheet_name(raw, used):
    """Excel: ≤31 chars, no []:*?/\\, and unique within the workbook."""
    name = "".join(c for c in str(raw or "Hoja") if c not in "[]:*?/\\").strip()[:31]
    name = name or "Hoja"
    base, n = name, 2
    while name.lower() in used:
        suffix = f" {n}"
        name = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(name.lower())
    return name


def _build_xlsx(data, out_path, tmp_imgs):
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    used = set()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor=PALETTE["olive"].lstrip("#"))
    notes = []   # everything that is prose rather than a grid

    for i, sec in enumerate(data.get("sections", [])):
        heading = sec.get("heading") or f"Section {i + 1}"
        table = sec.get("table")
        chart = _normalize_chart(sec)

        if table:
            ws = wb.create_sheet(_sheet_name(heading, used))
            headers = [str(h) for h in table.get("headers", [])]
            rows = table.get("rows", [])
            if headers:
                ws.append(headers)
                for c in ws[1]:
                    c.font, c.fill = head_font, head_fill
                    c.alignment = Alignment(vertical="center")
                ws.freeze_panes = "A2"
            for row in rows:
                # Numbers stay numbers: a column of text that looks like money
                # cannot be summed, which is the whole point of a spreadsheet.
                ws.append([_maybe_number(v) for v in row])
            _autosize(ws, get_column_letter)
        elif chart:
            ws = wb.create_sheet(_sheet_name(heading, used))
            labels = chart.get("labels", [])
            datasets = chart.get("datasets", [])
            ws.append(["Etiqueta"] + [d.get("label", "Datos") for d in datasets])
            for c in ws[1]:
                c.font, c.fill = head_font, head_fill
            for r, lab in enumerate(labels):
                ws.append([lab] + [_maybe_number(_at(d.get("data", []), r)) for d in datasets])
            _autosize(ws, get_column_letter)
            # A native Excel chart, not the quickchart PNG: in a spreadsheet the
            # chart should follow the cells when someone edits them.
            kind = (chart.get("type") or "bar").lower()
            ch = {"line": LineChart, "pie": PieChart}.get(kind, BarChart)()
            ch.title = heading
            ch.add_data(Reference(ws, min_col=2, min_row=1,
                                  max_col=1 + len(datasets), max_row=1 + len(labels)),
                        titles_from_data=True)
            ch.set_categories(Reference(ws, min_col=1, min_row=2, max_row=1 + len(labels)))
            ws.add_chart(ch, f"{get_column_letter(len(datasets) + 3)}2")

        body = sec.get("text") or sec.get("paragraph")
        if body:
            notes.append((heading, _paragraphs(body)))
        if "items" in sec and "questions" not in sec:
            notes.append((heading, [f"• {it}" for it in sec["items"]]))
        if "questions" in sec:
            lines = []
            for q in sec["questions"]:
                if q.get("question"):
                    lines.append(str(q["question"]))
                lines += [f"    {it}" for it in q.get("items", [])]
            notes.append((heading, lines))

    if notes:
        # Appended, after the grids: someone opening a movements workbook wants
        # the movements first. (The old `0 if not wb.sheetnames else None` read
        # as "Notas first" but evaluated to append in every reachable case.)
        ws = wb.create_sheet(_sheet_name("Notas", used))
        title = data.get("title")
        if title:
            ws.append([str(title)])
            ws["A1"].font = Font(bold=True, size=14)
            ws.append([])
        for heading, lines in notes:
            ws.append([heading])
            ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
            for line in lines:
                ws.append([line])
            ws.append([])
        ws.column_dimensions["A"].width = 100
        for row in ws.iter_rows(min_col=1, max_col=1):
            row[0].alignment = Alignment(wrap_text=True, vertical="top")

    # Nothing rendered. `create` refuses a contentless payload before we get
    # here, so reaching this means a section shape it accepted produced no
    # output — a bug, not a document. Raising sends the caller an error it
    # can act on; the old fallback wrote a one-slide file titled
    # "Presentation" and reported success, which is the report that got us
    # here: an empty deck, announced with a working download link.
    if not wb.sheetnames:
        raise ValueError("no sheet was generated")
    wb.save(str(out_path))


def _at(seq, idx):
    return seq[idx] if idx < len(seq) else None


# es/es-ES numbers: "." groups thousands and "," is the decimal mark. The
# grouped form has to be matched explicitly, because reading "39.990" with
# float() gives 39.99 — a peso amount silently divided by a thousand in the one
# artefact that exists to be summed. Anything that does not match one of these
# three shapes stays text: a cell that is obviously a number is worth converting,
# a cell that is only arguably one is not.
_NUM_GROUPED = re.compile(r"[+-]?\d{1,3}(?:\.\d{3})+(?:,\d+)?$")   # 39.990 · 1.234,56
_NUM_COMMA = re.compile(r"[+-]?\d+,\d+$")                          # 1,5
_NUM_DOT = re.compile(r"[+-]?\d+\.\d+$")                           # 3.14


def _maybe_number(v):
    if isinstance(v, (int, float)) or v is None:
        return v
    s = str(v).strip()
    try:
        return int(s)
    except ValueError:
        pass
    if _NUM_GROUPED.fullmatch(s):
        s = s.replace(".", "").replace(",", ".")
        return int(s) if "." not in s else float(s)
    if _NUM_COMMA.fullmatch(s):
        return float(s.replace(",", "."))
    if _NUM_DOT.fullmatch(s):
        return float(s)
    return v


def _autosize(ws, get_column_letter, cap=60):
    for idx, col in enumerate(ws.iter_cols(), start=1):
        width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[get_column_letter(idx)].width = min(max(width + 2, 10), cap)


# --- pptx ---------------------------------------------------------------------

def _build_pptx(data, out_path, tmp_imgs):
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Emu, Inches, Pt
    try:
        from PIL import Image as _PILImage
    except ImportError:                       # sizing falls back to 500x300
        _PILImage = None

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)  # 16:9
    blank = prs.slide_layouts[6]
    title_layout = prs.slide_layouts[0]
    content_layout = prs.slide_layouts[1]
    ink = RGBColor.from_string(PALETTE["ink"].lstrip("#"))
    honey = RGBColor.from_string(PALETTE["honey"].lstrip("#"))

    title = data.get("title")
    if title:
        s = prs.slides.add_slide(title_layout)
        s.shapes.title.text = str(title)
        if len(s.placeholders) > 1:
            sub = data.get("subtitle") or ""
            if sub:
                s.placeholders[1].text = str(sub)
            else:
                # An empty placeholder still prints "Click to add subtitle" in
                # some viewers; removing it is cleaner than leaving it blank.
                ph = s.placeholders[1]
                ph._element.getparent().remove(ph._element)

    for i, sec in enumerate(data.get("sections", [])):
        heading = sec.get("heading") or ""
        bullets = []
        body = sec.get("text") or sec.get("paragraph")
        if body:
            bullets += _paragraphs(body)
        if "items" in sec and "questions" not in sec:
            bullets += [str(it) for it in sec["items"]]
        if "questions" in sec:
            for q in sec["questions"]:
                if q.get("question"):
                    bullets.append(str(q["question"]))
                bullets += [f"   {it}" for it in q.get("items", [])]

        images = [p for kind, p, _ in _section_images(sec, i, tmp_imgs) if p]
        table = sec.get("table")

        s = prs.slides.add_slide(content_layout if (bullets and not images and not table) else blank)
        # The blank layout has no title placeholder, so headings are drawn.
        if s.slide_layout is blank:
            box = s.shapes.add_textbox(Inches(0.7), Inches(0.45),
                                       prs.slide_width - Inches(1.4), Inches(1.0))
            p = box.text_frame.paragraphs[0]
            p.text = heading
            p.font.size, p.font.bold, p.font.color.rgb = Pt(30), True, honey
        else:
            s.shapes.title.text = heading

        top = Inches(1.6)
        if s.slide_layout is content_layout and bullets:
            tf = s.placeholders[1].text_frame
            tf.clear()
            for j, line in enumerate(bullets[:10]):
                para = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
                para.text = line
                para.font.size, para.font.color.rgb = Pt(18), ink
        elif bullets:
            box = s.shapes.add_textbox(Inches(0.7), top,
                                       prs.slide_width - Inches(1.4), Inches(2.2))
            tf = box.text_frame
            tf.word_wrap = True
            for j, line in enumerate(bullets[:8]):
                para = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
                para.text = line
                para.font.size, para.font.color.rgb = Pt(16), ink
            top = Inches(3.9)

        if table:
            headers = [str(h) for h in table.get("headers", [])]
            rows = [[str(c) for c in r] for r in table.get("rows", [])]
            # A slide is not a spreadsheet: past a dozen rows it is unreadable,
            # so it is truncated visibly rather than silently overflowing.
            shown, cut = rows[:12], max(0, len(rows) - 12)
            n_cols = max(len(headers), len(shown[0]) if shown else 1)
            n_rows = len(shown) + (1 if headers else 0)
            if n_rows:
                shape = s.shapes.add_table(n_rows, n_cols, Inches(0.7), top,
                                           prs.slide_width - Inches(1.4),
                                           Inches(0.4) * n_rows)
                tbl = shape.table
                for j, h in enumerate(headers[:n_cols]):
                    tbl.cell(0, j).text = h
                for r_idx, row in enumerate(shown):
                    for c_idx, val in enumerate(row[:n_cols]):
                        tbl.cell(r_idx + (1 if headers else 0), c_idx).text = val
                if cut:
                    note = s.shapes.add_textbox(Inches(0.7), top + Inches(0.4) * n_rows + Inches(0.1),
                                                Inches(6), Inches(0.4))
                    note.text_frame.paragraphs[0].text = f"(+{cut} more rows in the original file)"
                    note.text_frame.paragraphs[0].font.size = Pt(11)

        for img in images:
            try:
                with _PILImage.open(img) as im:
                    iw, ih = im.size
            except Exception:                 # unreadable, or no Pillow
                iw, ih = 500, 300
            max_w = prs.slide_width - Inches(1.4)
            max_h = prs.slide_height - top - Inches(0.6)
            scale = min(max_w / Emu(int(iw * 9525)), max_h / Emu(int(ih * 9525)), 1.0) \
                if iw and ih else 1.0
            w = int(iw * 9525 * scale)
            s.shapes.add_picture(img, int((prs.slide_width - w) / 2), top, width=w)

    # Nothing rendered. `create` refuses a contentless payload before we get
    # here, so reaching this means a section shape it accepted produced no
    # output — a bug, not a document. Raising sends the caller an error it
    # can act on; the old fallback wrote a one-slide file titled
    # "Presentation" and reported success, which is the report that got us
    # here: an empty deck, announced with a working download link.
    if not prs.slides:
        raise ValueError("no slide was generated")
    prs.save(str(out_path))


# --- html ---------------------------------------------------------------------

def _data_uri(path):
    # The mime has to come from the file: charts and formulas are PNG, but an
    # image the `images` skill fetched is just as likely a JPEG or an SVG, and
    # a browser handed `data:image/png` holding SVG bytes draws nothing.
    try:
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        return f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode()
    except Exception:
        return ""


_HTML_CSS = """
*,*::before,*::after{box-sizing:border-box}
body{margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",
  Roboto,Helvetica,Arial,sans-serif;line-height:1.65;color:%(ink)s;
  background:%(paper)s;-webkit-text-size-adjust:100%%}
.wrap{max-width:46rem;margin:0 auto;padding:clamp(1.5rem,5vw,4rem) 1.25rem 5rem}
h1{font-size:clamp(1.9rem,5vw,2.8rem);line-height:1.15;letter-spacing:-.02em;
  margin:0 0 2.5rem;color:%(ink)s}
h2{font-size:clamp(1.25rem,3.2vw,1.6rem);line-height:1.25;letter-spacing:-.01em;
  margin:3rem 0 .9rem;color:%(honey)s}
h3{font-size:1.1rem;margin:2rem 0 .6rem;color:%(ink)s}
p{margin:0 0 1.1rem}
ul{margin:0 0 1.3rem;padding-left:1.3rem}li{margin:.35rem 0}
.q{font-weight:650;margin:1.4rem 0 .5rem}
.tw{overflow-x:auto;margin:0 0 1.6rem;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%%;min-width:22rem;font-size:.94rem;
  background:#fff}
th,td{border:1px solid %(line)s;padding:.55rem .8rem;text-align:left;
  vertical-align:top}
th{background:%(paper2)s;font-weight:700;white-space:nowrap}
figure{margin:0 0 1.8rem}
img{max-width:100%%;height:auto;display:block;margin:0 auto}
hr{border:0;border-top:1px solid %(line)s;margin:3rem 0}
.note{color:%(soft)s;font-size:.86rem}
/* `"page_break": true` on a section. Only means anything on paper, which is
   now every PDF — the html build is what chromium prints. */
.pb{break-before:page;page-break-before:always}
/* Printing is a first-class output, not an afterthought: the Designer's
   pieces (posters, invitations, CVs) are produced as HTML precisely
   so the design survives, and the browser's Print → Save as PDF is how they
   reach paper. Without @page the browser adds its own margins on top of the
   layout and drops every background colour. */
@page{size:letter;margin:12mm}
@media print{
  body{background:#fff;color:#000}
  .wrap{max-width:none;margin:0;padding:0}
  h2{break-after:avoid;page-break-after:avoid}
  figure,table,.tw{break-inside:avoid;page-break-inside:avoid}
  a{text-decoration:none;color:inherit}
  /* Backgrounds are dropped by default; a designed piece needs them. */
  *{-webkit-print-color-adjust:exact;print-color-adjust:exact}
}
/* Screen only. Headless chromium renders with whatever colour scheme the
   environment reports, and this block sits after the print one — so a dark
   preference used to win and print a dark-brown page onto white paper. */
@media screen and (prefers-color-scheme:dark){
  body{background:#1a1714;color:#ece5d8}
  h1,h3{color:#ece5d8}
  th{background:#272219}td,th{border-color:#3a332a}table{background:#221e18}
  .note{color:#a99f8d}
}
"""


def _build_html(data, out_path, tmp_imgs):
    """A standalone page: no external CSS, no fonts to fetch, no scripts.

    Everything is inlined (charts become data URIs) so the file works opened
    from the share, mailed as an attachment, or served from HomeCore's sandboxed
    preview route — which blocks outbound requests anyway.
    """
    raw = data.get("html")
    if raw and str(raw).strip():
        # The designer wrote real HTML. Pass it through untouched — that control
        # is the entire reason this format exists — only wrapping a bare
        # fragment so it is still a valid document.
        raw = str(raw)
        if "<html" not in raw.lower():
            raw = ('<!doctype html><html lang="es"><head><meta charset="utf-8">'
                   '<meta name="viewport" content="width=device-width,initial-scale=1">'
                   f'<title>{_html_escape(str(data.get("title") or out_path.stem))}</title>'
                   f"</head><body>{raw}</body></html>")
        Path(out_path).write_text(raw, encoding="utf-8")
        return

    title = str(data.get("title") or out_path.stem)
    body = []
    if data.get("title"):
        body.append(f"<h1>{_html_escape(title)}</h1>")

    for i, sec in enumerate(data.get("sections", [])):
        # A section that starts a new page. Emitted as an empty marker rather
        # than a class on the heading, because a section may have no heading.
        if sec.get("page_break") and i:
            body.append('<div class="pb"></div>')

        heading = sec.get("heading")
        if heading:
            level = min(int(sec.get("level", 1) or 1) + 1, 3)
            body.append(f"<h{level}>{_html_escape(str(heading))}</h{level}>")

        text = sec.get("text") or sec.get("paragraph")
        if text:
            body += [f"<p>{_html_escape(p)}</p>" for p in _paragraphs(text)]

        if "questions" in sec:
            for q in sec["questions"]:
                if q.get("question"):
                    body.append(f'<p class="q">{_html_escape(str(q["question"]))}</p>')
                items = q.get("items", [])
                if items:
                    body.append("<ul>" + "".join(
                        f"<li>{_html_escape(str(it))}</li>" for it in items) + "</ul>")

        if "items" in sec and "questions" not in sec:
            body.append("<ul>" + "".join(
                f"<li>{_html_escape(str(it))}</li>" for it in sec["items"]) + "</ul>")

        if "table" in sec:
            tbl = sec["table"]
            headers = tbl.get("headers", [])
            rows = tbl.get("rows", [])
            if headers or rows:
                out = ['<div class="tw"><table>']
                if headers:
                    out.append("<thead><tr>" + "".join(
                        f"<th>{_html_escape(str(h))}</th>" for h in headers) + "</tr></thead>")
                out.append("<tbody>" + "".join(
                    "<tr>" + "".join(f"<td>{_html_escape(str(c))}</td>" for c in row) + "</tr>"
                    for row in rows) + "</tbody>")
                out.append("</table></div>")
                body.append("".join(out))

        for kind, path, fallback in _section_images(sec, i, tmp_imgs):
            uri = _data_uri(path) if path else ""
            if uri:
                alt = _html_escape(str(sec.get("heading") or kind))
                body.append(f'<figure><img src="{uri}" alt="{alt}"></figure>')
            else:
                body.append(f'<p class="note">{_html_escape(fallback)}</p>')

    css = _HTML_CSS % PALETTE
    Path(out_path).write_text(
        '<!doctype html>\n<html lang="es">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>{_html_escape(title)}</title>\n<style>{css}</style>\n</head>\n"
        f'<body>\n<div class="wrap">\n{chr(10).join(body)}\n</div>\n</body>\n</html>\n',
        encoding="utf-8")


# --- html -> pdf --------------------------------------------------------------
# A designed piece is written as HTML because that is the only format where the
# model controls type, colour and layout. Printing it needs a renderer that
# understands the same CSS the designer wrote — grid, flex, print-color-adjust —
# so this drives a real browser and does not try to re-implement one.
#
# Chromium's own CLI does this with no Node and no Playwright: --print-to-pdf on
# a file:// URL. The candidates cover the Debian package, the Playwright cache
# (handy on a dev box) and a system Chrome.
_CHROME_CANDIDATES = (
    "chromium", "chromium-browser", "google-chrome-stable", "google-chrome",
    "headless_shell", "chrome",
)
_PDF_RENDER_TIMEOUT_S = 120


def _find_browser():
    for name in _CHROME_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    # Playwright keeps its browsers outside PATH.
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
                 or (Path.home() / ".cache" / "ms-playwright"))
    if cache.is_dir():
        for pat in ("chromium-*/chrome-linux64/chrome",
                    "chromium-*/chrome-linux/chrome",
                    "chromium_headless_shell-*/chrome-headless-shell-linux64/headless_shell",
                    "chromium_headless_shell-*/chrome-linux/headless_shell"):
            hits = sorted(cache.glob(pat))
            if hits:
                return str(hits[-1])
    return None


def _html_to_pdf(html_path, pdf_path):
    """Render a saved HTML file to PDF. (ok, error).

    Best-effort on purpose: the HTML is the deliverable and it already exists by
    the time this runs. A box with no browser installed should still hand over
    the page, with a line saying the PDF could not be made — not lose both.
    """
    browser = _find_browser()
    if browser:
        try:
            r = subprocess.run(
                [browser, "--headless", "--disable-gpu", "--no-sandbox",
                 "--no-pdf-header-footer", "--run-all-compositor-stages-before-draw",
                 "--virtual-time-budget=8000",
                 f"--print-to-pdf={pdf_path}", Path(html_path).resolve().as_uri()],
                capture_output=True, text=True, timeout=_PDF_RENDER_TIMEOUT_S)
            if Path(pdf_path).exists() and Path(pdf_path).stat().st_size > 800:
                return True, None
            return False, (r.stderr or r.stdout or "el navegador no produjo PDF")[-200:]
        except subprocess.TimeoutExpired:
            return False, f"the browser took longer than {_PDF_RENDER_TIMEOUT_S}s"
        except Exception as e:
            return False, str(e)
    try:
        # Lighter fallback. Its CSS support is narrower than a browser's — grid
        # especially — so the PDF can differ from what the page shows. Better
        # than nothing, and the caller is told which one produced it.
        from weasyprint import HTML as _WeasyHTML
        _WeasyHTML(filename=str(html_path)).write_pdf(str(pdf_path))
        return True, "weasyprint"
    except ImportError:
        return False, "no hay navegador ni weasyprint para renderizar el PDF"
    except Exception as e:
        return False, str(e)


def _build_pdf_page(data, out_path, tmp_imgs):
    """A PDF is a rendered web page, not a second layout engine.

    reportlab understood a strict subset of what the model can express: heading,
    paragraph, bullet, table, image, stacked in that order. Everything else —
    colour, a designed poster, two columns, the `html` escape hatch — was
    silently dropped, which is why "hazme un PDF" kept coming back looking like
    a memo no matter what was asked for. The HTML builder already handles every
    section type *and* accepts raw markup, and chromium prints it exactly as
    designed, so there is no reason to keep a second, weaker path in front.

    The HTML is written next to the PDF and kept. It costs nothing, and it is
    what makes a correction cheap: the next call can edit that markup instead of
    rebuilding the document from the section list.

    reportlab stays as the fallback for a box with no browser. A plain document
    beats no document — but it is reported, because the difference is visible
    on the page and nowhere else.
    """
    html_path = Path(out_path).with_suffix(".html")
    _build_html(data, html_path, tmp_imgs)
    ok, note = _html_to_pdf(html_path, out_path)
    if ok:
        return {"pdf_renderer": note or "chromium",
                "html_file": str(html_path.relative_to(WORKSPACE))}
    try:
        __import__("reportlab")
    except ImportError:
        raise RuntimeError(
            f"no hay navegador para renderizar el PDF ({note}) y reportlab "
            f"is not installed either")
    _build_pdf(data, out_path, tmp_imgs)
    return {"pdf_renderer": "reportlab", "pdf_degraded": note}


BUILDERS = {"docx": _build_docx, "pdf": _build_pdf_page, "xlsx": _build_xlsx,
            "pptx": _build_pptx, "html": _build_html}

# What each renderer needs, so a missing library is reported by name instead of
# surfacing as an ImportError traceback the model then tries to debug.
# `pdf` asks for nothing: it renders HTML with a browser, and only reaches for
# reportlab if there isn't one — which `_build_pdf_page` reports itself.
_REQUIRES = {"docx": ("docx", "python-docx"), "pdf": (None, None),
             "xlsx": ("openpyxl", "openpyxl"), "pptx": ("pptx", "python-pptx"),
             "html": (None, None)}

# Label plus the participle that agrees with it — "Planilla creado" is what a
# shared `f"{label} creado"` produces for three of the five, and this line is
# relayed to the family verbatim (see SKILL.md's Output section).
KIND_LABEL = {"docx": "Documento creado", "pdf": "PDF creado",
              "xlsx": "Spreadsheet created", "pptx": "Presentation created",
              "html": "Web page created"}


# Keys a model reaches for instead of the documented ones. Accepting the
# near-misses is cheaper than a failed call and a retry, and they are
# unambiguous: nothing else in the schema is called `slides` or `bullets`.
_SECTION_ALIASES = ("slides", "pages", "chapters")
_ITEMS_ALIASES = ("bullets", "points", "list")
# What makes a section worth rendering. A heading alone is not content — a deck
# of empty titled slides is exactly the "it generated an empty pptx" report this
# guards against.
_CONTENT_KEYS = ("text", "paragraph", "items", "questions", "table",
                 "chart", "chart_type", "data", "formula", "image")


def _normalize(data):
    """Return *data* with the shapes a model actually emits mapped onto the
    documented ones. Never invents content — only renames and re-wraps."""
    out = dict(data)
    if not out.get("sections"):
        for alias in _SECTION_ALIASES:
            if out.get(alias):
                out["sections"] = out.pop(alias)
                break
    sections = []
    for sec in out.get("sections") or []:
        if isinstance(sec, str):
            # `sections: ["primero", "segundo"]` used to raise AttributeError
            # halfway through the build and lose the whole document.
            sections.append({"text": sec})
            continue
        if not isinstance(sec, dict):
            continue
        sec = dict(sec)
        if "items" not in sec:
            for alias in _ITEMS_ALIASES:
                if isinstance(sec.get(alias), list):
                    sec["items"] = sec.pop(alias)
                    break
        sections.append(sec)
    out["sections"] = sections
    return out


def _content_error(data, fmt):
    """A message naming what is missing, or None when there is something to
    render.

    Producing a file with nothing in it is worse than failing: the caller
    announces a download link, the family opens a blank deck, and nothing
    anywhere reports a problem. Refusing tells the model what to send instead.
    """
    # `html` is the designer's escape hatch, and it is content. It counts for
    # pdf as well as html now that a PDF *is* the rendered page — refusing it
    # there would reject a fully designed piece for having no `sections`.
    if fmt in ("html", "pdf", "docx") and str(data.get("html") or "").strip():
        return None
    sections = data.get("sections") or []
    if not sections:
        return ("no hay nada que poner en el documento: falta `sections`, una "
                "lista de objetos como "
                '{"heading": "...", "text": "..."} — ver la SKILL.md. '
                "No file was generated.")
    if not any(
        any(str(sec.get(k) or "").strip() if not isinstance(sec.get(k), (list, dict))
            else sec.get(k) for k in _CONTENT_KEYS)
        for sec in sections
    ):
        return (f"the {len(sections)} sections carry only `heading`, so the "
                "file would come out empty. Every section needs content: "
                "`text`, `items`, `questions`, `table`, `chart` o `image`. "
                "No file was generated.")
    return None


def create(data):
    fmt, filename = _resolve_format(data)
    data = _normalize(data)
    if problem := _content_error(data, fmt):
        return {"error": problem}

    module, package = _REQUIRES[fmt]
    if module:
        try:
            __import__(module)
        except ImportError:
            return {"error": f"{package} not installed"}

    out_path = _free_path(DOCS_DIR, filename)
    filename = out_path.name          # it may have been renamed to avoid a clash
    tmp_imgs = []
    built = {}
    try:
        built = BUILDERS[fmt](data, out_path, tmp_imgs) or {}
    except Exception as e:
        return {"error": f"no se pudo generar el {fmt}: {e}"}
    finally:
        for p in tmp_imgs:
            try:
                os.unlink(p)
            except Exception:
                pass

    share_path, share_error = _save_to_share(out_path, filename)

    def _link(name, share_rel, workspace_rel):
        """The chat link for a generated file: the share copy when there is one.

        `download:` reaches both — the share under the reader's own access, the
        workspace only under `media/` — so the fallback still delivers the file
        when the share is down. It just does not survive the container.

        A target carrying brackets goes in the angle form: every markdown
        parser downstream ends a bare URL at the first ")", so "Informe
        (final).docx" produced a link to "…/Informe (final" that failed on
        click, with the rest printed beside it as text.
        """
        target = f"download:{share_rel or workspace_rel}"
        if any(ch in target for ch in "()[]<>"):
            return f"[📥 Download {name}](<{target}>)"
        return f"[📥 Download {name}]({target})"

    # A designed piece is written as HTML — the only format where the model
    # controls type, colour and layout — and is usually also wanted on paper.
    # `render_pdf` hands back both from one call: the page as designed, and a
    # PDF of that same page rendered by a real browser. Default on for html,
    # because "hazme un afiche" nearly always means both; pass false when the
    # piece will only ever live in a browser.
    extra = {}
    if fmt == "html" and data.get("render_pdf", True):
        pdf_name = f"{os.path.splitext(filename)[0]}.pdf"
        pdf_path = DOCS_DIR / pdf_name
        ok, note = _html_to_pdf(out_path, pdf_path)
        if ok:
            # The error is kept, not dropped. When only the PDF's share write
            # failed, the reply still said "guardado en tu carpeta compartida"
            # — true of the HTML, not of the PDF — while the PDF link quietly
            # became a workspace one that dies with the container.
            pdf_share, pdf_share_error = _save_to_share(pdf_path, pdf_name)
            pdf_rel = pdf_path.relative_to(WORKSPACE)
            extra = {
                "pdf_file": str(pdf_rel),
                "pdf_share_path": pdf_share,
                "pdf_share_error": pdf_share_error,
                "pdf_download_link": _link(pdf_name, pdf_share, pdf_rel),
                "pdf_renderer": note or "chromium",
            }
        else:
            # The HTML already exists and is the deliverable; a missing renderer
            # must cost the PDF, not the piece.
            extra = {"pdf_error": note}

    rel = str(out_path.relative_to(WORKSPACE))
    link = _link(filename, share_path, rel)
    saved_line = (
        f"Saved to your shared folder: {share_path}" if share_path
        else f"(No se pudo guardar en la carpeta compartida: {share_error})"
    )
    # Said separately, because the two files can land differently: the piece
    # filed and its PDF not. One line reporting only the first read as though
    # both had.
    if extra.get("pdf_share_path"):
        saved_line += f"\nY el PDF: {extra['pdf_share_path']}"
    elif extra.get("pdf_share_error"):
        saved_line += ("\n(El PDF no se pudo guardar en la carpeta compartida: "
                       f"{extra['pdf_share_error']})")
    return {
        "format": fmt,
        # What the builder reported about itself — which renderer drew the PDF,
        # and the HTML it came from. Deliberately not in `message`: a PDF was
        # asked for, so exactly one link goes back. Two links per document is
        # how a reply ends up offering the guide and the answer key and linking
        # the same file twice.
        **built,
        **extra,
        "file": rel,
        "saved_to": str(out_path),
        "share_path": share_path,
        "share_error": share_error,
        "download_link": link,
        "saved_line": saved_line,
        "message": "\n".join(filter(None, [
            f"{KIND_LABEL[fmt]} exitosamente.", link,
            extra.get("pdf_download_link"), saved_line,
            # A caveat the person has to hear, not one the model may keep to
            # itself: a poster converted to docx is its own text, and someone
            # who opens it expecting the design has to know that beforehand.
            f"Nota: {built['note']}" if built.get("note") else None,
            f"(No se pudo generar el PDF: {extra['pdf_error']})"
            if extra.get("pdf_error") else None,
        ])),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: create_doc.py '<json>'"}))
        sys.exit(1)
    try:
        data = json.loads(sys.argv[1])
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid JSON: {e}"}))
        sys.exit(1)
    result = create(data)
    if "error" in result:
        print(json.dumps(result))
        sys.exit(1)
    print(result["download_link"])
    if result.get("pdf_download_link"):
        print(result["pdf_download_link"])
    elif result.get("pdf_error"):
        print(f"(No se pudo generar el PDF: {result['pdf_error']})")
    print(result["saved_line"])
