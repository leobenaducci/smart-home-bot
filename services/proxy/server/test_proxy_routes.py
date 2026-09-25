"""Which prefixes this proxy forwards, and what an unrouted one costs.

Run (needs the server's own dependencies, so easiest in a container):

    docker run --rm -v $PWD/server:/srv:ro \\
      -e SESSION_SIGNING_KEY=x -e HOMECORE_LOCAL_URL=http://127.0.0.1:9 \\
      python:3.12-slim sh -c 'cp -r /srv /tmp/server && cd /tmp &&
        pip install -q -r server/requirements.txt &&
        python server/test_proxy_routes.py'

This proxy is the only way into the house from the app — the app talks to
`chat.home` even on the home wifi — and it routes by prefix, 404ing
anything it does not know before the request reaches HomeCore. So a HomeCore page
that is perfectly fine can be a dead link in the app, working in a browser on
the LAN the whole time.

That has now happened to `/menu`, `/camaras` and `/files`, each noticed by somebody tapping the tile. HomeCore's
`local/test_app_tiles_reachable.py` is the other half of this: it reads this
file and fails when a tile in Casa apps has no route here. This half checks the
routes actually behave.
"""
import ipaddress
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("SESSION_SIGNING_KEY", "0" * 32)
os.environ.setdefault("HOMECORE_LOCAL_URL", "http://127.0.0.1:9")

import main  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


client = TestClient(main.app)
routed = {p.split("{")[0].rstrip("/") for p in
          {getattr(r, "path", "") for r in main.app.routes} if "{rest" in p}

print("the prefixes every Casa apps tile needs")
# Each of these is a page in the app. Missing one is not a degraded feature;
# it is a tile that does nothing.
for prefix in ("/chat", "/tasks", "/grocery", "/menu", "/camaras", "/files"):
    check(prefix, prefix in routed, sorted(routed))

print("\nand the ones the app needs for a page to draw at all")
for prefix in ("/geo", "/theme"):
    check(prefix, prefix in routed, sorted(routed))

print("\na logged-out page navigation lands on the login, never on a 404")
# The distinction that matters: 404 is the shape of "this prefix is not routed",
# which is the bug. A redirect means the route exists and auth did its job.
for path in ("/files", "/menu", "/stats"):
    r = client.get(path, follow_redirects=False)
    check(f"{path} -> login", r.status_code == 302 and r.headers.get("location") == "/login",
          f"{r.status_code} {r.headers.get('location')}")

print("\nand the house-only prefixes are not served from the VPS at all")
# The cameras are for the house. A login on a public host is not the boundary
# anybody wants around a live camera feed, so off the VPN they are not served —
# checked before auth, because whether the prefix
# exists here does not depend on who is asking. 404 and not 403 for the same
# reason /nope is: from outside, this genuinely is not a thing this host serves.
check("the default is 'not the house'", main.IS_LAN_COPY is False, main.IS_LAN_COPY)
for path in ("/camaras/", "/camaras/api/cams"):
    r = client.get(path, follow_redirects=False)
    check(f"{path} -> 404 from the VPS", r.status_code == 404, r.status_code)

print("\nand the copy at home serves them normally")
main.IS_LAN_COPY = True
try:
    for path in ("/camaras/",):
        r = client.get(path, follow_redirects=False)
        check(f"{path} -> login", r.status_code == 302, r.status_code)
    # /stats is the deliberate exception: debug numbers, no camera, wanted
    # from anywhere.
    for path in ("/stats",):
        r = client.get(path, follow_redirects=False)
        check(f"{path} is never house-only", r.status_code == 302, r.status_code)
finally:
    main.IS_LAN_COPY = False
r = client.get("/stats", follow_redirects=False)
check("/stats still works from the VPS", r.status_code == 302, r.status_code)

print("\nand a caller on the VPN is inside the house")
# The Android app hard-codes the public URL and always arrives *here*, wherever
# the phone is, so a LAN browser's answer ("this copy is only reachable from
# inside") can never apply to it. The VPN is what distinguishes it.
#
# The address is read from the LAST X-Forwarded-For hop -- the one Caddy wrote --
# so the checks below send a forged first hop too. If the first were ever read
# instead, being inside would be something any caller could simply claim, and
# the cameras would be public.


