"""The wall at `/`: what it shows, and what it refuses to show.

Run: python local/test_dashboard.py   (needs Flask; skips loudly without it)

The root of the house was a redirect to gethomepage for a while. That was a
second web server with its own config format and no idea who was asking, which
is why this page exists again: the tiles a household may open depend on where
the request came from, and only this app knows.

What is pinned here is which squares a given request gets, and every one of
these has a failure that looks like something else:

- **A house-only tile is absent from outside**, not greyed out. `_at_home()`
  fails closed, so the interesting direction is the away one.
- **A tile that is not this person's is absent too.** `/stats` and the rest
  send a child back to `/`, which is this page -- so the square returns them
  to the page they clicked it on, and reads as a broken link rather than as a
  page that is not theirs.
- **`/` is never a tile.** `home-core`'s own `portal_path` is `/`, which is
  this page: a square linking to the wall, from the wall.
- **A path this app already serves appears once**, whoever put it there. A
  household is free to add a custom service pointing at `/tasks`, and two
  squares for one page is a wall that looks broken.
- **A tile the catalogue has never heard of keeps its own title.** Extensions
  are named by the household, and `portal.service.plex` on the wall is worse
  than `Plex`.

The generated file is written by hand here rather than by calling the deployer:
this is the portal's side of that contract, and a test that produced its own
input from the same code that produces the real one would pass on a shape
neither of them agrees to.
"""
import json
import os
import shutil
import sys
import tempfile

SRC = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SRC)))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-dashboard-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs", "i18n"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
# The catalogues, staged the way `assets:` stages them into the build context.
# Without them every string renders as its key and the title assertions below
# would pass on `portal.service.chat`.
#
# Next to the service first, then the repo. Those are the same catalogues by
# two routes: the deployer copies `i18n/` into the build context, so inside the
# built image they sit beside app.py -- and `ROOT` here climbs three levels,
# which from `/app` lands on `/` and found nothing. Preferring the staged copy
# also means this checks what actually ships rather than what is in the
# checkout beside it.
_i18n = os.path.join(SRC, "i18n")
if not os.path.isdir(_i18n):
    _i18n = os.path.join(ROOT, "i18n")
shutil.copytree(_i18n, os.path.join(dst, "i18n"),
                ignore=shutil.ignore_patterns("__pycache__"))
os.chdir(dst)

PROXY_SECRET = "p" * 32
USER1 = "user1"
SERVICES = os.path.join(dst, "services.json")
CHILD = "user4"
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET=PROXY_SECRET,
                  DEBUG_API_KEY="d" * 32, SERVICES_FILE=SERVICES,
                  HOME_STACK_DEFAULT_LOCALE="en", HOME_STACK_LOCALES="en,es",
                  HOMECORE_MEMBERS="user1,user4",
                  HOMECORE_ADMIN_MEMBERS="user1")
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    json.dump([{"username": USER1, "nanobot_id": 1, "member": "user1"},
               {"username": CHILD, "nanobot_id": 2, "member": "user4"}], f)


def write_services(groups):
    with open(SERVICES, "w", encoding="utf-8") as fh:
        json.dump({"groups": groups}, fh)


write_services({
    "basic": [
        {"name": "home-core", "title": "home-core", "description": "The portal",
         "icon": "🏠", "url": "/", "lan_only": False, "source": "system"},
        {"name": "home-cameras", "title": "home-cameras",
         "description": "Camera registry and the camera wall", "icon": "📷",
         "url": "/camaras/", "lan_only": True, "source": "system"},
        {"name": "home-paperless", "title": "home-paperless",
         "description": "Document archive", "icon": "📄",
         "url": "http://192.168.1.12:21030", "lan_only": True, "source": "system"},
    ],
    "advanced": [
        {"name": "mqtt", "title": "mqtt", "description": "The broker",
         "icon": "📡", "url": "http://192.168.1.10:21050", "lan_only": True,
         "source": "system"},
        {"name": "admin", "title": "admin", "description": "Settings",
         "icon": "⚙️", "url": "http://192.168.1.10:21002", "lan_only": True,
         "adults": True, "source": "system"},
    ],
    "extensions": [
        {"name": "plex", "title": "Plex", "description": "Films",
         "icon": "🎬", "url": "http://192.168.1.9:32400", "lan_only": True,
         "source": "custom"},
        {"name": "weather", "title": "Weather", "description": "The forecast",
         "icon": "🌦", "url": "https://example.invalid/weather",
         "lan_only": False, "source": "custom"},
        # Aimed at a path this app serves itself. Nothing stops a household
        # doing this, and the wall must not then show Chores twice.
        {"name": "chores-again", "title": "Chores Again", "description": "",
         "icon": "⭐", "url": "/tasks", "lan_only": True, "source": "custom"},
    ],
})

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


def wall(headers):
    with A.app.test_request_context("/", headers=headers):
        A._proxy_auth()
        return A._dashboard_wall()


def urls(w):
    return [tile["url"] for group in w.values() for tile in group]


def titles(w):
    return [tile["title"] for group in w.values() for tile in group]


