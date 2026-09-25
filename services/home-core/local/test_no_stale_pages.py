"""No page this app renders may be stored.

Run: python local/test_no_stale_pages.py   (needs Flask; skips loudly without it)

Reported from a phone on 5G, 2026-08-18: the Apps menu was right on first load,
then a trip to /luces and back brought the Cámaras tile back and lost Alfred's
voice, WhatsApp and Consumo de Alfred.

It reads like a permissions bug and is not one. A tile that had been *removed*
reappearing, together with three that had been *added* going missing, is one
page from before the deploy — a cached copy, served on back-navigation, which is
exactly when a WebView reaches for the cache instead of the network.

Every page here is specific to three things at once, and none of them is in the
URL:

- **who** is logged in — the history, the CSRF token, the member's theme;
- **which build** is deployed — `BUILD_TIME` is rendered into the page;
- **which side of the split horizon** the request came through, since the
  house-only tiles started depending on it.

`Vary: Cookie` was all the chat page carried and it cannot express any of that:
the cookie is byte-identical on wifi and on mobile data, and identical across a
deploy. So the only honest answer is not to store the page at all.

The guard itself was never in question — /camaras stays a 404 off the VPN
whatever a cache shows — so the worst this bug could do was offer a dead tile.
It is still the kind of wrong that costs an evening to diagnose, because the
page looks like a plausible older *version of the software* rather than a stale
copy of one page.

Assets are deliberately untouched — the hook keys on `text/html` — so anything
that chose its own caching keeps it.
"""
import os
import shutil
import sys
import tempfile

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-nostale-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
PROXY_SECRET = "p" * 32
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET=PROXY_SECRET,
                  DEBUG_API_KEY="d" * 32)

USER1 = "user1"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2}]' % USER1)

sys.path.insert(0, dst)
import app as A  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


client = A.app.test_client()
HOME = {"X-Proxy-Secret": PROXY_SECRET, "X-Proxy-User": USER1, "X-Proxy-Lan": "1"}
AWAY = {"X-Proxy-Secret": PROXY_SECRET, "X-Proxy-User": USER1}


def cc(path, headers=AWAY):
    r = client.get(path, headers=headers)
    val = r.headers.get('Cache-Control', '')
    r.close()
    return r.status_code, val


print("the chat page is never stored")
status, val = cc('/chat')
check("it renders", status == 200, status)
check("and says no-store", 'no-store' in val, val or '(nothing)')

print("\nnor is any other page a member navigates to")
# Each of these renders identity into the page — the theme, the member's own
# lists, their tasks. A copy of one is a copy of somebody's data.
for path in ('/login', '/'):
    status, val = cc(path)
    check(f"{path} ({status})", 'no-store' in val, val or '(nothing)')

print("\nand that is what actually fixes the reported bug")
# The same URL, two different pages, nothing in the URL to tell them apart.
# Without no-store this is precisely the pair a cache would confuse.
away = client.get('/chat', headers=AWAY)
home = client.get('/chat', headers=HOME)
a_body, h_body = away.get_data(as_text=True), home.get_data(as_text=True)
away.close(); home.close()
check("off the VPN the page has no Cámaras tile", '/camaras/' not in a_body)
check("at home it does", '/camaras/' in h_body)
check("so the two differ at the same URL", a_body != h_body)
check("and neither may be kept",
      'no-store' in away.headers.get('Cache-Control', '')
      and 'no-store' in home.headers.get('Cache-Control', ''))
# Vary: Cookie was the old answer and is not one: the cookie is identical on
# wifi and on mobile data, so a cache keyed on it holds one page for both.
check("Vary alone could never have covered it",
      'Cookie' in away.headers.get('Vary', ''),
      "if this ever stops being sent, the comment above needs revisiting")

print("\nand nothing that is not a page was touched")
# The hook keys on text/html precisely so that everything else keeps whatever
# caching it chose for itself. theme.css asks for `no-cache` (revalidate, do not
# assume) and must keep asking for that, not be upgraded to no-store.
r = client.get('/theme.css', headers=AWAY)
val = r.headers.get('Cache-Control', '')
check("/theme.css keeps its own answer", val == 'no-cache', val or '(nothing)')
check("and it is not html", r.mimetype != 'text/html', r.mimetype)
r.close()

print()
shutil.rmtree(tmp, ignore_errors=True)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")


print("\nthe four config pages wear one shell, not four copies of it")
# Proyectos, Credenciales, Directorio and Correos configure the same thing from
# four sides, and were four copies of one header, one token block and one
# toast. They had already drifted once: the caret and the contrast tokens were
# fixed in two of them and not the other two, the same afternoon. The shell now
# lives in _config_base.html, which is the only place it can stay in agreement.
CONFIG_PAGES = ("projects.html", "credentials.html", "profiles.html", "mailboxes.html")
TPL = os.path.join(SRC, "templates") if "SRC" in dir() else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "templates")

for page in CONFIG_PAGES:
    body = open(os.path.join(TPL, page), encoding="utf-8").read()
    check(f"{page} extends the shell",
          body.lstrip().startswith('{% extends "_config_base.html" %}'), body[:60])
    # The four things a copy-pasted shell brings with it every time.
    for owned, what in (("--plaster:#EFE9DC", "the token block"),
                        ("<title>", "the document head"),
                        ("function toast(", "the toast"),
                        ("async function api(", "the fetch helper")):
        check(f"  and does not carry {what}", owned not in body,
              f"{page} still defines it")

base = open(os.path.join(TPL, "_config_base.html"), encoding="utf-8").read()
check("the shell defines them once", all(
    base.count(x) == 1 for x in ("--plaster:#EFE9DC", "function toast(", "async function api(")))
# A header somebody has to remember is one somebody forgets, and the failure is
# a 403 on a form that looks correct.
check("and the fetch helper carries the CSRF header itself",
      "headers['X-CSRF-Token'] = CSRF" in base)
