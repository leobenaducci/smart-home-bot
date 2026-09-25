"""Getting out of a profession, and back to the ordinary chat.

Run: python local/test_chat_nav.py   (needs Flask + Jinja; skips loudly without)

Three things that were each individually defensible and together made leaving a
persona awkward:

* the header's back arrow always went to `/`, whatever you were in. From a
  profession -- which is *opened from* the ordinary chat -- "back" pointing at
  the house dashboard is a lie about where you came from, and it was the only
  visible way out, so people pressed it to reach Alfred and left the chat.
* Alfred was filed inside the «Profesiones» group. He is not a profession; he
  is what the chat is when you have not chosen one. The way back to the
  ordinary conversation was two taps inside a menu named after the things it
  is not.
* nothing in the collapsed menu said which profession you were in.

The assertions are about *destinations and marking*, not wording: a rewrite may
rename anything, but the arrow may not go to the house from inside a persona,
and Alfred may not go back inside the group.
"""
import json
import os
import re
import sys
from pathlib import Path

LOCAL = Path(__file__).resolve().parent
try:
    from jinja2 import Environment, FileSystemLoader, Undefined
except ImportError:
    print("SKIP: Jinja2 is not installed here.")
    raise SystemExit(0)

# Beside the service first, then the repo -- the same two routes to the same
# catalogues that `test_dashboard.py` documents. The deployer copies `i18n/`
# into the build context, so inside the built image it sits next to app.py;
# climbing four levels from there lands on `/` and finds nothing, which is a
# test that fails only during a deploy and passes every time you run it by hand.
_CAT_DIRS = (LOCAL / "i18n", LOCAL.parent.parent.parent / "i18n")
for _d in _CAT_DIRS:
    if (_d / "es.json").is_file():
        CAT = json.loads((_d / "es.json").read_text(encoding="utf-8"))
        break
else:
    print("SKIP: no i18n catalogue beside the service or in the repo.")
    raise SystemExit(0)


class Anything(Undefined):
    def __str__(self): return ""
    __repr__ = __str__
    def __call__(self, *a, **k): return self
    def __getattr__(self, n): return self
    def __getitem__(self, k): return self
    def __iter__(self): return iter(())
    def __bool__(self): return False


def t(key, **kw):
    s = CAT.get(key, key)
    for k, v in kw.items():
        s = s.replace("{%s}" % k, str(v))
    return s


env = Environment(loader=FileSystemLoader(str(LOCAL / "templates")),
                  undefined=Anything)
env.filters["tojson"] = lambda v, **k: json.dumps(
    "" if isinstance(v, Anything) else v)
env.globals.update(t=t, url_for=lambda *a, **k: "#", session={},
                   request=Anything())


def render(space):
    return env.get_template("chat.html").render(
        t=t, space=space, space_title="Programador" if space else "",
        space_icon="💻" if space else "", space_mode_label="", is_admin=True,
        has_projects=True, build="b1", site_name="Mi Casa", embed=False,
        lang="es", user="u", professions=[
            {"key": "programmer", "url": "programador", "icon": "💻",
             "title": "Programador", "hint": "code"},
            {"key": "designer", "url": "disenador", "icon": "🎨",
             "title": "Diseñador", "hint": "art"}])


failures = []


def check(name, ok, why=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else "  -- " + why))
    if not ok:
        failures.append(name)


persona, plain = render("programmer"), render("")

# The header's back link. Matched on the <a> that carries the arrow.
def back_href(html):
    m = re.search(r'<a\s+href="([^"]+)"[^>]*>\s*&#8592;', html) or \
        re.search(r'<a[^>]*href="([^"]+)"[^>]*>\s*&#8592;', html)
    return m.group(1) if m else None


check("from a profession, back goes to the ordinary chat",
      back_href(persona) == "/chat",
      f"it goes to {back_href(persona)!r}; from inside a persona that is not "
      "where you came from, and it is the only visible way out")
check("from plain Alfred, back goes to the house",
      back_href(plain) == "/",
      f"it goes to {back_href(plain)!r}; Alfred is the top of the chat")

# Alfred, out of the professions group and above it.
group = re.search(r'<div class="menu-group" id="prof-group">(.*?)</div>',
                  persona, re.S)
check("there is still a professions group", bool(group))
if group:
    check("Alfred is not inside it",
          'href="/chat"' not in group.group(1),
          "filed under «Profesiones» he is two taps away and mis-labelled")
    check("and the professions still are",
          'href="/chat/programador"' in group.group(1))

before_group = persona.split('data-group="prof-group"')[0]
check("Alfred is offered above the group",
      'href="/chat"' in before_group,
      "the way back to the ordinary conversation has to be visible")

# Which one you are in, without opening anything.
check("the collapsed group names the profession you are in",
      re.search(r'Profesiones\s*<span class="app-here">Programador</span>', persona)
      is not None,
      "a collapsed menu that does not say where you are makes you open it to find out")
check("and on plain Alfred it does not claim one",
      re.search(r'Profesiones\s*<span class="app-here">', plain) is None)
check("Alfred is marked when you are in him",
      re.search(r'href="/chat"[^>]*>\s*🤵 Alfred\s*<span class="app-here">', plain)
      is not None)

print()
if failures:
    print(f"{len(failures)} check(s) failed")
    sys.exit(1)
print("all checks passed")
