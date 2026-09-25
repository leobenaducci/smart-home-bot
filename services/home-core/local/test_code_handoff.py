"""The ticket that lets the portal's session open opencode's own hostname.

Run: python3 test_code_handoff.py   (from services/home-core/local)

opencode's interface cannot live under a path -- its assets and its API are
absolute from the origin root, and `/api/...` collides with this app's own -- so
it gets a hostname of its own, and this app's host-only session cookie does not
reach it. The alternative was widening SESSION_COOKIE_DOMAIN to `.home`, which
would hand a live household session to Paperless, n8n, Node-RED and the cameras
app.

So: a signed sixty-second ticket, redeemed for a cookie scoped to that host.
What is pinned here is almost entirely the refusals. `/_code/whoami` decides
*which member's server* a request reaches, and each of those holds one person's
MCP token -- so a wrong answer is one member getting another's projects and
their folder on the share, with nothing failing.
"""

import os
import shutil
import sys
import tempfile
import time

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-code-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32, CODE_NAME="code.home")
sys.path.insert(0, dst)

import app  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label
          + ("" if cond else f"   <- {detail!r}"))
    if not cond:
        failures.append(label)


client = app.app.test_client()


def ticket_for(member="user1", user="999000111"):
    return app._code_serializer().dumps({"m": member, "u": user})


def whoami(cookie=None):
    """A fresh client each time: the shared one keeps a cookie jar, so a
    refusal checked after a successful redeem would be testing the cookie the
    redeem set rather than the one passed in -- and would pass while proving
    nothing."""
    c = app.app.test_client()
    if cookie:
        c.set_cookie(app._CODE_COOKIE, cookie, domain="localhost")
    return c.get("/_code/whoami")


print("redeeming a ticket")
r = client.get(f"/_code/auth?t={ticket_for()}")
check("  a good ticket redirects", r.status_code in (301, 302), r.status_code)
cookie = r.headers.get("Set-Cookie", "")
check("  and sets the cookie", app._CODE_COOKIE in cookie, cookie[:60])
check("  HttpOnly", "HttpOnly" in cookie, cookie[:120])
check("  Secure", "Secure" in cookie, cookie[:120])
check("  not scoped to a wider domain",
      "Domain=" not in cookie, cookie[:120])

print("\nand then Caddy can ask who it is")
value = app._code_cookie_serializer().dumps({"m": "user1"})
app.OPENCODE_SERVERS = {"user1": "http://127.0.0.1:4096",
                        "user2": "http://127.0.0.1:4097"}
r = whoami(value)
check("  200 with the member", r.status_code == 200
      and r.headers.get("X-Code-Member") == "user1",
      (r.status_code, dict(r.headers)))
check("  and that member's port", r.headers.get("X-Code-Port") == "4096",
      r.headers.get("X-Code-Port"))
r2 = whoami(app._code_cookie_serializer().dumps({"m": "user2"}))
check("  a different member gets a different server",
      r2.headers.get("X-Code-Port") == "4097", r2.headers.get("X-Code-Port"))
check("  and no host travels, only the port",
      not any("127.0.0.1" in v for v in r.headers.values()), dict(r.headers))
r3 = whoami(app._code_cookie_serializer().dumps({"m": "user9"}))
check("  a member with no server is refused, not routed",
      r3.status_code == 401, r3.status_code)

print("\nbut the refusals are the point")
check("  no cookie is 401", whoami("").status_code == 401, whoami("").status_code)
check("  a forged cookie is 401", whoami("user1").status_code == 401, whoami("user1").status_code)
check("  a tampered cookie is 401", whoami(value[:-4] + "AAAA").status_code == 401, whoami(value[:-4] + "AAAA").status_code)
check("  a cookie signed for another purpose is 401",
      whoami(app._code_serializer("something-else").dumps({"m": "user1"}))
      .status_code == 401)