print("the page renders at all")
for label, headers in (("at home", HOME), ("from outside", AWAY)):
    r = client.get("/", headers=headers)
    check(f"GET / {label} is a page, not a redirect", r.status_code == 200,
          f"{r.status_code} {r.headers.get('Location')}")

print("\nwhat is on the wall at home")
at_home = wall(HOME)
check("the portal's own pages are there", "/chat" in urls(at_home), urls(at_home))
check("a deployed service is there",
      "http://192.168.1.12:21030" in urls(at_home), urls(at_home))
check("an extension is there",
      "http://192.168.1.9:32400" in urls(at_home), urls(at_home))
check("the camera wall is there", "/camaras/" in urls(at_home), urls(at_home))
check("`/` is not a tile: it is this page",
      "/" not in urls(at_home), urls(at_home))
check("a custom tile aimed at one of this app's own pages does not double it",
      urls(at_home).count("/tasks") == 1, urls(at_home))
check("and the page that survived is this app's own",
      "Chores Again" not in titles(at_home), titles(at_home))

print("\nand from outside, where half of it would be a dead link")
away = wall(AWAY)
check("the house-only service is gone",
      "http://192.168.1.12:21030" not in urls(away), urls(away))
check("so is the camera wall", "/camaras/" not in urls(away), urls(away))
check("the portal's own pages stay", "/chat" in urls(away), urls(away))
# The one extension the household said is reachable from anywhere. Absent
# tiles are the whole mechanism here, so a run where *everything* vanished
# would pass every check above and be badly wrong.
check("an extension that is not house-only stays",
      "https://example.invalid/weather" in urls(away), urls(away))
check("something is still on the wall", len(urls(away)) > 3, urls(away))

print("\nand the pages that are not this person's are not offered")
# `/stats` and the rest send anybody who is not an adult back to `/` -- which
# is this page. A tile that returns you to the page you clicked it on reads as
# a broken link, not as a page that is not yours.
kid = wall({"X-Proxy-Secret": PROXY_SECRET, "X-Proxy-User": CHILD,
            "X-Proxy-Lan": "1"})
check("an adult sees the adult pages", "/stats" in urls(at_home), urls(at_home))
check("a child does not", "/stats" not in urls(kid), urls(kid))
check("nor any of the others",
      not {"/credentials", "/mailboxes", "/profiles", "/projects"} & set(urls(kid)),
      urls(kid))
check("but keeps the pages that are theirs",
      {"/chat", "/tasks", "/files", "/account/devices"} <= set(urls(kid)), urls(kid))
# The same rule for a service, said by the manifest rather than by this file.
check("a service marked for adults is off a child's wall too",
      "http://192.168.1.10:21002" not in urls(kid), urls(kid))
check("and on an adult's",
      "http://192.168.1.10:21002" in urls(at_home), urls(at_home))

print("\ntitles come from the catalogue, by name")
check("a portal page is named, not keyed",
      "Assistant" in titles(at_home), titles(at_home))
check("so is a deployed service",
      "Documents" in titles(at_home), titles(at_home))
check("no key leaked onto the page",
      not any(x.startswith("portal.") for x in titles(at_home)), titles(at_home))
check("an extension keeps the name the household gave it",
      "Plex" in titles(at_home), titles(at_home))
# The service ids the deployer writes as a fallback must not be what a person
# reads. This is the assertion that fails if a catalogue entry is missing.
check("no service id is showing",
      not any(x in titles(at_home) for x in ("home-paperless", "mqtt", "home-cameras")),
      titles(at_home))

print("\nthe Spanish house gets a Spanish wall")
r = client.get("/?lang=es", headers=HOME)
body = r.get_data(as_text=True)
check("a translated tile title is rendered", "Documentos" in body,
      "the catalogue is staged but the page is not using the locale")
check("and the English one is not", ">Documents<" not in body)

print("\nan absent services.json is a short wall, not a 500")
os.rename(SERVICES, SERVICES + ".away")
try:
    r = client.get("/", headers=HOME)
    check("the page still renders", r.status_code == 200, r.status_code)
    check("with this app's own pages on it", "/chat" in urls(wall(HOME)))
finally:
    os.rename(SERVICES + ".away", SERVICES)

print("\nand a rewritten one is picked up without a restart")
write_services({"basic": [], "advanced": [], "extensions": [
    {"name": "newthing", "title": "New Thing", "description": "", "icon": "✨",
     "url": "http://192.168.1.5:8000", "lan_only": True, "source": "custom"}]})
check("the new tile appears", "New Thing" in titles(wall(HOME)), titles(wall(HOME)))
check("and the old ones are gone",
      "Plex" not in titles(wall(HOME)), titles(wall(HOME)))

print("\na damaged file keeps the last good wall rather than blanking it")
with open(SERVICES, "w", encoding="utf-8") as fh:
    fh.write("{not json")
check("the page renders", client.get("/", headers=HOME).status_code == 200)
check("and still shows what it last read",
      "New Thing" in titles(wall(HOME)), titles(wall(HOME)))

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")