class _Req:
    """Just enough Request for `_from_house`: a header bag and a peer."""

    def __init__(self, xff):
        self.headers = {"X-Forwarded-For": xff}
        self.client = None


# The shipped default, not a value invented for the test: Tailscale's range.
check("the default is the VPN range",
      main._parse_home_networks(main.DEFAULT_HOME_NETWORKS) ==
      [ipaddress.ip_network("100.64.0.0/10")], main.DEFAULT_HOME_NETWORKS)

main.HOME_NETWORKS = main._parse_home_networks(main.DEFAULT_HOME_NETWORKS)
try:
    r = client.get("/camaras/", follow_redirects=False,
                   headers={"X-Forwarded-For": "100.101.102.103"})
    check("a VPN address gets the cameras", r.status_code == 302, r.status_code)

    r = client.get("/camaras/", follow_redirects=False,
                   headers={"X-Forwarded-For": "198.51.100.9"})
    check("a public address does not", r.status_code == 404, r.status_code)

    # The rejected design, pinned so it cannot come back by accident: arriving
    # from the household's own public address is arriving from the internet.
    # Treating it as "inside" showed house-only links to the home wifi with no
    # VPN at all, which is not what LAN-or-VPN means.
    # A stand-in for the household's own public address -- the rejected design,
    # pinned so it cannot come back by accident. Arriving from the address the
    # house itself NATs to is still arriving from the internet, and treating it
    # as inside showed the house-only links on the home wifi with no VPN at all.
    # A documentation-range address on purpose: the real one is household data
    # and does not belong in a tracked file.
    r = client.get("/camaras/", follow_redirects=False,
                   headers={"X-Forwarded-For": "203.0.113.200"})
    check("nor does a household's own public address", r.status_code == 404,
          r.status_code)

    # Prepending is the only thing a caller can do to this header, and it has to
    # buy nothing at all.
    r = client.get("/camaras/", follow_redirects=False,
                   headers={"X-Forwarded-For": "100.101.102.103, 198.51.100.9"})
    check("a forged first hop cannot claim the VPN",
          r.status_code == 404, r.status_code)

    check("an address that is not one is not inside",
          main._from_house(_Req("not-an-ip")) is False, "parsed a non-address")

    # The VPS's own network is 172.16/12 and is deliberately not in the default:
    # a hop in front of Caddy would otherwise make every caller the house.
    check("the VPS's own subnet is not inside",
          main._from_house(_Req("172.16.0.4")) is False, "172.16/12 matched")
finally:
    main.HOME_NETWORKS = []

# An explicitly empty list means nobody, and must not fall back to the default.
check("an empty list is nobody, not everybody",
      main._from_house(_Req("100.101.102.103")) is False, "empty list matched")

main.HOME_NETWORKS = main._parse_home_networks(main.DEFAULT_HOME_NETWORKS)
try:
    # An unparseable entry is dropped, not widened. A CIDR typo that fell back
    # to "match everything" would publish the cameras from a config mistake.
    check("an unparseable entry is dropped",
          main._parse_home_networks("nonsense, 203.0.113.0/24") ==
          [ipaddress.ip_network("203.0.113.0/24")],
          main._parse_home_networks("nonsense, 203.0.113.0/24"))
finally:
    main.HOME_NETWORKS = []

print("\nwhen the house cannot be reached at all")
# HOMECORE_LOCAL_URL points at 127.0.0.1:9 in this harness, so every forwarded
# request genuinely fails to connect — the same thing a hub reboot or a Caddy
# restart does to the tunnel. What the family used to get here was
# `{"error": "upstream unreachable: [Errno 111] Connection refused"}` painted as
# plain text in the WebView.
# Signed in, since the unreachable answer lives past the auth gate. The session
# is a header rather than a cookie on the client, so no other check inherits it,
# and users.json is written here because a token only counts for a user who
# exists — the proxy re-reads that file on every lookup by design.
import json as _json, tempfile as _tempfile, users as _users
_users_path = _tempfile.mktemp(suffix=".json")
with open(_users_path, "w", encoding="utf-8") as _f:
    _json.dump([{"username": "user1"}], _f)
