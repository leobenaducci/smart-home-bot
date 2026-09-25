"""Every tile in Casa apps has to be reachable from the app.

Run: python local/test_app_tiles_reachable.py

A link whose URL is a path on this host (`/files`, `/menu`, `/tasks`…) is
opened *inside* the WebView, which is logged into `chat.home` — so it
is fetched through the home-chat proxy on the VPS. That proxy routes by prefix
and 404s anything it does not know, before the request ever reaches HomeCore.

Which means a tile can be perfectly correct here, work in a browser on the LAN,
and be a dead link in the app. It has happened four times — `/menu`,
`/camaras` and `/files` — each found by somebody
tapping it and each fixed on its own. The comments left behind say it plainly:
"works on the LAN, silently wrong off-VPN". The app comes through the proxy
even on the home wifi, so "off-VPN" undersells it: in the app it is always
broken.

Nothing catches this at review time, because the two halves live in different
repositories: the tile is added here and the route belongs in home-chat. So
this reads the sibling checkout. It is a sibling of this repo inside the
home-stack superproject; when it is not there, this says so and skips rather
than passing quietly, because a check that silently finds nothing is how the
gap survived the last four times.

Tiles pointing at another host (`http://media.home:8096`) are not affected —
the app hands those to the browser.
"""
import pathlib
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / "app.py"

# This one reads a *sibling service* -- the proxy decides which of the portal's
# tiles are reachable, so the two files have to be checked against each other.
# That makes it the only test here that needs the whole checkout, and inside the
# service's own image there is no checkout: `/app` has no `services/` above it,
# and `parents[1]` raised IndexError before it could say why.
#
# Skipping loudly rather than passing quietly, and rather than crashing: the
# deploy gate runs every one of these against the built image, and a test that
# cannot run there has to say so in the words the runner already understands.
# It still runs, and still guards the pair, from a checkout.
_root = HERE.parents[1] if len(HERE.parents) > 1 else None
PROXY = (_root / "proxy" / "server" / "main.py") if _root else None
if PROXY is None or not PROXY.exists():
    print("SKIP: this checks the portal against the proxy, and the proxy is not "
          "here — run it from a checkout, not from inside the service image.")
    raise SystemExit(0)

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


# Not a skip. The proxy was a sibling repo when this was written, so its
# absence was ordinary; in this package it is services/proxy/, tracked here,
# and the only way it can be missing is that somebody moved it and did not
# update this line. Skipping would report that as a pass.
if not PROXY.is_file():
    raise SystemExit(f"FAIL: the proxy source is not at {PROXY}. It is part of "
                     f"this repository, so this is a stale path, not a missing "
                     f"checkout.")

# The chat's Apps menu: this app's own routes, offered from inside a
# conversation. The dashboard itself is Homepage now and is generated per
# install from the enabled services, so it cannot be read out of this source
# file and does not need to be -- a tile for a service nobody runs is not a
# dead link, it is absent.
#
# What this test still guards is the thing that broke before: an internal tile
# whose prefix the chat proxy does not route, which shows up as a dead square
# only once somebody opens it from outside the house.
internal = re.search(r"^CHAT_APP_LINKS\s*=\s*\[(.*?)^\]", APP.read_text(encoding="utf-8"),
                     re.S | re.M)
assert internal, "app.py no longer has a CHAT_APP_LINKS list"
tiles = [(n, u) for n, u in re.findall(
    r"'name':\s*'([^']+)'.*?'url':\s*'([^']+)'", internal.group(1))]
assert tiles, "no links parsed out of CHAT_APP_LINKS"

routed = set(re.findall(r'@app\.api_route\(\s*"/(\w+)\{rest', PROXY.read_text(encoding="utf-8")))
assert routed, "no prefixes parsed out of the proxy"

print(f"the proxy routes: {', '.join('/' + p for p in sorted(routed))}\n")

print("every tile that opens in the app is routed there")
own_host = [(n, u) for n, u in tiles if u.startswith("/")]
check("there are tiles to check", bool(own_host))
for name, url in own_host:
    top = url.strip("/").split("/")[0]
    check(f"{name} ({url})", top in routed,
          f"add `@app.api_route(\"/{top}{{rest:path}}\", …)` to home-chat's server/main.py, "
          "or the tile is a dead link in the app")

