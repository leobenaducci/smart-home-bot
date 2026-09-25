"""The cameras stay in the house.

Run: python local/test_house_only.py   (needs Flask; skips loudly without it)

One page is for the house and nowhere else: `/camaras`, a live view into the
rooms. It used to be reachable from anywhere with a login, because the app talks to
`chat.home` even on the home wifi and the proxy could not tell the two
sides of the split horizon apart. A password on a public host is not the
boundary that belongs around a camera feed.

The rule now has three parts and this pins all three, because each one alone
fails quietly:

- home-chat refuses the prefix unless it is the copy running on hub
  (`server/test_proxy_routes.py` is that half);
- **this app refuses it too**, so adding a route over there can never publish
  the cameras by accident;
- and the Apps menu leaves the tiles out, so nobody is offered a link that
  404s.

The direction of failure is the point. `_at_home()` answers from a header only
the proxy may set — it is stripped from anything a client sends, alongside
X-Proxy-Secret — and when the header is missing the answer is "not at home".
A phone on mobile data cannot claim otherwise; the worst a bug here can do is
hide the cameras from somebody sitting in the living room, which is visible the
moment it happens.

`/stats` is deliberately *not* house-only, and that is checked too: the token
numbers are wanted from anywhere.
"""
import json
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

tmp = tempfile.mkdtemp(prefix="homecore-houseonly-")
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


HOME = {"X-Proxy-Secret": PROXY_SECRET, "X-Proxy-User": USER1, "X-Proxy-Lan": "1"}
AWAY = {"X-Proxy-Secret": PROXY_SECRET, "X-Proxy-User": USER1}
client = A.app.test_client()


def at_home(headers):
    """What `_at_home()` answers for a request carrying `headers`."""
    with A.app.test_request_context("/", headers=headers):
        A._proxy_auth()
        return A._at_home()


print("who counts as being at home")
check("the copy on hub does", at_home(HOME) is True)
check("the VPS does not", at_home(AWAY) is False)
check("a browser on the LAN, no proxy at all, does", at_home({}) is True)

print("\nand the header is the proxy's to set, not a client's")
# _DROP_REQUEST_HEADERS in home-chat strips it on the way in, so this can only
# arrive from the proxy itself. Pinned here as well because the consequence
# lands in this file: a phone that could set it would have the cameras.
forged = dict(AWAY, **{"X-Proxy-Lan": "1"})
check("a forged header only helps if the proxy passed it through",
      at_home(forged) is True,
      "this is the trusted-hop assumption; the strip is in home-chat")
_services = os.path.dirname(os.path.dirname(SRC))
proxy_src = os.path.join(_services, "proxy", "server", "main.py")
# Asserted, not skipped -- but only where the assertion means something. In this
# repository the proxy is services/proxy/, so a missing file there is a stale
# path rather than an absent sibling, and that is worth failing over.
#
# Inside the service's own image there is no `services/` at all: the deploy gate
# runs this against the built container, where the premise of that assertion is
# simply false. Told apart by whether the sibling *directory* exists, so a stale
# path in a checkout still fails exactly as before.
_in_checkout = os.path.isdir(_services) and os.path.isdir(
    os.path.join(_services, "home-core"))
if _in_checkout:
    check("the proxy source is where this expects it", os.path.isfile(proxy_src),
          proxy_src)
else:
    print("  SKIP  the proxy half: no sibling services/ here, so this is the "
          "service image rather than a checkout")
if _in_checkout and os.path.isfile(proxy_src):
    src = open(proxy_src, encoding="utf-8").read()
    check("and the proxy does strip it", '"x-proxy-lan"' in src,
          "add it to _DROP_REQUEST_HEADERS or a phone can claim to be at home")

print("\nthe house-only page is refused from outside")
for path in ("/camaras/", "/camaras/api/cams"):
    r = client.get(path, headers=AWAY)
    check(f"{path} -> 404", r.status_code == 404, r.status_code)

print("\nand answer normally from inside")
for path in ("/camaras/",):
    r = client.get(path, headers=HOME)
    # Whatever the upstream does (it is not running here), the one answer that
    # would be wrong is our own 404.
    check(f"{path} is not refused", r.status_code != 404, r.status_code)
    r.close()   # streamed from an upstream that is not running here

print("\nthe Apps menu offers what the request can actually open")
with A.app.test_request_context("/chat", headers=AWAY):
    A._proxy_auth()
    names = [s["name"] for s in A._chat_external_links()]
check("no Camaras tile from outside", "Cameras" not in names, names)
check("the modules are panels, not links", "Files" not in names, names)

with A.app.test_request_context("/chat", headers=HOME):
    A._proxy_auth()
    names = [s["name"] for s in A._chat_external_links()]
check("the camera tile is offered at home", "Cameras" in names, names)

print("\nthe menu can be re-asked when the phone changes networks")
# Rendered at load, the menu is right until the phone moves under it: wifi to 5G
# is not a reload. Re-asking works because the ask is itself routed by the
# network it is made on — at home the copy on hub answers, outside the VPS
# does, and each reports only what it will actually serve. A client-side ".home
# ping" answers a different question: reachability is not the test, since the
# tile has to be served by the copy with the cameras behind it.
r = client.get('/chat/apps', headers=AWAY)
check("the menu can be re-asked at all", r.status_code == 200,
      f"{r.status_code} {r.get_data(as_text=True)[:120]}")
