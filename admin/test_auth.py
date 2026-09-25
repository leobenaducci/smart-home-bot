#!/usr/bin/env python3
"""The admin page's front door.

Run: python admin/test_auth.py

This page edits the household's settings and credentials and can start a
deploy, so the only interesting question is whether anything at all answers
without a session. It used to be everything: the page was unauthenticated
"because it is on the LAN", which is not a thing the app can know -- published
on a host with a public address it was reachable by anyone.
"""
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


tmp = Path(tempfile.mkdtemp(prefix="admin-auth-"))
(tmp / "config").mkdir()
(tmp / "secrets").mkdir()
(tmp / "config" / "home-stack.yml").write_text(
    "site:\n  name: Test House\nlocale:\n  default: en\nservices: {}\n")
SECRETS = tmp / "secrets" / "smart-home-bot.env"
SECRETS.write_text("")

os.environ["HOME_STACK_CONFIG"] = str(tmp / "config" / "home-stack.yml")
os.environ["HOME_STACK_SECRETS"] = str(SECRETS)
os.environ["HOME_STACK_MODELS_CACHE"] = str(tmp / "config" / "models.json")
# The user store and everything else the page can write, under the scratch
# tree. Set before app is imported: these become module-level constants there,
# and the default is the machine's own /var/lib/home-stack.
for _var, _sub in (("HOME_STACK_STATE_DIR", "state"),
                   ("HOME_STACK_CONFIG_DIR", "config"),
                   ("HOME_STACK_MEDIA_DIR", "media"),
                   ("HOME_STACK_BACKUPS_DIR", "backups"),
                   ("HOME_STACK_PLUGINS_DIR", "plugins"),
                   ("HOME_STACK_DEPLOY_ROOT", "deployroot")):
    os.environ[_var] = str(tmp / _sub)
os.environ["ADMIN_SECRET_KEY"] = "test" * 8

sys.path.insert(0, str(HERE))
try:
    import bcrypt  # noqa: F401
except ImportError:
    print("SKIP: bcrypt is not installed (pip install -r admin/requirements.txt)")
    raise SystemExit(0)

app_mod = importlib.import_module("app")
app_mod.app.config.update(TESTING=True)
client = app_mod.app.test_client()

# Every route that changes something, plus the read-only ones. `/healthz` is
# deliberately absent: a container healthcheck must not need a credential.
GUARDED = ["/", "/services", "/site", "/members", "/secrets", "/models", "/deploy"]

# --- with no password set: only the setup screen answers --------------------

for path in GUARDED:
    r = client.get(path, follow_redirects=False)
    check(f"{path} redirects to setup before a password exists",
          r.status_code == 302 and "/setup" in r.headers.get("Location", ""),
          f"{r.status_code} {r.headers.get('Location')}")

check("/healthz answers without a session",
      client.get("/healthz").status_code == 200)

r = client.get("/setup")
check("the setup screen renders", r.status_code == 200, r.status_code)

# --- setting the password ---------------------------------------------------


def csrf():
    """A token from a page the client has actually loaded."""
    with client.session_transaction() as sess:
        return sess.setdefault("_csrf", "t" * 32)


# One under the minimum, taken from the constant rather than written out, so
# this tracks the rule instead of happening to sit below it.
too_short = "a" * (app_mod.MIN_PASSWORD_LENGTH - 1)
r = client.post("/setup", data={"password": too_short, "password2": too_short,
                                "_csrf": csrf()}, follow_redirects=False)
check(f"a password of {len(too_short)} is refused",
      r.status_code == 200 and "ADMIN_PASSWORD_HASH="
      not in SECRETS.read_text().replace("ADMIN_PASSWORD_HASH=\n", ""),
      r.status_code)
check("and the form says how long it has to be",
      str(app_mod.MIN_PASSWORD_LENGTH) in r.get_data(as_text=True),
      "the hint has to carry the number, or the refusal is unexplained")

r = client.post("/setup", data={"password": "test-long-enough-password",
                                "password2": "different-one-entirely",
                                "_csrf": csrf()})
check("a mismatch is refused", r.status_code == 200, r.status_code)

r = client.post("/setup", data={"password": "test-long-enough-password",
                                "password2": "test-long-enough-password",
                                "_csrf": csrf()}, follow_redirects=False)
check("setting the password signs you in", r.status_code == 302, r.status_code)