# The one that matters most, because the ticket is the thing that travels in
# plain sight: it is in the URL, so it is in the browser's history, in the
# Referer of every asset the interface loads, and in the access log the code
# site's Caddy block writes. If it were also a valid cookie, "sixty seconds"
# would be a fiction and it would open the Programmer for twelve hours.
check("  a ticket is not a cookie",
      whoami(ticket_for()).status_code == 401,
      whoami(ticket_for()).status_code)
# And the other way round: a cookie must not be redeemable as a fresh ticket,
# which would reset the clock on it every time it was used.
_c = client.get("/_code/auth?t="
                + app._code_cookie_serializer().dumps({"m": "user1"}))
check("  and a cookie is not a ticket", _c.status_code == 403, _c.status_code)

print("\na ticket is single-purpose and short-lived")
r = client.get("/_code/auth?t=" + app._code_serializer().dumps({"m": ""}))
check("  a ticket naming no member is refused", r.status_code == 403, r.status_code)
r = client.get("/_code/auth?t=not-a-ticket")
check("  nonsense is refused", r.status_code == 403, r.status_code)
r = client.get("/_code/auth")
check("  a missing ticket is refused", r.status_code == 403, r.status_code)

_real_age = app._CODE_TICKET_MAX_AGE_S
app._CODE_TICKET_MAX_AGE_S = -1          # everything is already expired
r = client.get(f"/_code/auth?t={ticket_for()}")
check("  an expired ticket is refused", r.status_code == 403, r.status_code)
app._CODE_TICKET_MAX_AGE_S = _real_age

print("\nthe portal will not mint one for a stranger")
r = client.get("/_code/enter")
check("  minting requires a login", r.status_code in (302, 401, 403), r.status_code)

print("\nand it says so when the household has not configured the name")
_host = app.os.environ.pop("CODE_NAME", None)
check("  no dns.code means no host", app._code_host() == "")
if _host:
    app.os.environ["CODE_NAME"] = _host

print("\nand handing a job to opencode is scoped to the caller")

# What is pinned here is the refusals and the scoping. A task id is short
# enough to guess, and a finished job names files, branches and findings out of
# one member's checkout -- so reading somebody else's must be impossible rather
# than merely unlikely.
#
# The proxy header is the real one, derived the way alfred-mcp derives it. An
# earlier version of this faked `_proxy_auth` by rebinding the module
# attribute, which does nothing -- it is registered as a before_request, so the
# registered function is still the real one -- and every check "passed" on the
# 403 it was supposed to be proving the absence of.
_LOGIN = "999000111"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as _fh:
    _fh.write('[{"username": "%s", "nanobot_id": 2}]' % _LOGIN)
app._refresh_people() if hasattr(app, "_refresh_people") else None
# The background-task table, which boot creates in production. Without it the
# status route raises rather than answering, which is a property of the harness
# and not of the route -- the same call test_bgtask_actions.py makes.
app.init_bgtask_db()
# And the projects table, for the deploy-script door below. Boot creates
# both in production; a harness that skips them gets a 500 where the route
# would have answered.
app.init_projects_db()
_c = app.app.test_client()
_H = {"X-Proxy-User": _LOGIN, "X-Proxy-Secret": app._proxy_user_token(_LOGIN)}

check("  starting a job without proxy auth is refused",
      _c.post("/projects/api/code/run", json={"task": "x"}).status_code
      in (401, 403))
check("  and reading one is refused too",
      _c.get("/projects/api/code/status/code-deadbeef").status_code
      in (401, 403))

_saved = dict(app.OPENCODE_SERVERS)
app.OPENCODE_SERVERS.clear()
check("  a member with no opencode server is told so, not 500'd",
      _c.post("/projects/api/code/run", json={"task": "x"},
              headers=_H).status_code == 409)

app.OPENCODE_SERVERS.update({"user1": "http://127.0.0.1:4096"})
check("  an empty task is refused before anything is started",
      _c.post("/projects/api/code/run", json={"task": "   "},
              headers=_H).status_code == 400)
check("  and a task id nobody owns is a 404, not somebody else's result",
      _c.get("/projects/api/code/status/code-nope",
             headers=_H).status_code == 404)
app.OPENCODE_SERVERS.clear()
app.OPENCODE_SERVERS.update(_saved)