print("\nand so is every link in the apps menu, not just the Casa tiles")
# The tiles above come from SERVICES. The menu's own entries — Ajustes and
# Debug — are plain <a class="app-link"> and were checked by nothing, which is
# how /projects, /credentials, /profiles and /mailboxes all shipped as dead
# links in the app while working perfectly on the LAN. Same failure as the four
# in this file's header, found the same way: by tapping one.
chat_html = (HERE / "templates" / "chat.html").read_text(encoding="utf-8")
menu_links = sorted(set(re.findall(r'class="app-item app-link" href="(/[^"]+)"', chat_html)))
check("there are menu links to check", bool(menu_links), menu_links)
for url in menu_links:
    top = url.strip("/").split("/")[0]
    check(f"{url} is routed", top in routed,
          f"add `@app.api_route(\"/{top}{{rest:path}}\", ...)` to home-chat's "
          "server/main.py, or the menu entry is a dead link in the app")

# A page is not reachable just because its own URL is: these four are inert
# without the API they call, and an API that 404s at the proxy looks to
# somebody using it like a broken page.
print("\nand the APIs those pages cannot work without")
for prefix, page in (("profiles", "Correos de Alfred"),
                     ("family", "Directorio familiar"),
                     ("projects", "Proyectos"),
                     ("credentials", "Credenciales")):
    check(f"/{prefix}/api for {page}", prefix in routed,
          "the page loads and then fails every fetch")

# Two of those tiles are routed *and* deliberately refused off the VPN, which
# the loop above cannot tell apart from a working one. Pin both halves, so the
# guard cannot be dropped on either side and leave this file still passing.
print("\nand the house-only tiles are routed but refused off the VPN")
proxy_src = PROXY.read_text(encoding="utf-8")
app_src = APP.read_text(encoding="utf-8")
for prefix in ("camaras",):
    body = re.search(rf'async def proxy_{prefix}\(.*?(?=\n@app\.|\nasync def |\Z)',
                     proxy_src, re.S)
    # `_lan_only(` and not `_lan_only()`: the guard takes the app key now, so
    # that which apps are house-only is the household's setting rather than a
    # fact hardcoded in two services. Matching the empty call made this check
    # fail the moment the argument was added -- and fail describing an exposure
    # that was not happening.
    check(f"home-chat's /{prefix} is guarded", bool(body) and "_lan_only(" in body.group(0),
          "the proxy would serve it from the VPS")
check("home-chat only lifts the guard for the house copy",
      'IS_LAN_COPY = os.environ.get("IS_LAN_COPY", "0")' in proxy_src,
      "the default has to be 'not the house'")
check("and only the house copy sets it",
      'IS_LAN_COPY: "1"' in (PROXY.parents[1] / "docker-compose.local.yml").read_text(encoding="utf-8")
      and "IS_LAN_COPY" not in (PROXY.parents[1] / "docker-compose.yml").read_text(encoding="utf-8"),
      "docker-compose.local.yml sets it; docker-compose.yml must not")
# The list stopped being a literal: it is derived from HOUSE_ONLY_APPS, so the
# household's choice of what stays in the house and the tiles the Apps menu
# hides are one fact rather than two that drift. What this file can still
# assert is the default, which is the shipped behaviour -- cameras house-only,
# nothing else -- and it has to assert it without importing app.py, which pulls
# in smbclient and the rest of the portal's dependencies.
#
# Matched against the default of the environment variable rather than against
# a tuple literal. The previous version looked for `CHAT_HOUSE_ONLY_LINKS = (`;
# when that became `tuple(`, the `re.search(...).group(1)` below it raised
# AttributeError on None and every check after it silently stopped running.
default = re.search(r"os\.environ\.get\('HOUSE_ONLY_APPS', '([^']*)'\)", app_src)
check("HomeCore's house-only default is still cameras",
      default is not None and default.group(1) == "cameras",
      "the Apps menu would offer a link that 404s, or hide one that works")