stored = app_mod.get_secret("ADMIN_PASSWORD_HASH")
check("the hash is stored, not the password", stored.startswith("$2"), stored[:12])
check("the plaintext is nowhere in the file",
      "test-long-enough-password" not in SECRETS.read_text())

check("setup stops answering once a password exists",
      client.get("/setup", follow_redirects=False).status_code == 302)

# --- a signed-in session reaches the pages ----------------------------------

r = client.get("/", follow_redirects=False)
check("the overview opens with a session", r.status_code == 200, r.status_code)

# --- a stranger does not -----------------------------------------------------

stranger = app_mod.app.test_client()
for path in GUARDED:
    r = stranger.get(path, follow_redirects=False)
    check(f"{path} sends a stranger to the login form",
          r.status_code == 302 and "/login" in r.headers.get("Location", ""),
          f"{r.status_code} {r.headers.get('Location')}")

r = stranger.post("/site", data={"name": "Taken Over"}, follow_redirects=False)
check("a stranger's POST is refused, not redirected into losing its body",
      r.status_code in (401, 403), r.status_code)

check("a wrong password does not sign anyone in",
      not app_mod.check_admin_password("not-the-password"))
check("the right password does", app_mod.check_admin_password("test-long-enough-password"))

# --- changing the password ends other sessions ------------------------------

app_mod.set_admin_password("test-second-long-password")
r = client.get("/", follow_redirects=False)
check("changing the password signs other browsers out",
      r.status_code == 302 and "/login" in r.headers.get("Location", ""),
      f"{r.status_code} {r.headers.get('Location')}")

# --- the login form itself ---------------------------------------------------

fresh = app_mod.app.test_client()
with fresh.session_transaction() as sess:
    sess["_csrf"] = "t" * 32
r = fresh.post("/login", data={"password": "test-second-long-password",
                               "_csrf": "t" * 32}, follow_redirects=False)
check("the login form accepts the current password", r.status_code == 302, r.status_code)

# An open redirect on a login form is how a convincing phishing link is built.
with fresh.session_transaction() as sess:
    sess["_csrf"] = "t" * 32
r = fresh.post("/login", data={"password": "test-second-long-password",
                               "next": "//evil.example.com/",
                               "_csrf": "t" * 32}, follow_redirects=False)
check("an off-site `next` is ignored",
      "evil.example.com" not in r.headers.get("Location", ""),
      r.headers.get("Location"))

# --- a malformed hash locks the door, it does not open it -------------------

app_mod.set_secret("ADMIN_PASSWORD_HASH", "not-a-bcrypt-hash")
check("a corrupt hash authenticates nobody",
      not app_mod.check_admin_password("anything"))
check("a corrupt hash does not crash the page",
      app_mod.app.test_client().get("/", follow_redirects=False).status_code == 302)

# --- the container has to be reachable from outside itself -------------------
#
# admin/Dockerfile's CMD is `python /app/admin/app.py`, so the __main__ branch
# below is how the container starts -- not a dev convenience.
#
# The container is on the host's network, so there is no port publish between a
# container side and a host side: what the app binds is what the network can
# reach, and it is the only thing that decides it. So the default here has to
# be the closed one.
#
# It was the opposite before, and correctly so. With
# `${ADMIN_BIND:-127.0.0.1}:${ADMIN_PORT}:8099` in the compose file the publish
# decided reach, `0.0.0.0` inside was the only address it could forward to, and
# binding the container's own loopback made the page unreachable while the
# healthcheck -- curling 127.0.0.1 from inside -- went on reporting healthy.
# That shipped for one commit. Host networking makes the two addresses one, and
# the direction of the safe default flips with it.
app_src = (HERE / "app.py").read_text(encoding="utf-8")
compose = (HERE / "docker-compose.yml").read_text(encoding="utf-8")
run_call = app_src[app_src.index("    app.run("):].split(")")[0]

check("the app binds what it is told, and nothing wider",
      'ADMIN_BIND_INSIDE' in run_call, run_call)
check("and closed is the default, because this bind is the whole of who can reach it",
      '"127.0.0.1"' in run_call and '"0.0.0.0"' not in run_call, run_call)
check("the compose file hands it the household's choice",
      "ADMIN_BIND_INSIDE: ${ADMIN_BIND:-127.0.0.1}" in compose,
      [l for l in compose.splitlines() if "ADMIN_BIND" in l])
check("which still defaults to loopback there too",
      "${ADMIN_BIND:-127.0.0.1}" in compose)
check("and there is no port publish left to disagree with it",
      "\n    ports:" not in compose,
      "a publish beside network_mode: host is refused by compose, and two "
      "things deciding reach is how this went wrong the first time")