# The property that actually matters: one member's finished job is not
# readable by another, and the refusal does not leak its label either -- a
# label is the person's own words about what they are working on.
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as _fh:
    _fh.write('[{"username": "%s", "nanobot_id": 2},'
              ' {"username": "888000222", "nanobot_id": 3}]' % _LOGIN)
app._refresh_people() if hasattr(app, "_refresh_people") else None
app._bgtask_start(_LOGIN, "code-owned01", "refactor the vault",
                  "2026-09-05", "", "programmer")
app._bgtask_finish(_LOGIN, "code-owned01", "done")
_other = {"X-Proxy-User": "888000222",
          "X-Proxy-Secret": app._proxy_user_token("888000222")}
check("  the owner can read their own finished job",
      _c.get("/projects/api/code/status/code-owned01",
             headers=_H).status_code == 200)
_r = _c.get("/projects/api/code/status/code-owned01", headers=_other)
check("  another member gets 404 for it", _r.status_code == 404, _r.status_code)
check("  and the refusal does not leak the label",
      "refactor the vault" not in _r.get_data(as_text=True))

# A finished job has to carry its answer, because `code_task_status` promises
# one -- the first cut stored nothing and every status call read as a job that
# had produced silence.
app._bgtask_start(_LOGIN, "code-res001", "a job", "2026-09-05", "", "programmer")
app._bgtask_finish(_LOGIN, "code-res001", "done")
app._bgtask_set_result(_LOGIN, "code-res001", "Fixed it on branch fix/x.")
_res = _c.get("/projects/api/code/status/code-res001", headers=_H).get_json()
check("  a finished job reports what it produced",
      (_res or {}).get("result") == "Fixed it on branch fix/x.", _res)
check("  and says it is done", (_res or {}).get("status") == "done")

# Where a tap on the notification lands. The answer is filed in the
# Programmer's history, which is not the ordinary chat's -- so a link without
# the space shows the person an empty conversation and reads as the work having
# been lost. This notification was sent with no click target at all until it
# did exactly that.
_sent = {}
_real_ntfy, _real_watch = app.send_ntfy, app._user_watching
app.send_ntfy = lambda topic, msg, **kw: _sent.update(kw, topic=topic) or True
app._user_watching = lambda u: False
app._ntfy_topic = lambda u: 'a-topic'
try:
    app._code_task_report(_LOGIN, '', 'Fixed it on branch fix/x.')
finally:
    app.send_ntfy, app._user_watching = _real_ntfy, _real_watch

check("  the notification carries a tap target at all",
      bool(_sent.get('click')), _sent)
check("  which opens the Programmer's space, not the ordinary chat",
      '/chat/programmer' in str(_sent.get('click')), _sent.get('click'))
check("  and the day it was filed under",
      f"date={time.strftime('%Y-%m-%d')}" in str(_sent.get('click')),
      _sent.get('click'))

# Writing a deploy script goes through its own door rather than the projects
# PUT beside it: that one is admin-only because it can move a project to
# another host or swap its credential, and none of that should follow from
# "write me a deploy script".
_r = _c.post("/projects/api/code/deploy-script/nope",
             json={"deploy_script": "echo hi"})
check("  writing a deploy script without proxy auth is refused",
      _r.status_code in (401, 403), _r.status_code)
_r = _c.post("/projects/api/code/deploy-script/nope",
             json={"deploy_script": "echo hi"}, headers=_H)
check("  and a project this person cannot see is a 404",
      _r.status_code == 404, _r.status_code)

# The reply parser, which decides what the household is told the job found.
check("  the answer joins every text part, not just the first",
      app._code_task_text({"parts": [
          {"type": "text", "text": "Looking now."},
          {"type": "tool", "text": "ignored"},
          {"type": "text", "text": "Fixed the timeout."}]})
      == "Looking now.\n\nFixed the timeout.")
check("  and an answer with no text is empty rather than a stray part",
      app._code_task_text({"parts": [{"type": "tool", "text": "ran tests"}]})
      == "")

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")