_users.USERS_FILE = _users_path
SESSION = {"Cookie": f"{main.COOKIE_NAME}={main.create_token('user1')}"}

r = client.get("/chat", headers={**SESSION, "Accept": "text/html"}, follow_redirects=False)
check("a page navigation gets a page", r.status_code == 503
      and "text/html" in r.headers.get("content-type", ""), r.status_code)
check("saying it is the house and not the phone",
      "I can't reach the house" in r.text, r.text[:120])
check("and that nothing was lost", "nothing you wrote has been lost" in r.text)
# The page heals itself: /healthz reports whether the tunnel is up, so the
# usual outage (a redeploy) ends without anybody tapping anything.
check("it checks by itself until the house answers",
      "/healthz" in r.text and "location.reload" in r.text)
check("with a retry for somebody who would rather tap", 'id="retry"' in r.text)
# The detail belongs in the log, not on a public host's page.
check("no exception text on screen", "Errno" not in r.text and "httpx" not in r.text)

r = client.get("/chat/history", headers={**SESSION, "Accept": "application/json"})
check("a fetch gets JSON, not a document", r.status_code == 503
      and r.json().get("error"), r.text[:120])
check("and it is a sentence, not a traceback",
      "casa" in r.json()["error"] and "Errno" not in r.json()["error"], r.json())
# Every fetch in chat.html reads `.error`; handing those HTML turns a clear
# failure into "JSON.parse: unexpected character at line 1 column 1".
check("the JSON shape is what the page already reads",
      set(r.json()) == {"error"}, r.json())

print("\nand a logged-out API call is a 401, not a redirect into HTML")
for path in ("/geo/api/geofences",):
    r = client.get(path, follow_redirects=False)
    check(f"{path} -> 401", r.status_code == 401, r.status_code)

print("\nwhat is genuinely not routed still 404s")
for path in ("/nope", "/admin", "/wiki"):
    r = client.get(path, follow_redirects=False)
    check(f"{path} -> 404", r.status_code == 404, r.status_code)

# `{rest:path}` does not require a `/` after the prefix, so `/filesx` is
# matched by the /files route with rest="x" and forwarded. True of all the
# prefixes and always has been; harmless, because HomeCore has no such route and
# answers 404 one hop later. Written down rather than tightened: the check is
# that it stays a nuisance and never becomes a way to reach something.
r = client.get("/filesx", follow_redirects=False)
check("a name that merely starts with a prefix is not a 404 here",
      r.status_code == 302, r.status_code)

print("\nthe upstream URL keeps the prefix, the rest of the path and the query")
check("/camaras/api/cams?x=1",
      main._upstream_url("/camaras", "/api/cams", "x=1")
      == os.environ["HOMECORE_LOCAL_URL"] + "/camaras/api/cams?x=1",
      main._upstream_url("/camaras", "/api/cams", "x=1"))
check("no query, no trailing ?",
      main._upstream_url("/files", "", "") == os.environ["HOMECORE_LOCAL_URL"] + "/files")

# --- a session on a machine that holds no user store -------------------------
#
# `AUTH_MODE=upstream` is what this proxy is for: the household's password
# hashes stay on the household's own hardware, and the login path asks the home
# machine over the tunnel. `_current_user` did not know that. It called
# `find_user`, which reads /app/data/users.json -- a file this deployment
# deliberately does not have -- so it returned None for every username.
#
# Every login therefore succeeded, set a cookie, and every request after it was
# 401. On a phone that is a working sign-in followed by nothing working, which
# is the worst arrangement of those two facts, and it survives reinstalling the
# app because nothing about the app is wrong.
print("\na signed session is enough where there is no user store")

import types as _types
from auth import create_token as _mint

class _Req:
    def __init__(self, token):
        self.cookies = {main.COOKIE_NAME: token} if token else {}