# --- the session cookie has to be storable by the browser that got it --------
#
# A login that fails with "This form was not issued by this page" is almost
# always this: no session cookie, so no CSRF token to compare against. The
# cause here was an after_request hook that set SESSION_COOKIE_SECURE from
# `request.is_secure` -- a global, process-wide flag set from one request's
# scheme, which nothing reset. One https probe against a page published on a
# LAN latched it, and every http login afterwards failed with a message about
# forms and sessions that named neither cause nor cure.
app_src = (HERE / "app.py").read_text(encoding="utf-8")
# Code, not prose: the comment above the fix explains the bug and names it.
app_code = "\n".join(l for l in app_src.splitlines()
                     if not l.lstrip().startswith("#"))
check("nothing sets the cookie flag from a single request",
      "request.is_secure" not in app_code,
      "SESSION_COOKIE_SECURE is process-wide; deciding it per request latches")
check("it is decided from configuration instead",
      "ADMIN_COOKIE_SECURE" in app_src)
check("and it is off unless somebody turns it on",
      app_mod.app.config.get("SESSION_COOKIE_SECURE") is False,
      app_mod.app.config.get("SESSION_COOKIE_SECURE"))

# The end-to-end shape: fetch the form, keep the cookie, post it back.
fresh_client = app_mod.app.test_client()
page = fresh_client.get("/login")
_cookie = app_mod.app.config["SESSION_COOKIE_NAME"]
check("the login page issues a session cookie",
      any(h.startswith(_cookie + "=") for h in page.headers.getlist("Set-Cookie")),
      page.headers.getlist("Set-Cookie"))
# A name of its own. Cookies are scoped to a host and ignore the port, so this
# page and the portal -- two Flask apps, two signing keys, one machine -- both
# wrote `session` for whatever name a household reaches them by. The second
# overwrites the first, whichever page you opened last wins, and the other sees
# an empty session: no CSRF token, and a login that refuses the form however
# many times you reload it.
check("  and it is not the default name the portal also uses",
      _cookie != "session", _cookie)
import re as _re
_m = _re.search(r'name="_csrf" value="([^"]+)"', page.get_data(as_text=True))
check("and the form carries the token that cookie holds", _m is not None)
if _m:
    posted = fresh_client.post("/login", data={"password": "test-not-the-password",
                                               "_csrf": _m.group(1)})
    body = posted.get_data(as_text=True)
    check("posting it back is not refused as a stale form",
          "issued by an older page" not in body and "No session cookie" not in body,
          "the CSRF round trip is broken")

# The two failures are different questions and get different answers: a cookie
# that never arrived is not a form from an older page, and saying "or" meant
# every report of this arrived without the one bit that narrows it.
_no_cookie = app_mod.app.test_client()
_r = _no_cookie.post("/login", data={"password": "x", "_csrf": "anything"})
check("a POST with no session says the cookie is missing",
      "No session cookie" in _r.get_data(as_text=True), _r.get_data(as_text=True)[:80])
_stale = app_mod.app.test_client()
_stale.get("/login")
_r2 = _stale.post("/login", data={"password": "x", "_csrf": "not-the-token"})
check("  and a wrong token says the page is old, not that the cookie is gone",
      "issued by an older page" in _r2.get_data(as_text=True),
      _r2.get_data(as_text=True)[:80])

# --- the browser's own background requests must not break the form ---------
#
# The reported symptom: "This form was not issued by this page, or your session
# expired", on every first attempt, in incognito, with the right password.
#
# The cause was the auth guard calling session.clear() for *any* request
# without a current session -- including one from a visitor who has simply not
# logged in yet. Every browser asks for /favicon.ico as soon as a page loads.
# That had no route, fell through the guard, and answered
# `Set-Cookie: session=; Expires=1970`, deleting the CSRF token the login form
# on screen had been rendered with. The form was then unusable, and the log
# showed a cheerful 200 for the request that broke it.
browser = app_mod.app.test_client()
page = browser.get("/login?next=/")
import re as _re2
_tok = _re2.search(r'name="_csrf" value="([^"]+)"', page.get_data(as_text=True))
check("the login page renders a token", _tok is not None)

fav = browser.get("/favicon.ico")
check("a favicon request is answered without the auth guard",
      fav.status_code in (200, 204), fav.status_code)
