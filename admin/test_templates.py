"""What the admin pages must be true of, checked without a browser.

Run: ./.venv/bin/python admin/test_templates.py

Two of these are here because I broke them while tidying the markup:

- **One `class` attribute per tag.** Replacing `style="margin-top:16px"` with
  `class="stack"` on an element that already had a class produced
  `class="settings" class="stack"`. That is not an error anywhere -- the
  parser takes the first and drops the second -- so nineteen elements silently
  kept their old look and the change appeared to have worked.

- **Every class a page uses is defined.** The vocabulary lives in base.html;
  a page naming `.chip` that nobody styles renders as unstyled text, which
  looks like a design decision rather than a typo.

And one that is about the pages rather than my edits: a `{{ }}` inside an
inline `on*` handler or a `<script>` is how a rendered value ends up as
JavaScript, which is the shape that took the whole chat page down.
"""
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
BASE = TEMPLATES / "base.html"

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


pages = sorted(TEMPLATES.glob("*.html"))
print("the admin pages hold together")
check("there are pages to check", len(pages) > 5, len(pages))

TAG = re.compile(r"<[a-zA-Z][^>]*>", re.S)
CLASS = re.compile(r'\sclass="([^"]*)"')

for page in pages:
    body = page.read_text(encoding="utf-8")
    doubled = [t[:70] for t in TAG.findall(body) if len(CLASS.findall(t)) > 1]
    check(f"  {page.name}: one class attribute per tag", not doubled,
          "; ".join(doubled[:2]))

# Every class named by a page has to exist in the stylesheet base.html carries.
# Every stylesheet the pages carry, not just base.html: a page may style its
# own thing, and a check that only read the shared one would call that a typo.
styles = "\n".join(
    "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", p.read_text(encoding="utf-8"), re.S))
    for p in pages)
defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", styles))
# Classes a page invents for its own script to find, never for styling.
BEHAVIOURAL = {"rel-hint", "paths-group", "js-confirm", "js-confirm-maybe",
                "js-probe",
                   # The models page adds the rest of a role's
                   # options on request; see its expander script.
                   "js-more"}
JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)
unknown = {}
for page in pages:
    body = page.read_text(encoding="utf-8")
    for attr in CLASS.findall(body):
        # A class list can be `"settings {% if x %}warn{% endif %}"`. Strip the
        # template out first: splitting it on whitespace turns `{%`, `if` and
        # `==` into class names and buries the one real finding.
        for name in JINJA.sub(" ", attr).split():
            if name in defined or name in BEHAVIOURAL:
                continue
            unknown.setdefault(page.name, set()).add(name)
check("every class a page uses is defined in base.html", not unknown,
      {k: sorted(v) for k, v in unknown.items()})

# A rendered value inside JavaScript is the shape that killed the chat page.
for page in pages:
    body = page.read_text(encoding="utf-8")
    inline = re.findall(r'\son[a-z]+="[^"]*\{\{[^"]*"', body)
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", body, re.S)
    # `{{ x | tojson }}` is the correct way to put a value into a script: it
    # emits valid JSON, quotes and all. Anything else interpolated there is
    # the shape that ended a string early and took the chat page with it.
    in_script = [s for s in scripts
                 if re.search(r"\{\{(?![^}]*\|\s*tojson)", s)]
    check(f"  {page.name}: no rendered value inside script",
          not in_script,
          "a value that expands to a quote ends the string and takes the block")
    if inline:
        # An `onsubmit="return confirm('{{ … }}')"` is exactly that, one
        # attribute along. Reported rather than failed: it is already there and
        # the strings behind it are ours, but a new one deserves a look.
        print(f"  note  {page.name}: {len(inline)} handler(s) carry a rendered "
              f"value -- {inline[0][:52].strip()}…")

print("\nJSON never goes into a double-quoted attribute")
# `tojson` escapes < > & and ' -- not the double quote. In `data-x="{{ v | tojson }}"`
# the JSON's own first quote ends the attribute, the script's JSON.parse throws
# on load, and every button the script drives falls back to a bare form post:
# the benchmark card answered Run with a raw `{"ok":false,"reason":"busy"}`.
# Single-quoted attributes are safe, because tojson does escape those.
for _page in pages:
    _bad = re.findall(r'=\s*"[^"]*\{\{[^}]*\|\s*tojson', _page.read_text(encoding="utf-8"))
    check(f"  {_page.name}: no tojson inside a double-quoted attribute", not _bad, _bad[:1])

