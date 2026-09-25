"""Every page's inline JavaScript has to parse.

Run: python local/test_templates_parse.py   (needs node; skips loudly without)

One unescaped apostrophe -- `say('… don't save …')` -- ended a string early and
took the whole `<script>` block with it. The page still rendered: the HTML was
fine, the CSS and fonts loaded, and the chat looked exactly like the chat. None
of its JavaScript ran, so it fetched no history, opened no event stream and
sent no message. From the outside that is "I see Alfred's chat, but nothing
works inside", and from the server it is a page served 200 with no API calls
after it -- which is why every check that asked the server whether it was
healthy said yes.

Templates are rendered first, because a `{{ … }}` that expands to a quote or a
newline breaks the same way and would not be visible in the source file.

Deliberately not a linter. The only question here is whether a browser can
parse it at all, which is the failure that takes everything with it.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"

if not shutil.which("node"):
    print("SKIP: no node here — this test needs a JavaScript parser.")
    raise SystemExit(0)

try:
    from jinja2 import Environment, FileSystemLoader, Undefined
except ImportError:
    print("SKIP: Jinja2 is not installed here.")
    raise SystemExit(0)


class Anything(Undefined):
    """Every unknown name renders as something short and harmless.

    The point is to exercise the *shape* of the template, not its data: an
    undefined that raised would stop at the first one, and an undefined that
    rendered as nothing would hide a `'{{ x }}'` that needs quoting.
    """
    def __str__(self):
        return "x"
    __repr__ = __str__
    def __call__(self, *a, **k):
        return self
    def __getattr__(self, name):
        return self
    def __getitem__(self, key):
        return self
    def __iter__(self):
        return iter(())
    def __bool__(self):
        return False


env = Environment(loader=FileSystemLoader(str(TEMPLATES)), undefined=Anything)
env.globals.update(t=lambda key, **kw: key, csrf_token=lambda: "x",
                   url_for=lambda *a, **k: "/", locale="en")


def _tojson(value, **kw):
    """`|tojson` on a value the page would really have.

    Jinja's own filter refuses an Undefined, and several pages hand it whole
    objects. What is being checked here is the JavaScript around the hole, so
    the hole becomes an empty object -- which is what an absent list or dict
    renders as anyway, and is valid wherever the real value would have been.
    """
    import json
    try:
        return json.dumps(value)
    except TypeError:
        return "{}"


env.filters["tojson"] = _tojson

SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)
failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


print("every page's inline JavaScript parses")
pages = sorted(p for p in TEMPLATES.glob("*.html"))
check("there are pages to check", len(pages) > 3, len(pages))

for page in pages:
    try:
        html = env.get_template(page.name).render()
    except Exception as exc:  # noqa: BLE001 - a template that will not render
        check(f"  {page.name} renders", False, f"{type(exc).__name__}: {exc}")
        continue
    blocks = [b for b in SCRIPT.findall(html) if b.strip()]
    bad = []
    for i, code in enumerate(blocks):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(code)
            path = fh.name
        result = subprocess.run(["node", "--check", path],
                                capture_output=True, text=True)
        os.unlink(path)
        if result.returncode != 0:
            first = next((l for l in result.stderr.splitlines()
                          if "SyntaxError" in l), result.stderr.strip()[:120])
            bad.append(f"block {i}: {first}")
    check(f"  {page.name} ({len(blocks)} block(s))", not bad, "; ".join(bad))

    # Parsing is not enough, and this is where a stronger check stops being
    # possible here. `credentials.html` parsed perfectly and was dead: it read
    # `PROJECTS_READY`, which the route passed to the template and the template
    # never turned into anything JavaScript could see. The first statement to
    # touch it threw ReferenceError, took the rest of the script with it --
    # including the fetch that loads the page's content -- and the page then
    # showed its own empty state. Served 200, looked right, had never once
    # listed anything.
    #
    # A check for "every SHOUTING_CASE name a script reads is declared in it"
    # was written and removed. Scanning raw text reads prose as code (`CSS` in a
    # sentence, `UTC` in a date format, base64 inside a data: URI); stripping
    # comments and string literals first fixes that and then desynchronises on
    # regex literals, because `/[^']/` opens a quote that never closes and
    # swallows the rest of the file. Telling a regex literal from a division
    # needs parser context, and node --check will not report an undefined name
    # because that is not a syntax error. It reported nine false failures on
    # chat.html, every one of them a declared variable, which is the kind of
    # check people learn to ignore.
    #
    # So this suite still only promises that the scripts *parse*. Whoever
    # replaces this with something real should reach for a JS parser rather
    # than another regex.

print()
print("no script reaches for an element the page does not have")

# The check the note above says to reach for a parser to write -- except this
# one needs no parser, because `getElementById('literal')` is unambiguous.
#
# Written after shipping the fault it catches: `credentials.html` called
# `getElementById('auth-user-field').hidden = ...` for a field that was never
# added to the markup. It parsed, the page loaded, and then every save, cancel
# and type-change threw on `null.hidden` -- so the form silently stopped
# resetting and the SSH-only fields stopped hiding. `node --check` cannot see
# that, and the verification that missed it grepped for a string that appears
# in the markup AND in the script.
#
# The template *source*, not the render: a `{% if %}` block that a stub render
# drops takes its ids with it, which is what made an earlier version of this
# report four pages' worth of ids that were plainly there. Ids a script assigns
# to elements it builds itself are counted too.
_GET = re.compile(r"""getElementById\(\s*['"]([A-Za-z0-9_\-]+)['"]\s*\)""")
_IDATTR = re.compile(r"""\bid\s*=\s*["']([A-Za-z0-9_\-]+)["']""")
_ASSIGN = re.compile(r"""\.id\s*=\s*['"]([A-Za-z0-9_\-]+)['"]""")
# Whatever a page inherits is part of it: `_config_base.html` holds the toast
# and the shared helpers every config page calls into.
_BASE = "".join(p.read_text(encoding="utf-8") for p in sorted(TEMPLATES.glob("_*.html")))
for page in pages:
    _src = page.read_text(encoding="utf-8") + _BASE
    _missing = sorted(set(_GET.findall(_src)) - set(_IDATTR.findall(_src))
                      - set(_ASSIGN.findall(_src)))
    check(f"  {page.name}", not _missing, ", ".join(_missing[:6]))

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")