check("and the everyday pages stay reachable from anywhere",
      default is not None
      and not {"files", "tasks", "grocery", "menu"} & set(default.group(1).split(",")),
      "those are served by this app, so they work wherever the chat does")
check("the hidden tiles are derived from that set, not listed separately",
      "CHAT_HOUSE_ONLY_LINKS = tuple(" in app_src
      and "for key in sorted(HOUSE_ONLY_APPS)" in app_src,
      "a second list of names is a second thing to keep in step")

print("\nand a tile on another host is the browser's problem, not the proxy's")
# Off-host tiles are not literals in this file: the deployer generates them
# from the enabled services and the household's own additions, and the portal
# reads the result. What they contain -- an address a browser can reach rather
# than loopback, nothing for a service that is switched off -- is asserted
# against the generator's actual output in deploy/test_deploy.py, and against
# the page that draws it in local/test_dashboard.py.
#
# Named here rather than grepped: this file used to assert a substring of
# deploy.py's source, which passes for as long as nobody edits a line and says
# nothing about what the function returns. The one thing worth pinning across
# the two is that the entry point still exists under the name the portal's
# side of the contract was written for.
GENERATOR = pathlib.Path(__file__).resolve().parents[3] / "deploy" / "deploy.py"
check("the deployer is where this expects it", GENERATOR.is_file(), GENERATOR)
if GENERATOR.is_file():
    gen_src = GENERATOR.read_text(encoding="utf-8")
    check("and it still builds the wall this app reads",
          "def build_portal_dashboard(" in gen_src,
          "renaming it leaves services.json unwritten and the wall short")
DASHBOARD_TEST = pathlib.Path(__file__).resolve().parent / "test_dashboard.py"
check("the page's own suite is beside this one", DASHBOARD_TEST.is_file(),
      "what a tile links to is asserted there, not here")

print("\nthe prefixes the app needs for a page to render at all")
# Not tiles, but the same failure: unrouted means the app gets a 404 where the
# LAN gets a page. /theme is how every page paints the member's colours.
for needed in ("chat", "theme"):
    check(f"/{needed}", needed in routed)

print("\nthe apps menu keeps settings and debugging apart")
# Everything real ended up under «Debug» — the projects registry, the
# credentials, the family directory — where it was findable only by somebody
# who already knew it was there. Debug keeps what is actually debugging; how
# Alfred is set up lives under Ajustes.
chat = (HERE / "templates" / "chat.html").read_text(encoding="utf-8")
ajustes = chat[chat.index('id="ajustes-group"'):chat.index('id="debug-group"')]
debug = chat[chat.index('id="debug-group"'):]
debug = debug[:debug.index("</div>")]

for href in ("/profiles", "/mailboxes", "/projects", "/credentials"):
    check(f"{href} is a setting", href in ajustes and href not in debug,
          "in Debug" if href in debug else "in neither group")
# Diagnostics about a running assistant, not settings on one.
for href in ("/stats", "/alfred/profile.html"):
    check(f"{href} stays in Debug", href in debug and href not in ajustes)
check("«Ver pasos» and the log stay too",
      'id="steps-btn"' in debug and 'id="log-btn"' in debug)
# The phone-level controls, split by whether somebody sets one and lives with
# it or reaches for it to work out why something is broken.
check("«Voz de Alfred» is a preference",
      'id="tts-item"' in ajustes and 'id="tts-item"' not in debug)
check("and its engine, voice and ▶️ Probar stay with it",
      all(f'id="{i}"' in ajustes for i in ("tts-engine", "tts-voice", "tts-probe")),
      "a test button separated from what it tests helps nobody")
check("the beta channel is a tester's switch",
      'id="beta-item"' in debug and 'id="beta-item"' not in ajustes)
check("so is picking between four speech recognizers",
      'id="stt-item"' in debug and 'id="stt-reset"' in debug)
check("every group has a button that opens it",
      chat.count('data-group="ajustes-group"') == 1
      and chat.count('data-group="debug-group"') == 1)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