check("and it does not expire the session cookie",
      not any("Expires=Thu, 01 Jan 1970" in h
              for h in fav.headers.getlist("Set-Cookie")),
      fav.headers.getlist("Set-Cookie"))

if _tok:
    after = browser.post("/login?next=/", data={"password": "test-wrong-but-well-formed",
                                                "_csrf": _tok.group(1)})
    body = after.get_data(as_text=True)
    check("and the form still works after it",
          after.status_code != 403 and "not issued by this page" not in body,
          f"{after.status_code}: the browser's own favicon request broke the form")

# --- writing the secrets file ------------------------------------------------
# Three things, each of which fails silently or catastrophically.
print("\nthe secrets file survives being written")

import os as _os          # noqa: E402
import stat as _stat      # noqa: E402
import threading as _thr  # noqa: E402

app_mod.set_secret("PROBE_ONE", "first")
app_mod.set_secret("PROBE_TWO", "second")
check("a key round-trips", app_mod.get_secret("PROBE_ONE") == "first")
check("and setting a second keeps the first",
      app_mod.get_secret("PROBE_ONE") == "first"
      and app_mod.get_secret("PROBE_TWO") == "second")

_mode = _stat.S_IMODE(_os.stat(app_mod.SECRETS).st_mode)
check("the file is owner-only", _mode == 0o600, oct(_mode))
# The temporary is where every secret in the house lands before the rename, and
# the rename is allowed to fail -- so it must never exist group- or
# world-readable, and it must not be left behind holding them.
# Checked at the moment of the rename rather than after it: on success the
# temporary is gone, so "it is not lying around" passes even when it was
# created 0644 and held every credential in the house while it was written.
_seen = {}
_real_put = app_mod._replace_contents


def _spy(tmp, target):
    # The last moment the temporary exists holding every secret in the house.
    _seen["mode"] = _stat.S_IMODE(_os.stat(tmp).st_mode)
    return _real_put(tmp, target)


app_mod._replace_contents = _spy
try:
    app_mod.set_secret("PROBE_MODE", "x")
finally:
    app_mod._replace_contents = _real_put
check("the temporary is owner-only while it holds them",
      _seen.get("mode") == 0o600, oct(_seen.get("mode", 0)))
_tmp = app_mod.SECRETS.with_suffix(".tmp")
check("and is not left behind", not _tmp.exists())
app_mod.delete_secret("PROBE_MODE")

# In the container this path is a single-file bind mount: a rename over it
# raises EBUSY, and a rename that *succeeded* would be worse -- it would put a
# new inode there and leave every running container reading the old file.
#
# This used to patch `Path.replace` to raise and check the write survived. It
# stopped meaning anything the moment `_write_secrets_unlocked` started going
# through `_replace_contents`, which writes the destination rather than renaming
# onto it: the patch was never reached, so the check passed no matter what the
# code did. Assert the real property instead -- that no rename is attempted at
# all -- which is both the reason EBUSY cannot happen and the reason the inode
# survives.
_renamed = []
_real_replace = Path.replace


def _spy_replace(self, target):
    _renamed.append((str(self), str(target)))
    raise OSError(16, "Device or resource busy")


Path.replace = _spy_replace
try:
    app_mod.set_secret("PROBE_THREE", "written anyway")
    _ok = app_mod.get_secret("PROBE_THREE") == "written anyway"
except OSError as exc:
    _ok = False
    print(f"    (raised {exc})")
finally:
    Path.replace = _real_replace
check("the secrets write succeeds", _ok)
check("  because it never renames onto the mount point", not _renamed, _renamed)
check("  and the earlier keys are still there",
      app_mod.get_secret("PROBE_ONE") == "first"
      and app_mod.get_secret("PROBE_TWO") == "second")

# Read-modify-write under one lock. Two separately-locked halves lose whichever
# key was set by the writer that read first.
for _k in ("PROBE_ONE", "PROBE_TWO", "PROBE_THREE"):
    app_mod.delete_secret(_k)
_names = [f"CONC_{i:02d}" for i in range(12)]
_threads = [_thr.Thread(target=app_mod.set_secret, args=(n, n.lower()))
            for n in _names]
for _t in _threads:
    _t.start()
for _t in _threads:
    _t.join()
_missing = [n for n in _names if app_mod.get_secret(n) != n.lower()]
check("twelve writers at once lose nothing", not _missing, f"lost: {_missing}")
for _n in _names:
    app_mod.delete_secret(_n)
check("and delete really removes a key",
      app_mod.get_secret(_names[0]) == "")

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")