names = [l['name'] for l in (r.get_json() or {}).get('links', [])]
check("asked from outside it offers no house-only tile",
      'Cameras' not in names, names)
check("and says so plainly", r.get_json()['at_home'] is False, r.get_json())
r = client.get('/chat/apps', headers=HOME)
names = [l['name'] for l in r.get_json()['links']]
check("asked from the house it offers the camera tile too",
      'Cameras' in names, names)
# Against the list that has something in it. This ran on the away list, which is
# empty, so the generator never iterated and the check could not fail -- and
# `l['name']` over a list of strings would have raised the moment it did. The
# duplication it guards against is only possible where the modules could appear,
# which is here.
check("the modules are not duplicated as links",
      not any(n in ('Files', 'Chores', 'Shopping', 'Menu') for n in names), names)
check("and says so", r.get_json()['at_home'] is True, r.get_json())
check("every link carries what the menu needs to draw it",
      all({'name', 'url', 'icon', 'description'} <= set(l)
          for l in client.get('/chat/apps', headers=HOME).get_json()['links']))
# The wrapper has to exist even when the server drew nothing, or a repaint that
# finds tiles has nowhere to put them.
page = open(os.path.join(SRC, "templates", "chat.html"), encoding="utf-8").read()
check("the group's wrapper is always in the page", 'id="casa-wrap"' in page)
check("and it is hidden rather than omitted when empty",
      '{% if not links %}hidden{% endif %}' in page)
check("the repaint is a repaint, not a reload",
      'refreshCasaLinks' in page and 'location.reload' not in page,
      "a reload mid-answer would throw the answer away")
# The household modules live as embedded panels in the same menu — that is
# where the menu's "not duplicated as links" above points. Pin the other half
# of the contract so one can never come back without the other.
check("and the household modules are the embedded panels",
      all(f'data-app="{k}"' in page for k in ('tasks', 'files', 'grocery', 'menu')),
      [k for k in ('tasks', 'files', 'grocery', 'menu')
       if f'data-app="{k}"' not in page])

print("\nand the pages that are not house-only stay that way")
# /stats is the one Alex asked for by name: debug numbers, no camera and no
# money, wanted from the bus like anything else.
# Cameras comes from the household's setting (`house_only:` in the manifest);
# Code is house-only by construction, because `dns.code` is a `.home` name that
# the household's own resolver answers and nothing else does. Away from the
# house it resolves nowhere, and a menu entry leading to a browser error reads
# as the service being broken. Anything *else* appearing here is a page that
# quietly stopped working from the bus, which is what this check is for.
check("the house-only list is the camera page",
      set(A.CHAT_HOUSE_ONLY_LINKS) == {"Cameras"},
      A.CHAT_HOUSE_ONLY_LINKS)
# And Code is not a link at all any more -- it is a tool the Programmer uses,
# not a door the household walks through. Checked by name rather than by count
# so that adding some other link cannot quietly put this one back.
check("and Code is no longer offered as a link",
      "Code" not in {l["name"] for l in A.CHAT_APP_LINKS},
      [l["name"] for l in A.CHAT_APP_LINKS])
r = client.get("/stats", headers=AWAY)
check("/stats is not refused from outside", r.status_code != 404, r.status_code)
r.close()

# --- the same rule, for extensions ------------------------------------------
# A plugin tile can ask for the Apps menu (`menu: casa`), and one that is only
# reachable on the house network says so with `lan_only`. Off the network it is
# *dropped*, not merely badged: the badge is honest about where a link works,
# but leaving the entry there is not, because tapping it in the app is a
# spinner and then nothing -- and the phone cannot tell that from the service
# being down. Cameras has always been dropped this way; this is the same rule
# reaching the extensions beside it.
print("\nan extension that only works at home is not offered from away")


def menu_from(headers, tiles):
    """The Apps-menu groups for a request carrying *headers*.

    Through a real services.json rather than by pinning the cache: the cache is
    keyed on the file's mtime and size, so anything written straight into it is
    thrown away on the next read.
    """
    services_file = os.path.join(tmp, "services.json")
    with open(services_file, "w", encoding="utf-8") as fh:
        json.dump({"groups": {"basic": [], "advanced": [], "extensions": tiles}}, fh)
    A.SERVICES_FILE = services_file
    A._dashboard_cache.update(key=None)
    with A.app.test_request_context("/", headers=headers):
        A._proxy_auth()
        return A._extension_menu_links()


_lan = {"name": "Lights", "url": "http://192.168.1.11:5010", "icon": "*",
        "menu": "casa", "lan_only": True}
_any = {"name": "Trading", "url": "https://example.invalid", "icon": "*",
        "menu": "casa"}
_wall = {"name": "Wallonly", "url": "https://example.invalid", "icon": "*"}

_home = menu_from(HOME, [_lan, _any, _wall])
check("at home, a lan_only extension is offered",
      [t["name"] for t in _home["casa"]] == ["Lights", "Trading"],
      [t["name"] for t in _home["casa"]])
_away = menu_from(AWAY, [_lan, _any, _wall])
check("from away it is gone", [t["name"] for t in _away["casa"]] == ["Trading"],
      [t["name"] for t in _away["casa"]])
check("and one that works anywhere still is",
      any(t["name"] == "Trading" for t in _away["casa"]))
check("a tile with no menu: is in no group at all",
      not any(t["name"] == "Wallonly"
              for rows in _home.values() for t in rows))
A._dashboard_cache.update(key=None)

print()
shutil.rmtree(tmp, ignore_errors=True)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