print("\nevery endpoint the navigation names is a route that exists")
# `url_for` raises BuildError on an unknown endpoint, and base.html builds the
# nav on *every* page -- so one missing route is not one broken link, it is a
# 500 on the whole admin.
#
# Which is what shipped: a route appended to app.py after the
# `if __name__ == "__main__":` block registers fine when the module is imported,
# and never at all under `python app.py`, because `app.run()` above it never
# returns. Importing app.py -- which is what the other checks here do -- cannot
# see that. Reading the routes out of the app and the endpoints out of the
# template can.
_nav = re.findall(r"^\s*\('([a-z_]+)',\s*'[^']*',",
                  BASE.read_text(encoding="utf-8"), re.M)
check("  the nav was parsed at all", len(_nav) >= 5, _nav)

# The routes as the app really registers them. Imported here rather than at the
# top so the checks above still run on a machine without Flask.
import os as _os, tempfile as _tf
_os.environ.setdefault("HOME_STACK_STATE_DIR", _tf.mkdtemp(prefix="navcheck-"))
sys.path.insert(0, str(HERE))
import app as _A  # noqa: E402
_routes = {r.endpoint for r in _A.app.url_map.iter_rules()}
for _endpoint in _nav:
    check(f"  {_endpoint} is a registered route", _endpoint in _routes,
          "url_for raises BuildError, and base.html renders on every page -- "
          "so this is a 500 everywhere, not a broken link")

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)

# --- does the script run after what it reaches for --------------------------
# `getElementById` at parse time returns null for an element further down the
# page, and the usual shape here is `if (!el) return;` -- so the feature simply
# does not exist, silently. The APK card's whole log viewer was dead that way:
# the script sat at line 170 and its element at line 301, and every check in
# this file passed.
print("\nevery script runs after the elements it looks up")
for _tpl in sorted(TEMPLATES.glob("*.html")):
    _text = _tpl.read_text()
    for _m in re.finditer(r"getElementById\(['\"]([\w-]+)['\"]\)", _text):
        _id = _m.group(1)
        _decl = _text.find(f'id="{_id}"')
        if _decl == -1:
            continue          # rendered elsewhere, or built by script
        check(f"{_tpl.name}: #{_id} exists before the script that reads it",
              _decl < _m.start(),
              f"id at {_decl}, lookup at {_m.start()}")

# --- do they compile at all ---------------------------------------------------
# The checks above read the templates as text: they find a class nobody defined
# and a rendered value inside a <script>, and they pass happily on a template
# Jinja cannot parse. One did -- an edit left two `{% endif %}` where one
# belonged, this suite went green, and the page 500'd on the next request with
# "Encountered unknown tag 'endif'". Compiling is the cheapest check here and the
# only one that catches that.
import jinja2  # noqa: E402

_env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(HERE / "templates")))
# Taken from the real app rather than listed here. Jinja resolves a filter at
# *compile* time in some positions -- `{% set x = y | f %}` is one -- so a
# template using a filter this environment has never heard of fails for the
# want of a filter rather than for its own syntax, which is not what this gate
# is asking. Copying app.py's own table keeps it honest the other way too: a
# filter that really does not exist still fails, and adding one to app.py no
# longer means remembering to add it here as well.
_env.filters.update(_A.app.jinja_env.filters)
_env.filters.setdefault("tojson", lambda v: v)
_env.globals.update(t=lambda *a, **k: "", url_for=lambda *a, **k: "",
                    csrf_token=lambda: "")

print("\nevery template compiles")
for _tpl in sorted(p.name for p in (HERE / "templates").glob("*.html")):
    try:
        _env.get_template(_tpl)
        check(f"{_tpl} compiles", True)
    except Exception as _exc:  # noqa: BLE001
        check(f"{_tpl} compiles", False, str(_exc)[:120])