_saved_mode = main.auth.AUTH_MODE
try:
    main.auth.AUTH_MODE = "upstream"
    check("a token this proxy signed identifies its user",
          main._current_user(_Req(_mint("someone"))) == "someone",
          main._current_user(_Req(_mint("someone"))))
    check("  even though no users.json exists here",
          not os.path.exists(os.environ.get("USERS_FILE", "/app/data/users.json")),
          "this test proves nothing if the file is present")
    check("  and nothing at all is still nobody",
          main._current_user(_Req(None)) is None)
    check("  as is a token this proxy did not sign",
          main._current_user(_Req("not.a.token")) is None)

    # The other mode is the one with a store to check, and it must still check
    # it: there, a name with no record behind it is not a user.
    main.auth.AUTH_MODE = "local"
    check("with a local store, an unknown name is refused",
          main._current_user(_Req(_mint("someone"))) is None,
          "local mode must still consult the store")
finally:
    main.auth.AUTH_MODE = _saved_mode

# --- which hop the enrol rate limit counts against ---------------------------
# This app is on loopback behind Caddy, so `request.client.host` is 127.0.0.1
# for everybody and the enrol limiter has one bucket for the world. The fix is
# X-Forwarded-For, and the end it is read from decides whether the limiter
# works or stops existing: Caddy **appends** to a caller-supplied value rather
# than replacing it, so the first element is whatever the caller typed. That
# address is the only key on the limit protecting a six-digit enrol code -- a
# caller who picks their own key gets unlimited guesses at a million-wide
# space, and each success enrols a device on the household portal.
print("\nthe enrol limiter counts the hop Caddy saw, not the one the caller typed")


class _Req:
    """Just the two attributes `_client_ip` reads."""

    def __init__(self, xff=None, peer="127.0.0.1"):
        self.headers = {} if xff is None else {"X-Forwarded-For": xff}
        self.client = type("C", (), {"host": peer})() if peer else None


check("a forged first hop is ignored",
      main._client_ip(_Req("9.9.9.9, 203.0.113.7")) == "203.0.113.7",
      main._client_ip(_Req("9.9.9.9, 203.0.113.7")))
check("  so two callers forging the same address still count separately",
      main._client_ip(_Req("1.1.1.1, 203.0.113.7"))
      != main._client_ip(_Req("1.1.1.1, 203.0.113.8")))
check("a single hop is the client itself",
      main._client_ip(_Req("203.0.113.7")) == "203.0.113.7")
check("several forged hops still yield the last",
      main._client_ip(_Req("a, b, c, 203.0.113.7")) == "203.0.113.7")
check("no header falls back to the peer",
      main._client_ip(_Req(None, peer="198.51.100.4")) == "198.51.100.4")
check("an empty header falls back too",
      main._client_ip(_Req("  ", peer="198.51.100.4")) == "198.51.100.4")
check("no peer at all is not a crash",
      main._client_ip(_Req(None, peer=None)) == "unknown")
# X-Real-IP is deliberately not consulted: nothing in the generated Caddy block
# sets it, so trusting it would mean trusting a header only a caller sends.
_r = _Req(None, peer="198.51.100.4")
_r.headers["X-Real-IP"] = "9.9.9.9"
check("X-Real-IP is not trusted, because nothing here sets it",
      main._client_ip(_r) == "198.51.100.4", main._client_ip(_r))

print("\nthe ticket that opens opencode is forwarded, and only it")
# `/_code/enter` came back {"detail":"Not Found"} until this route existed: the
# proxy forwards an allowlist of prefixes and everything else 404s. That is the
# design, so the route is narrow -- one prefix, GET only -- and pinned here so
# the next path added by hand is a deliberate one.
_routes = {getattr(r, "path", "") for r in main.app.routes}
check("  /_code is forwarded", "/_code{rest:path}" in _routes, sorted(_routes)[:4])
_code_route = next((r for r in main.app.routes
                    if getattr(r, "path", "") == "/_code{rest:path}"), None)
check("  GET only", _code_route is not None
      and set(_code_route.methods or []) <= {"GET", "HEAD"},
      _code_route and _code_route.methods)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