# The gate above is not the last one, and it used to be the only one. Everything
# between it and here -- the element-ordering checks and the compile pass --
# printed FAIL and then this line, and the suite exited 0. A check that cannot
# fail the run is decorative, which is exactly what the ordering check was added
# to stop being true of the APK log viewer.
# --- nothing that matters may sit outside a block ----------------------------
#
# A child template is only its blocks. Jinja renders `{% extends %}` by filling
# the blocks the parent names; anything written outside one is dropped -- no
# warning, no error, nothing in the output.
#
# `member_profile.html` had the whole WhatsApp pairing script after its final
# `{% endblock %}`. The markup rendered, so the page showed the "Show QR"
# button, the empty `<pre>` and the help text -- and the code that binds the
# button and polls `/members/<id>/wa-qr` was never sent. Pressing it did
# nothing. Everything looked present, which is why it lasted: the page is
# exactly as complete as it should be except for the part that is invisible
# until somebody clicks.
#
# Rendering and compiling both pass on it. The file is valid Jinja and the
# orphan is valid HTML; what makes it a bug is only *where* it is.
for _tpl in sorted((HERE / "templates").glob("*.html")):
    _text = _tpl.read_text(encoding="utf-8")
    if "{% extends" not in _text:
        continue                      # a base template is legitimately outside
    _depth, _orphans, _in_comment = 0, [], False
    for _n, _line in enumerate(_text.splitlines(), 1):
        _st = _line.strip()
        # `{# … #}` spans lines, and the continuation of one is not an orphan.
        if _in_comment:
            if "#}" in _st:
                _in_comment = False
            continue
        if _st.startswith("{#") and "#}" not in _st:
            _in_comment = True
            continue
        # A top-level `{% macro %}` is a definition, not output -- it renders
        # where it is *called*, which is inside a block.
        if _st.startswith("{% macro"):
            _depth += 1
        elif _st.startswith("{% endmacro"):
            _depth = max(0, _depth - 1)
        elif _st.startswith("{% block"):
            _depth += 1
        elif _st.startswith("{% endblock"):
            _depth = max(0, _depth - 1)
        elif _depth == 0 and _st and not _st.startswith(("{#", "{% extends", "{% import",
                                                         "{% from", "{% set")):
            _orphans.append(f"{_n}: {_st[:55]}")
    check(f"{_tpl.name}: nothing outside a block",
          not _orphans,
          f"{_orphans[:4]} -- dropped silently; move it inside a block")

# --- a POST from a script has to carry the CSRF token ------------------------
#
# `guard_state_changing_requests` reads `request.form["_csrf"]`. A fetch that
# sends only a header gets 403 "This form was issued by an older page" -- and
# both hand-built POSTs on these pages did, so the WhatsApp pairing refresh had
# never once restarted a bridge. It failed silently on top of that: the handler
# never looked at `r.ok`, so the 403 ran the success path and the page sat
# showing "waiting for a code" forever.
#
# `new FormData(form)` is the other correct answer -- it carries the form's own
# hidden `_csrf` input -- so it passes, provided the page really has one. And a
# `.catch()` on the chain counts as checking the response: a 403 body is not
# JSON, so `r.json()` rejects and the catch is what runs.
for _page in pages:
    _body = _page.read_text(encoding="utf-8")
    _has_field = 'name="_csrf"' in _body
    for _script in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                              _body, re.S):
        for _m in re.finditer(r"fetch\(", _script):
            _call = _script[_m.start():_m.start() + 420]
            if not re.search(r"method:\s*'POST'", _call):
                continue
            _carries = ("_csrf" in _call
                        or ("FormData(" in _call and _has_field))
            check(f"  {_page.name}: a scripted POST carries the CSRF token",
                  _carries,
                  "the guard reads request.form['_csrf']; a header alone is a 403")
            # The whole chain, not the call: `.catch()` lands after the
            # handlers and is routinely a few hundred characters further on.
            check(f"  {_page.name}: and does not treat a refusal as success",
                  "r.ok" in _script or "response.ok" in _script
                  or ".catch(" in _script,
                  "without it a 403 runs the success path and the page waits "
                  "on something that never started")

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)

print("all checks passed")
