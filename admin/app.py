#!/usr/bin/env python3
"""The admin page.

Local settings for the whole stack: what runs, where, who lives here, and which
credentials exist. It is the intended way to edit config/home-stack.yml — the
file stays hand-editable, but this is what a person uses.

Two things it deliberately does not do:

  * It never shows a secret back. Values are write-only: once set, the page
    reports only whether a key is present. A settings page that renders your
    API keys is a settings page that leaks them to whoever walks past the
    screen it is open on.

  * It never deploys as a side effect of saving. Saving writes config; a change
    is saved and waiting until you press Deploy. That was true of the Jenkins
    jobs this replaced and it is worth keeping — surprise deploys are how a
    house loses its lights at dinner time.

Password-protected, and closed until it is. There is no default password and
none is generated for you: until a hash exists the only page that answers is
the setup screen, so a fresh install cannot be open and unattended. Access here
is equivalent to root on the machine — see docs/admin.md.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import secrets as secrets_mod
import shutil
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import (Flask, Response, flash, g, get_flashed_messages, has_request_context,
                   jsonify, redirect, render_template, request, session, url_for)

# Where the package lives. In a container the deployer stages i18n/, deploy/
# and config/ *beside* this file, so the default is the app's own directory;
# from a checkout it is the repository root. HOME_STACK_ROOT overrides both.
#
# This used to default to the repository root and be set to /stack in the
# container, while the compose file used the same variable as the host-side
# mount source -- so /stack was mounted from a host path that did not exist and
# every import failed.
_HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("HOME_STACK_ROOT")
            or (_HERE if (_HERE / "i18n").is_dir() else _HERE.parent))
sys.path.insert(0, str(ROOT / "i18n"))

import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402
import urllib.error  # noqa: E402
import yaml  # noqa: E402
from i18n import Translator  # noqa: E402

import models as model_catalogue  # noqa: E402
import bench as model_bench  # noqa: E402

# Round-trip YAML, so saving from this page keeps the comments. The config file
# is documented as hand-editable and carries about sixty lines explaining why
# each setting is what it is; a plain safe_dump silently deleted all of them on
# the first Save, which is a bad trade for a checkbox.
try:
    from ruamel.yaml import YAML as _RoundTripYAML
except ImportError:  # pragma: no cover - install.sh installs it
    _RoundTripYAML = None


def _round_trip_yaml():
    """A fresh parser per call, or None when ruamel is not installed.

    Deliberately not one shared instance. A `ruamel.YAML()` carries parser state
    across a load, and this page reads the config from more than one thread --
    the model-price watcher runs on its own and reads the same file a request
    handler is reading. Sharing one produced `'NoneType' object has no attribute
    'anchor'` from somewhere inside the composer: an opaque 500 on whichever
    caller lost the race, on a page whose job is to be the way out of a broken
    config. Constructing one is cheap; the race is not.
    """
    if _RoundTripYAML is None:
        return None
    parser = _RoundTripYAML()
    parser.preserve_quotes = True
    parser.width = 100
    return parser


# cloud.ollama.instances -- the deployer's own reading of it, so the page and
# the deploy cannot disagree about which Ollama a role is on.
sys.path.insert(0, str(ROOT / "deploy"))
import ollama_instances as OI  # noqa: E402

CONFIG = Path(os.environ.get("HOME_STACK_CONFIG", ROOT / "config" / "home-stack.yml"))
MANIFEST = ROOT / "deploy" / "manifest.yml"
SECRETS = Path(os.environ.get("HOME_STACK_SECRETS", ROOT / "secrets" / "smart-home-bot.env"))
DEPLOYER = ROOT / "deploy" / "deploy.py"
# The model catalogue, cached where the rest of the stack keeps state so a
# redeploy does not throw away a fetch and a container restart does not
# send everybody back to models.dev at once.
MODELS_CACHE = Path(os.environ.get("HOME_STACK_MODELS_CACHE",
                                   CONFIG.parent / "models.json"))
# What each role's own probe last measured about the model it points at.
# Beside the catalogue and for the same reason: it is state, it survives a
# redeploy, and it is not worth re-earning on a container restart -- a score
# only moves when the model or its endpoint does.
SCORES_CACHE = Path(os.environ.get("HOME_STACK_MODEL_SCORES",
                                   MODELS_CACHE.parent / "model-scores.json"))

app = Flask(__name__)
# Supplied, or every deploy of this page logs everybody out of it. Falling
# back to a fresh key per process means the session cookie in somebody's
# browser can no longer be decrypted, so `session` is empty, so there is no
# CSRF token to compare against -- and the login says "This form was not
# issued by this page, or your session expired", which reads like a bug in the
# form rather than like a container that was replaced under them.
app.secret_key = os.environ.get("ADMIN_SECRET_KEY") or secrets_mod.token_hex(32)
# The session cookie carries the only thing standing between a stranger and the
# household's credentials, so it is scoped as tightly as a browser allows.
# Secure is decided per-request, in _harden_cookie: this page is served over
# plain HTTP on a LAN and over TLS through the proxy, and a Secure cookie on
# the former is a login that silently never sticks.
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  # A name of its own, because cookies are scoped to a *host*
                  # and ignore the port. This page and the portal are two Flask
                  # apps on one machine, so on a household that reaches them by
                  # the same name -- portal.home:21002 and portal.home:21001 --
                  # both were writing `session` for host portal.home, each
                  # signed with its own key. The second overwrites the first,
                  # whichever page you opened last wins, and the other one then
                  # sees an empty session: no CSRF token to compare against, and
                  # a login that answers "This form was not issued by this page"
                  # however many times you reload it.
                  SESSION_COOKIE_NAME="home_stack_admin")
translator = Translator(ROOT / "i18n")

def t(key: str, **params) -> str:
    """Translate outside a template. `t` is a template global everywhere else;
    the auth code needs the same strings in Python, and must not fail because
    the config is unreadable -- an unreadable config is exactly when somebody
    is trying to log in and fix it."""
    # The locale is resolved once per request, not once per string. `t` is
    # called for every label on a page -- 149 times on the Models page alone --
    # and `load_config()` parses the whole commented home-stack.yml with
    # ruamel each time: 118 ms a call, 19 s of a 20 s render, 97% of it. The
    # answer cannot change inside one request, so it is worked out once.
    # Outside a request (startup, the price watcher) there is nowhere to keep
    # it and nothing calling it in a loop, so it behaves exactly as before.
    try:
        if has_request_context():
            locale = getattr(g, "_locale", None)
            if locale is None:
                locale = current_locale(load_config())
                g._locale = locale
        else:
            locale = current_locale(load_config())
    except Exception:
        locale = "en"
    return translator(key, locale=locale, **params)


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
# This page edits the household's configuration and its credentials, and can
# start a deploy. Access to it is equivalent to root on the machine, so it is
# not something to leave open and describe in a doc.
#
# It used to be unauthenticated "because it is on the LAN". That assumption is
# not the page's to make: published on a host with a public address it is
# reachable by anyone, and nothing in the app could tell the difference.
#
# There is no default password and none is generated on your behalf. Until a
# hash exists every route redirects to /setup, which is the only page that
# answers -- so a fresh install is closed rather than open, and the first
# person to reach it sets the password rather than discovering somebody else
# already did.
ADMIN_PASSWORD_KEY = "ADMIN_PASSWORD_HASH"

# A floor, not a policy. It stops an empty box and a slip of the keyboard; it
# is not what makes the page hard to get into. That is bcrypt, plus the
# five-attempts-per-900s lockout below, plus the page not being reachable
# through either proxy.
#
# Long enough is still better here than anywhere else in the stack -- this page
# holds the docker socket, the secrets file and a Deploy button -- so the hint
# on the setup screen says the number and the choice is the household's.
MIN_PASSWORD_LENGTH = 12

# Failed attempts, per process. bcrypt already makes guessing expensive; this
# turns a long run of guesses into a wait rather than a rate.
_failures: dict[str, list] = {}
_FAIL_WINDOW_S = 900
_FAIL_LIMIT = 5


def _bcrypt():
    """Imported lazily so a missing wheel is a clear message, not an ImportError
    at startup that reads as the whole page being broken."""
    try:
        import bcrypt
    except ImportError:  # pragma: no cover - install.sh installs it
        raise RuntimeError(
            "bcrypt is not installed; the admin page cannot check a password. "
            "pip install -r admin/requirements.txt"
        )
    return bcrypt


def admin_password_hash() -> str:
    return get_secret(ADMIN_PASSWORD_KEY)


def set_admin_password(password: str) -> None:
    """Store a new password as a bcrypt hash. The plaintext is never written."""
    bcrypt = _bcrypt()
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    set_secret(ADMIN_PASSWORD_KEY, hashed)


def check_admin_password(password: str) -> bool:
    stored = admin_password_hash()
    if not stored:
        return False
    bcrypt = _bcrypt()
    try:
        return bcrypt.checkpw(password.encode(), stored.encode())
    except ValueError:
        # A malformed hash must not authenticate anybody, and must not 500
        # either -- that would take the whole page down over one bad line.
        return False


def _throttled(ip: str) -> int:
    """Seconds the caller must wait, or 0. Old failures fall out of the window."""
    hits = [t for t in _failures.get(ip, []) if time.time() - t < _FAIL_WINDOW_S]
    _failures[ip] = hits
    if len(hits) < _FAIL_LIMIT:
        return 0
    return int(_FAIL_WINDOW_S - (time.time() - hits[-_FAIL_LIMIT]))


def _session_is_current() -> bool:
    """A session is only good while it matches the password that issued it.

    Changing the password therefore signs every other browser out, which is the
    behaviour somebody changing it after a laptop goes missing is counting on.
    """
    if not session.get("authed"):
        return False
    stored = admin_password_hash()
    return bool(stored) and session.get("pw") == _hash_fingerprint(stored)


def _hash_fingerprint(stored: str) -> str:
    import hashlib
    return hashlib.sha256(stored.encode()).hexdigest()[:16]


# Everything that answers without a session. `/healthz` is here so a container
# healthcheck does not need a credential; it returns no information.
_OPEN_ENDPOINTS = {"login", "setup", "healthz", "static", "favicon"}


@app.before_request
def require_admin_login():
    if request.endpoint in _OPEN_ENDPOINTS:
        return None
    if not admin_password_hash():
        return redirect(url_for("setup"))
    if not _session_is_current():
        # Only clear a session that was signed in. For a visitor who has not
        # logged in there is nothing to invalidate, and clearing takes the CSRF
        # token the login form was rendered with -- so the next request from
        # the browser destroys the token belonging to the form already on
        # screen, and submitting it is refused as "not issued by this page".
        #
        # Every page load does exactly that, because every browser asks for
        # /favicon.ico. The favicon has no route, so it reached this guard,
        # which answered `Set-Cookie: session=; Expires=1970` and logged a
        # cheerful 200. The login form was unusable on the first attempt every
        # time, and worked after a reload only because the second load raced
        # the favicon in the other order.
        if session.get("authed"):
            session.clear()
        if request.method != "GET":
            # A POST bounced to a login form would lose its body and look like
            # it silently did nothing.
            return (t("admin.auth.expired"), 401)
        return redirect(url_for("login", next=request.path))
    return None


# Decided once, from configuration -- not per request, and never by mutating
# `app.config` from inside a handler.
#
# This used to be an after_request hook that set SESSION_COOKIE_SECURE = True
# whenever `request.is_secure`, which latched: the flag is global and process
# wide, nothing ever set it back, and from that moment every response marked
# the session cookie Secure -- including the http ones. Browsers will not store
# a Secure cookie from an http origin, so there was no session, so there was no
# CSRF token to compare against, and every login said "This form was not issued
# by this page". One https request to a page published on a LAN was enough, and
# on a machine with a public address that is any scanner in the world.
#
# Off by default because this page is plain http on a household network by
# design. Set ADMIN_COOKIE_SECURE=1 when it is genuinely behind TLS -- and only
# then, because turning it on without TLS locks you out in exactly the way
# described above.
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("ADMIN_COOKIE_SECURE", "").strip().lower()
    in ("1", "true", "yes", "on"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """First run: choose the password. Available only while none is set."""
    if admin_password_hash():
        return redirect(url_for("login"))
    error = ""
    if request.method == "POST":
        pw = request.form.get("password", "")
        again = request.form.get("password2", "")
        if len(pw) < MIN_PASSWORD_LENGTH:
            error = t("admin.auth.too_short", n=MIN_PASSWORD_LENGTH)
        elif pw != again:
            error = t("admin.auth.mismatch")
        else:
            set_admin_password(pw)
            session.clear()
            session["authed"] = True
            session["pw"] = _hash_fingerprint(admin_password_hash())
            return redirect(url_for("overview"))
    return render_template("setup.html", error=error,
                           min_length=MIN_PASSWORD_LENGTH)


@app.route("/login", methods=["GET", "POST"])
def login():
    if not admin_password_hash():
        return redirect(url_for("setup"))
    error = ""
    if request.method == "POST":
        ip = request.remote_addr or "?"
        wait = _throttled(ip)
        if wait:
            error = t("admin.auth.locked", n=max(1, wait // 60))
        elif check_admin_password(request.form.get("password", "")):
            _failures.pop(ip, None)
            session.clear()
            session["authed"] = True
            session["pw"] = _hash_fingerprint(admin_password_hash())
            nxt = request.form.get("next") or url_for("overview")
            # Only ever back into this site: an open redirect on a login form
            # is how a convincing phishing link gets built.
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("overview")
            return redirect(nxt)
        else:
            _failures.setdefault(ip, []).append(time.time())
            error = t("admin.auth.wrong")
    return render_template("login.html", error=error,
                           next=request.args.get("next", ""))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# Request guards
# --------------------------------------------------------------------------
# A session is required for every route, but CSRF is a separate problem: a
# signed-in browser is exactly what a cross-site form post borrows. A plain form
# submission needs
# no preflight and no CORS approval, so a page on the internet could set a
# credential here, or start a full-house deploy, without ever reading a
# response. Two independent checks, because either alone has a bypass.

# Keys whose *shape* is decided by whatever reads them, not by us.
#
# Everything else here is "32 random bytes, written however"; these are not.
# `PROJECTS_KEY` is handed to Fernet, which requires exactly 32 bytes
# base64url-encoded -- 44 characters. Generated with the usual `token_hex(32)`
# it is 64 hex characters, decodes to 48 bytes, and Fernet refuses it at
# import: the portal logs "PROJECTS_KEY is set but unusable" once and the
# credentials page tells the household to ask an administrator for a key it
# already has. A generator that does not know the shape produces a secret that
# is present, plausible and wrong.
def _fernet_key() -> str:
    import base64
    import os as _os
    return base64.urlsafe_b64encode(_os.urandom(32)).decode()


SECRET_SHAPES = {"PROJECTS_KEY": _fernet_key}


def generate_secret(key: str) -> str:
    """A new value for `key`, in whatever shape its reader demands."""
    maker = SECRET_SHAPES.get(key)
    return maker() if maker else secrets_mod.token_hex(32)


def csrf_token() -> str:
    token = session.get("_csrf")
    if not token:
        token = secrets_mod.token_urlsafe(32)
        session["_csrf"] = token
    return token


@app.before_request
def guard_state_changing_requests():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None

    # 1. The form must carry this session's token.
    sent = request.form.get("_csrf", "")
    expected = session.get("_csrf", "")
    # Two different failures, said differently, because they have different
    # answers. No session at all is a cookie that never arrived -- reloading
    # fixes it. A token that does not match is a form from an older page, and
    # reloading is also the answer but for a different reason. Saying "or"
    # meant every report of this arrived without the one bit that narrows it.
    if not expected:
        return ("No session cookie came back with this form. Reload the page "
                "and sign in again; if it keeps happening, something else is "
                "writing a cookie for this host.", 403)
    if not secrets_mod.compare_digest(sent, expected):
        return ("This form was issued by an older page. Reload and try again.",
                403)

    # 2. And it must have come from this origin. A cross-site form post carries
    #    an Origin header on every browser that matters; a same-origin one from
    #    our own page matches. Missing Origin falls back to Referer.
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if origin:
        from urllib.parse import urlparse
        if urlparse(origin).netloc != urlparse(request.base_url).netloc:
            return ("Cross-site form submissions are refused.", 403)

    return None


@app.context_processor
def inject_csrf():
    return {"csrf_token": csrf_token}


# --------------------------------------------------------------------------
# Reading a price column
# --------------------------------------------------------------------------
# The roster puts $3.00 and $0.0028 in the same column -- three orders of
# magnitude, and with Together's roster on, 125 rows of it. Jinja's own repr
# printed `$0.5` above `$0.0028` above `$3`, so the decimal points did not line
# up and comparing two rows meant reading both numbers rather than glancing
# down the column. `.num` already sets tabular figures; what was missing was a
# consistent number of places to line up.
#
# Two places is the common case. A sub-cent price keeps its significant digits
# instead of rounding, because the number it must never be confused with is
# zero: `cache_read` at $0.003625 and a model that is genuinely free are the
# two ends of this page's whole argument, and `$0.00` for the first one loses
# it. `None` stays None here and the template prints an em dash -- "not
# published" is not a price, and it is not free either.


@app.template_filter("per_million")
def per_million(value) -> str | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v == 0:
        return "$0"
    if v < 0.01:
        # Six places is what models.py keeps, so this neither invents
        # precision nor drops any that was fetched.
        digits = f"{v:.6f}".rstrip("0")
        # Below what six places can show, every decimal is a zero and the
        # rstrip leaves a bare "$0." -- which is both malformed and the one
        # reading this filter exists to prevent, a real price shown as free.
        return "$<0.000001" if digits.endswith(".") else "$" + digits
    return f"${v:,.2f}"


@app.template_filter("token_count")
def token_count(value) -> str:
    """A context window at a glance: 131k, 262k, 1.0M.

    `n // 1000` rendered 1,048,576 as `1048k`, which is the one number on the
    row somebody actually compares and the one shape that makes a million look
    like a thousand.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n // 1000}k"
    # The speech models report 448. Integer-dividing that by a thousand
    # printed `0k`, which in a context column reads as "cannot hold anything"
    # rather than as a small number.
    return str(n)


# --------------------------------------------------------------------------
# Config and secrets
# --------------------------------------------------------------------------

def load_config() -> dict:
    """Load for editing, preserving comments where ruamel is available."""
    if not CONFIG.exists():
        return {}
    parser = _round_trip_yaml()
    if parser is not None:
        with CONFIG.open() as fh:
            return parser.load(fh) or {}
    return yaml.safe_load(CONFIG.read_text()) or {}


def _replace_contents(tmp: Path, target: Path) -> None:
    """Put `tmp`'s bytes into `target`, keeping target's inode. Then drop tmp.

    Serialise-then-swap, with the swap done by writing rather than renaming,
    and the difference is not a detail: **both files this is used on were
    single-file bind mounts into every container that read them.** This page
    stopped mounting them that way on 2026-09-11 (see its docker-compose.yml),
    after a host-side rename orphaned it once more. The in-place write stays:
    it costs nothing, and any container that does mount one of them as a file
    -- a household's own compose, a service added later -- has the same pin.

    `tmp.replace(target)` is the usual way and it is wrong here, in two
    different ways depending on which side of the mount you are standing.

    *Inside* a container the rename raises EBUSY -- a mount point cannot be
    renamed over -- which at least fails loudly.

    *Outside* it, on the host, the rename succeeds and does something worse. It
    puts a **new inode** at that path, and every running container keeps the old
    one, because a bind mount resolves once at mount time and never again. The
    file on disk is correct, the page still shows the old contents, and nothing
    anywhere reports it. That is not hypothetical: registering a plugin from a
    host-side script wrote a fourth entry into `plugins:` that the admin
    container could not see, and only recreating the container fixed it. The
    same shape had already cost a day over `paths.plugins`.

    So the inode is preserved always, not only when the rename fails. What is
    given up is atomicity for readers -- a reader arriving mid-write can see a
    partial file -- and the trade is deliberate: `tmp` is already fully
    serialised and validated by the caller, so this is one `write_text` of a
    known-good buffer, milliseconds wide and loud when it fails. The other
    failure is silent, permanent, and looks exactly like a setting that did not
    save.

    `deploy/deploy.py` has the twin of this for the same two files. They are
    separate because the admin page loads the deployer lazily and may not have
    it, and this has to work before that question is asked.
    """
    data = tmp.read_text()
    try:
        target.write_text(data)
    finally:
        tmp.unlink(missing_ok=True)


def save_config(cfg: dict) -> None:
    """Write the config back.

    Written to a sibling file and renamed, so a crash mid-write cannot leave the
    stack with a half-parsed config — the deployer would refuse to run and the
    portal would refuse to start, from a file nobody knowingly edited.
    """
    tmp = CONFIG.with_suffix(".yml.tmp")
    parser = _round_trip_yaml()
    if parser is not None:
        with tmp.open("w") as fh:
            parser.dump(cfg, fh)
    else:
        tmp.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    _replace_contents(tmp, CONFIG)


def plugins_view(cfg: dict) -> list:
    """Each configured plugin: where it is, what it contributes, and whether it
    loads at all.

    `plugins:` was edited by hand in the config file, which is the one thing
    the rest of this page exists to avoid. It is also the setting most likely
    to be wrong in a way nothing tells you about: a directory that has moved
    makes `load_plugins` refuse the *whole list*, and the services page then
    quietly shows only what the package ships.

    So each row is loaded on its own. One that cannot be read says so, next to
    its path, and the others still appear.
    """
    deployer = _deployer()
    rows = []
    for entry in (cfg.get("plugins") or []):
        entry = str(entry).strip()
        if not entry:
            continue
        row = {"entry": entry, "name": entry, "path": entry, "error": "",
               "services": [], "tiles": []}
        if deployer is not None:
            try:
                # One plugin at a time: a `plugins:` list with a bad entry in
                # it must not cost the good ones their row.
                one = deployer.load_plugins({**cfg, "plugins": [entry]})[0]
                row["name"] = one["name"]
                row["path"] = str(one["root"])
                doc = one.get("doc") or {}
                row["services"] = sorted(doc.get("services") or {})
                row["tiles"] = [t.get("name", "") for t in (doc.get("tiles") or [])]
            except Exception as exc:  # noqa: BLE001 - the message is the point
                row["error"] = str(exc).split("\n")[0]
        rows.append(row)
    return rows


def _wired_port_keys(name: str, doc: dict) -> set:
    """The port keys this plugin service actually reads out of `services:`.

    A port is editable here only if the plugin interpolates it — `FINANCE_PORT:
    "{services.finance-helper.port}"`. Where the plugin writes the number
    literally instead, as smart-lights does with 5010 and backup-report with
    21600, `services.<name>.port` reaches nothing: the container would come up
    on the old number and the page would have reported a change that never
    happened. That is the setting-that-never-reaches-a-container this codebase
    keeps warning about, so those get shown and not offered.

    Scanned over the plugin's whole `plugin.yml` rather than the env block, or
    even the one service's spec, because a verify URL or a tile href
    referencing the port is just as good evidence that the plugin routes it
    through config — and `tiles:` is a sibling of `services:`, so a scan of
    the service alone cannot see it. Passing the whole document is safe: the
    pattern is anchored on this service's own name, so another service's port
    cannot be picked up by it.
    """
    found, pattern = set(), re.compile(
        r"\{services\." + re.escape(name) + r"\.([a-z0-9_]*port[a-z0-9_]*)\}")

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            found.update(pattern.findall(node))

    walk(doc)
    return found


def _plugin_service_detail(name: str, spec: dict, configured: dict | None) -> str:
    """What this plugin actually registers, as hover text on the name.

    A plugin service is somebody else's `plugin.yml` and the page shows two
    fields of it: a name and a sentence. That is the least informative row on
    this screen, and it is the one people are least likely to already know --
    the shipped services at least have a manifest in this repository. So the
    row carries the rest where it costs no space: what it builds, what it
    listens on, what it writes and what proves it came up.

    Plain text with newlines, because it is a `title=` attribute. Ports are
    read out of the unit's `env:` rather than from config, since that is where
    a plugin puts them and most do not route them through `services:` at all.
    """
    lines = [f"{name} — from the plugin's own plugin.yml",
             f"role: {spec.get('role', 'hub')}"]
    if configured and configured.get("port"):
        lines.append(f"configured port: {configured['port']}")
    for unit in (spec.get("units") or []):
        built = "built here" if unit.get("build") else "pulled image"
        lines.append(f"unit {unit.get('name', '?')} ({built}, {unit.get('dir', '?')}/)")
        # Ends with, not contains: `BACKUP_REPORT_BIND` has "PORT" inside
        # "REPORT" and is an address, not a port.
        ports = {k: v for k, v in (unit.get("env") or {}).items()
                 if k.upper().endswith("PORT")}
        for k, v in ports.items():
            lines.append(f"    {k}={v}")
        for st in (unit.get("state") or []):
            keep = st.get("backup", "essential")
            lines.append(f"    state {st.get('path', '?')}"
                         + ("" if keep == "essential" else f"  [{keep}]"))
        for check in (unit.get("verify") or []):
            target = check.get("http") or check.get("cmd") or ""
            if target:
                lines.append(f"    verify {target}")
    return "\n".join(lines)


def plugin_service_rows(cfg: dict) -> list:
    """The services the household's plugins declare, with the state of each.

    Same shape the shipped list uses, so the two read alike and the only
    difference on screen is which question they answer.
    """
    deployer = _deployer()
    rows = []
    if deployer is None:
        return rows
    try:
        plugins = deployer.load_plugins(cfg)
    except Exception:  # noqa: BLE001
        return rows
    configured = cfg.get("services") or {}
    for plugin in plugins:
        for name, spec in ((plugin.get("doc") or {}).get("services") or {}).items():
            rows.append({
                "name": name,
                "plugin": plugin["name"],
                "description": (spec.get("description") or "").strip(),
                "enabled": (configured.get(name) or {}).get("enabled", True),
                "detail": _plugin_service_detail(name, spec, configured.get(name)),
                # Only the keys the plugin actually interpolates. Everything
                # else is shown as text with a reason, never as an input.
                "ports": {k: v for k, v in (configured.get(name) or {}).items()
                          if k in _wired_port_keys(name, plugin.get("doc") or {})},
                "fixed_ports": sorted(
                    {str(v) for u in (spec.get("units") or [])
                     for k, v in (u.get("env") or {}).items()
                     if k.upper().endswith("PORT") and str(v).isdigit()}),
            })
    rows.sort(key=lambda r: (r["plugin"], r["name"]))
    return rows


def check_plugin(cfg: dict, entry: str, replacing: str = "") -> str:
    """"" if this plugin can be added, else why not.

    Everything `load_plugins` refuses -- no directory, no plugin.yml, a
    contract this deployer cannot read, a name already taken, a path inside the
    package tree -- with its own message, which says what to do about it.

    `replacing` is the entry this one would take the place of, for an edit. It
    is left out of both checks: out of the duplicate test, because an edit that
    changes nothing else about a row is not a household adding the same plugin
    twice, and out of the list `load_plugins` is asked to read, because the old
    path is usually the reason the row needs editing at all -- a directory that
    has moved makes `load_plugins` refuse the whole list, and validating the
    new path against a list still containing the broken old one would refuse
    every repair.
    """
    entry = (entry or "").strip()
    if not entry:
        return t("admin.plugins.err_empty")
    others = [str(e).strip() for e in (cfg.get("plugins") or [])
              if str(e).strip() != (replacing or "").strip()]
    if entry in others:
        return t("admin.plugins.err_already")
    deployer = _deployer()
    if deployer is None:
        return t("admin.plugins.err_no_deployer")
    try:
        deployer.load_plugins({**cfg, "plugins": others + [entry]})
    except Exception as exc:  # noqa: BLE001 - the deployer's message is better
        return str(exc).split("\n")[0]
    return ""


def household_services(cfg: dict) -> list:
    """Links to things this stack does not run.

    A name and a URL, typed on this page, drawn as a tile and never touched
    again. That is the whole of it, and it is worth keeping separate from the
    two lists above it: those are services something builds, deploys and
    checks, and this is a bookmark. They were briefly one list, and the page
    then had two headings a word apart -- "your services" and "your own
    services" -- describing three different things.
    """
    return [{**c, "source": "custom", "removable": True}
            for c in (cfg.get("custom_services") or [])]


def _interpolate_or_blank(deployer, value: str, cfg: dict) -> str:
    """A tile's href, or nothing if it names something this config has not got.

    A row with a blank link is a household seeing that the plugin is there and
    that its address cannot be worked out yet. An exception here is a page that
    will not open, which is strictly worse on the screen somebody would use to
    fix it.
    """
    try:
        return deployer.interpolate(value, cfg)
    except Exception:  # noqa: BLE001
        return ""


def _browsable_hub(cfg: dict) -> str:
    deployer = _deployer()
    try:
        return deployer.browsable_address(cfg, "hub")
    except Exception:  # noqa: BLE001
        return ((cfg.get("hosts") or {}).get("hub") or {}).get("address", "127.0.0.1")


def load_manifest() -> dict:
    """What this package ships, and only that. See `all_services_merged`."""
    return yaml.safe_load(MANIFEST.read_text()) if MANIFEST.exists() else {}


def all_services_merged(cfg: dict) -> dict:
    """Shipped services plus whatever the household's plugins declare.

    Kept apart from `load_manifest()` on purpose. This page had one list of
    everything, and it read as though `finance-helper` were something the
    package brings -- when it is a directory in that household's own
    repository, deployed from a plugin.yml they wrote. Where somebody is
    deciding what to switch on, "what this ships" and "what we added" are two
    questions, and the page now asks them separately.

    Merged through the deployer's own `all_services`, never a second copy of
    the walk.
    """
    raw = load_manifest()
    deployer = _deployer()
    if deployer is None or not raw:
        return (raw or {}).get("services", {}) or {}
    try:
        merged = dict(raw)
        merged["_plugins"] = deployer.load_plugins(cfg)
        return deployer.all_services(merged, cfg)
    except Exception:  # noqa: BLE001 - a broken plugin must not blank the page
        return raw.get("services", {}) or {}


def _deployer():
    """The deployer module, for the things this page must agree with it about.

    Imported rather than reimplemented. A second copy of plugin discovery here
    would drift from the one that actually deploys, and this page's whole job is
    to tell somebody which services a change affects -- being wrong about that
    is worse than not saying.

    Guarded because the page has to keep working when it cannot: a syntax error
    in the deployer must not take the one screen you would use to fix it.
    """
    try:
        spec = importlib.util.spec_from_file_location("deployer", DEPLOYER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 - any failure here is non-fatal
        return None


def house_only_capable() -> set:
    """Service names for which "house only" is a real switch.

    Read from the deployer rather than restated here. It works by keeping a
    *prefix* off the public copy of the proxy, so it only means something for a
    service the portal actually serves; everything else is linked by address and
    port and is house-only whatever anybody ticks, because the VPS forwards the
    portal and nothing else. A second copy of that list here would drift from the
    one that decides, and this page's whole job is to not be wrong about which.

    Empty when the deployer cannot be read, which offers no checkbox rather than
    offering one that might do nothing.
    """
    mod = _deployer()
    return set(getattr(mod, "HOUSE_ONLY_APPS", {}) or {}) if mod else set()


def folder_clash(cfg: dict, member_id: str, display_name: str) -> str | None:
    """The member this name's share folder would collide with, or None.

    Nobody types a folder: `deploy.share_folder()` slugifies the display name
    -- accents folded, spaces and punctuation dropped -- and everything keyed
    on a folder then keys on the result. Two people who slugify the same way
    ("Ana Pérez" and "Ana Perez", "Ana Maria" and "AnaMaria") get one folder
    between them, and nothing anywhere notices: the finance overlay renders two
    bind mounts onto `/workspace/<folder>/finance` and the second wins, so one
    member's dashboard opens the other's bank statements -- which is precisely
    the failure `deploy/compose_finance.py` was written to make impossible.
    Their assistants also share a FILE_SHARE_FOLDER.

    Refused here because this page is the only place members are named.
    """
    D = _deployer()
    if D is None:
        return None
    want = D.share_folder({"id": member_id, "display_name": display_name})
    for other in (cfg.get("members") or []):
        if str(other.get("id") or "").strip() == member_id:
            continue
        if D.share_folder(other) == want:
            return str(other.get("display_name") or other.get("id"))
    return None


def dns_targets(cfg: dict) -> dict:
    """Each `dns:` name and the address it has to resolve to.

    Nothing in this stack resolves these names -- every internal URL is an
    address, because a container retrying `getaddrinfo ENOTFOUND` forever is
    indistinguishable from the service being down. They are the names *people*
    type, so something on the network has to answer them: a Pi-hole, a router
    that does local DNS, a hosts file. That is a table somebody copies by hand,
    and copying it by hand means reading it off two screens and getting one
    wrong.

    The name -> role mapping is the deployer's `DNS_FALLBACK_HOST`, imported
    rather than restated: it is the same fact that decides what a blank name
    falls back to, and two copies of it would disagree the first time a service
    moved. If the deployer cannot be loaded, the roles are unknown and this
    says so rather than guessing the hub for everything.

    Each row also says whether the service behind it is running. A name is only
    a name -- nothing in this stack resolves one -- so a row for a service the
    household does not deploy is a line somebody copies into their Pi-hole for
    an address where nothing is listening, and then spends an evening on.
    `ntfy` is the case that prompted it: with `cloud.notifications.mode` set to
    `external` the ntfy service is not deployed, and the row sat there looking
    exactly like the ones that work.
    """
    deployer = _deployer()
    fallback = getattr(deployer, "DNS_FALLBACK_HOST", None) if deployer else None
    behind = getattr(deployer, "DNS_SERVICE", {}) if deployer else {}
    hosts = cfg.get("hosts") or {}
    services = cfg.get("services") or {}
    out = {}
    for alias, name in (cfg.get("dns") or {}).items():
        service = behind.get(alias)
        # Unknown means unknown, not off: a name this table has never heard of
        # is a household's own, and calling it dead would be a guess.
        live = True if not service else bool(
            (services.get(service) or {}).get("enabled", True))
        if fallback is None:
            out[alias] = {"role": "", "address": "", "service": service,
                          "live": live}
            continue
        role = fallback.get(alias, "hub")
        out[alias] = {"role": role,
                      "address": (hosts.get(role) or {}).get("address", ""),
                      "service": service, "live": live}
    return out


def load_plugins() -> list:
    """Configured plugins, or an empty list if they cannot be read."""
    deployer = _deployer()
    if deployer is None:
        return []
    try:
        return deployer.load_plugins(load_config())
    except Exception as exc:  # noqa: BLE001 - the page must survive a bad plugin
        # Never silently. The deployer refuses to run at all with a plugin it
        # cannot read, which is the right answer there; this page has to keep
        # answering, because it is where somebody would go to switch the
        # offending plugin off. So it degrades -- and says so in the log rather
        # than quietly showing a household its own services do not exist.
        print(f"admin: cannot read plugins: {exc}", file=sys.stderr, flush=True)
        return []


SECRET_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


@contextlib.contextmanager
def _secrets_lock(exclusive: bool = True):
    """A lock around the secrets file, held for a whole read-modify-write.

    Not nestable, and it must not become so: `flock` is per open file
    description, so a second `open()` of the lock file inside this one blocks
    on a lock this same thread holds and never wakes. That is why the two
    `_unlocked` helpers below exist -- `set_secret` takes the lock once and
    does both halves inside it, rather than calling two separately-locked
    functions and leaving a gap between them where another writer reads the
    same old text and overwrites the first one's key.
    """
    lock_path = SECRETS.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield


def _read_secrets_unlocked() -> str:
    return SECRETS.read_text() if SECRETS.exists() else ""


def _write_secrets_unlocked(text: str) -> None:
    SECRETS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SECRETS.with_suffix(".tmp")
    # 0600 before a single credential reaches it, not after. This file can
    # outlive the call if anything goes wrong, and a file holding every secret
    # in the house must never exist world-readable even briefly.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    # Contents, not a rename: this path is a single-file bind mount and the
    # inode has to survive. See `_replace_contents`.
    _replace_contents(tmp, SECRETS)
    SECRETS.chmod(0o600)


def _read_secrets() -> str:
    with _secrets_lock(exclusive=False):
        return _read_secrets_unlocked()


def member_env_suffix(member: str) -> str:
    """`user1` -> `USER_1`. Must agree with deploy.py's copy: the deployer
    expands `{M}` with the same rule, and a disagreement here means the admin
    page generates a key the deployer never exports."""
    m = re.fullmatch(r"([A-Za-z]+)(\d+)", member)
    return f"{m.group(1)}_{m.group(2)}".upper() if m else member.upper()


# The keys a member owns. The proxy token is derived at deploy time and never
# stored, so it is not listed -- there is nothing of it to create or delete.
MEMBER_SECRET_KEYS = [
    ("NANOBOT_API_SECRET_{S}", "generated"),
    ("PAPERLESS_API_TOKEN_{S}", "optional"),
]


def member_keys(member: str) -> list[tuple[str, str]]:
    suffix = member_env_suffix(member)
    return [(tpl.replace("{S}", suffix), kind) for tpl, kind in MEMBER_SECRET_KEYS]


def get_secret(key: str) -> str:
    """Read one value. Only the reveal action uses this, and only over a
    CSRF-checked POST -- a value must never end up in a URL or a log line.

    Falls through SECRET_ALIASES for the same reason `secret_keys_present`
    does, and it has to be the same fallthrough or the two disagree: the page
    would say a renamed credential is set -- because the old name holds it --
    and then reveal an empty box, which reads as the file being corrupt.
    """
    lines = _read_secrets().splitlines()

    def read(name: str) -> str:
        for line in lines:
            if line.startswith(f"{name}="):
                return line.partition("=")[2].strip().strip("'\"")
        return ""

    alias = SECRET_ALIASES.get(key)
    return read(key) or (read(alias) if alias else "")


# A secret that was renamed still answers to the name it had, exactly as
# `SECRET_ALIASES` in deploy/deploy.py does -- and it has to be read the same
# way here, or the deploy succeeds on the old name while this page goes on
# saying the credential is missing. Keep the two lists in step.
SECRET_ALIASES = {
    "HOMECORE_SECRET_KEY": "HOMEWEB_SECRET_KEY",
    "HOMECORE_DEBUG_API_KEY": "HOMEWEB_DEBUG_API_KEY",
    "OPENCODE_API_KEY": "OPENCODE_GO_API_KEY",
}


def secret_keys_present() -> dict[str, bool]:
    """Which keys exist and are non-empty. Never the values."""
    present: dict[str, bool] = {}
    for line in _read_secrets().splitlines():
        match = SECRET_LINE.match(line.strip())
        if match:
            present[match.group(1)] = bool(match.group(2).strip().strip("'\""))
    for new, old in SECRET_ALIASES.items():
        if not present.get(new) and present.get(old):
            present[new] = True
    return present


def delete_secret(key: str) -> None:
    """Drop a key entirely. Used only when a member is removed: their bearer
    secret must stop existing, not merely stop being referenced -- a secret
    that outlives its owner is exactly how the id-reuse takeover worked."""
    with _secrets_lock(exclusive=True):
        lines = [line for line in _read_secrets_unlocked().splitlines(keepends=True)
                 if not line.startswith(f"{key}=")]
        _write_secrets_unlocked("".join(lines))


def set_secret(key: str, value: str) -> None:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
        raise ValueError(f"not a valid key name: {key}")
    if "\n" in value or "\r" in value:
        raise ValueError("a secret cannot contain a newline")

    # Read and write under one lock. Two separately-locked halves leave a gap
    # in which another writer reads the same old text, and the second write
    # wins -- losing whichever key the first one set. `install --generate-secrets`
    # writes a run of them, so the gap is not hypothetical.
    with _secrets_lock(exclusive=True):
        lines = _read_secrets_unlocked().splitlines(keepends=True)
        replaced = False
        out = []
        for line in lines:
            if line.startswith(f"{key}="):
                out.append(f"{key}={value}\n")
                replaced = True
            else:
                out.append(line)
        if not replaced:
            out.append(f"{key}={value}\n")
        _write_secrets_unlocked("".join(out))


# The five directories `paths:` names. Stated once, because the Site page
# renders this list and the form handler reads it: a path that appears in one
# and not the other is editable in the file and invisible on the page, which is
# what `backups:` and `plugins:` were.
PATH_KINDS = ("config", "state", "media", "backups", "plugins")

# Roles a path can belong to. The three under `hosts:` plus `vps`, which is
# deliberately not one of them -- it runs one proxy and its address lives under
# `cloud.vps` -- and which is exactly the role that needed this: moving `state`
# onto a second disk on the hub told the VPS to use that path too, and its
# deploy failed on `mkdir -p /mnt/data/...` for a disk it does not have.
PATH_ROLES = ("hub", "compute", "storage", "vps")


# --------------------------------------------------------------------------
# What a change affects
# --------------------------------------------------------------------------
# Saving writes config; it does not deploy. But a setting that never reaches a
# container is worse than one that was never changed -- the page says saved, the
# house behaves the old way, and nothing points at why. So every save records
# which services still need a deploy, and the Deploy page offers exactly those.
#
# Keyed by a dotted config path prefix. `*` means every service.
IMPACT = {
    "site.name": ["*"],
    "site.domain": ["*"],
    "site.host": ["home-core"],
    "site.timezone": ["*"],
    "site.assistant_name": ["nanobot", "nanobot-house", "home-core"],
    # Every service the manifest interpolates `{locale.default}` into, checked
    # against it rather than remembered: SEARCH_LANGUAGE for the two assistants
    # and WHISPER_LANGUAGE for the voice gateway. home-voice was the one this
    # list kept missing, and its manifest comment names the cost -- whisper
    # force-decodes speech as the old language, which produces confident
    # nonsense rather than an error, in front of a room.
    "locale": ["home-core", "admin", "mqtt", "home-cameras", "nanobot",
               "nanobot-house", "home-voice", "home-paperless"],
    "hosts": ["*"],
    "paths": ["*"],
    # Every service whose env the manifest builds out of a `dns:` name. This
    # was `["home-core", "local-proxy"]`, from when internal URLs were
    # addresses and a name only reached the portal's certificate and the
    # proxy's site blocks. Nine services interpolate one now -- the assistants'
    # TASKS_API_URL and PAPERLESS_URL, the wall's and the gateway's broker, the
    # portal's WHISPER_URL -- so renaming a host on this page marked two of
    # them pending and left the other seven holding the old name, which is the
    # "saved, and the house behaves the old way" failure this table exists for.
    "dns": ["home-core", "local-proxy", "nanobot", "nanobot-house",
            "home-cameras", "home-voice", "mqtt", "nodered", "home-paperless",
            "alfred-mcp"],
    # alfred-mcp because `members[].programmer` renders that person's
    # bridge, and home-core because it is told which members have one.
    # home-paperless because each member's locale is one of the languages its
    # OCR reads scans in (ocr_language() in the deployer).
    "members": ["nanobot", "nanobot-house", "home-core", "alfred-mcp", "home-paperless"],
    # Which model answers as which persona. The Models page saved and marked
    # nothing pending, so a household that changed the everyday model -- or the
    # programmer the coding harness runs as CODE_HARNESS_MODEL -- got "saved"
    # and containers still holding the old one, with nothing pointing at why.
    # `assistant.models` and not `assistant`: `price_check` is read by this
    # page's own thread out of the file it edits, and needs no deploy at all.
    # home-cameras as well: the wall's CLIP_REVIEW_MODEL is
    # `{derived.vision_model}`, so `assistant.models.vision` is its setting
    # too, and a change that redeployed only the assistants left the clip
    # reviewer loading the model the household had just moved off.
    "assistant.models": ["nanobot", "nanobot-house", "home-cameras"],
    # Two roles on this page that the assistant never reads. The portal's
    # titler gets CHAT_TITLE_MODEL from `titles`; Paperless's own AI gets
    # PAPERLESS_AI_MODEL from `documents`. Both arrive through `{derived.*}`
    # exports, so the service holding the old name is the one to redeploy.
    # Background tasks on pi (services/nanobot/nanobot/harness): written into
    # both assistants' config at deploy, so both have to be redeployed.
    "assistant.harness": ["nanobot", "nanobot-house"],
    "assistant.routing": ["nanobot", "nanobot-house"],
    "assistant.models.titles": ["home-core"],
    "assistant.models.documents": ["home-paperless"],
    # The Speech card on the Models page. faster-whisper's own block already
    # marks faster-whisper; audio.cpp's package is ALSO read by the voice
    # gateway as AUDIOCPP_TTS_MODEL, so changing it must redeploy home-voice.
    "services.audio-cpp.tts_model": ["home-voice"],
    # Alfred's voice. Read by the gateway at startup from its environment, so
    # choosing one on this page does nothing until home-voice is deployed --
    # which is the whole reason this table exists.
    "services.home-voice.tts_engine": ["home-voice"],
    "services.home-voice.tts_voice": ["home-voice"],
    # Written into each member's agent front matter and read by opencode only
    # at startup, so the unit that rewrites those files has to run again.
    "cloud.opencode.model": ["alfred-mcp"],
    "cloud.notifications": ["ntfy", "home-core", "nanobot", "nanobot-house", "mqtt"],
    "cloud.certificates": ["home-core"],
    "cloud.vps": ["local-proxy", "cloud-proxy"],
    "cloud.optional_integrations": ["nanobot", "nanobot-house"],
    # Both self-hosted model endpoints. The assistants read them, and so do
    # the portal's titler and Paperless's AI whenever `assistant.models.titles`
    # or `.documents` names one: the deployer resolves those roles to a URL
    # and key and exports them to home-core and home-paperless.
    "cloud.openai_compatible": ["nanobot", "nanobot-house", "home-core", "home-paperless"],
    "cloud.freetoken": ["nanobot", "nanobot-house", "home-core", "home-paperless"],
    # Where the household's own Ollama is, and the window it runs. Every
    # service with a `{derived.ollama*}`, `.title_*`, `.vision_model` or
    # `.paperless_*` export reads it; it had no entry at all until 2026-09-12,
    # so moving the Ollama redeployed nothing.
    "cloud.ollama": ["nanobot", "nanobot-house", "home-core", "home-paperless",
                     "home-cameras", "home-search"],
}

# Which services read a given credential, for the same reason: changing a secret
# does nothing until the containers that hold it are rebuilt.
SECRET_IMPACT = {
    # home-core and home-paperless too: a `titles` or `documents` model on a
    # hosted provider is exported to them with that provider's key
    # (model_endpoint in deploy.py), so a rotation must reach them as well.
    "OPENCODE_API_KEY": ["nanobot", "nanobot-house", "home-core", "home-paperless"],
    "HOMECORE_SECRET_KEY": ["home-core"],
    "HOMECORE_DEBUG_API_KEY": ["home-core"],
    "ADMIN_SECRET_KEY": ["admin"],
    # Not deployed anywhere: the admin page reads it from the file it already
    # edits, so a password change takes effect on the next request rather than
    # on the next deploy. Listed so the secrets page shows it as set.
    "ADMIN_PASSWORD_HASH": [],
    "PROXY_SHARED_SECRET": ["home-core", "local-proxy", "nanobot", "home-cameras"],
    # admin as well: the Voice page plays a sample through the gateway, so a
    # rotated token has to reach this container too or the preview 401s while
    # everything else works.
    "VOICE_GATEWAY_TOKEN": ["home-voice", "nanobot-house", "home-core", "admin"],
    # home-core too: the portal proxies the assistants' profile panel and
    # adds this header on their behalf, so a rotation that redeploys only
    # the assistants leaves it sending the old one and getting 401s.
    "NANOBOT_DEBUG_SECRET": ["nanobot", "nanobot-house", "home-core"],
    "NANOBOT_API_SECRET_HOUSE": ["nanobot-house", "home-voice"],
    "SHARE_SMB_PASSWORD": ["home-core", "nanobot"],
    "PAPERLESS_SECRET_KEY": ["home-paperless"],
    # Only the seed. It is substituted into the deployed settings.yml the first
    # time that file still carries the image's placeholder, and after that the
    # file is state -- so rotating this key does not rotate the instance's, and
    # the secrets page says so rather than implying a redeploy is enough.
    "SEARXNG_SECRET_KEY": ["home-search"],
    # The projects registry lives in the portal; the broker runs beside
    # the assistants and presents the token, so both services read it.
    "PROJECTS_KEY": ["home-core"],
    "PROJECTS_BROKER_TOKEN": ["home-core", "nanobot"],
    "CODE_BROKER_SECRET": ["nanobot"],
    "PAPERLESS_MAIL_PASSWORD": ["home-paperless"],
    "HOMEASSISTANT_TOKEN": ["nanobot", "nanobot-house"],
    "NTFY_CREDENTIALS": ["home-core", "nanobot", "nanobot-house", "mqtt"],
    "ACME_DNS_API_TOKEN": ["home-core"],
    "PROXY_SESSION_SIGNING_KEY": ["local-proxy", "cloud-proxy"],
    "BRIGHTDATA_API_TOKEN": ["nanobot"],
    # The token reaches two places: crawl4ai reads it to decide its bind, and
    # nanobot puts it in the MCP Authorization header. Rotating it without
    # deploying both leaves the assistant holding the old one.
    "CRAWL4AI_API_TOKEN": ["crawl4ai", "nanobot"],
    "CRAWL4AI_SECRET_KEY": ["crawl4ai"],
    "TOGETHER_API_KEY": ["nanobot", "nanobot-house", "home-core", "home-paperless"],
    "OPENAI_COMPATIBLE_API_KEY": ["nanobot", "nanobot-house", "home-core", "home-paperless"],
    "FREETOKEN_API_KEY": ["nanobot", "nanobot-house", "home-core", "home-paperless"],
    # Read by ./home-stack backup on the host, not by any container.
    # Listed so the secrets page shows it and can generate it.
    "BACKUP_ENCRYPTION_KEY": [],
}

# Per-member keys carry a suffix, so they match by prefix.
SECRET_PREFIX_IMPACT = {
    "NANOBOT_API_SECRET_USER_": ["nanobot", "home-core"],
    "PAPERLESS_API_TOKEN_USER_": ["nanobot"],
    "HOMECORE_PROXY_TOKEN_USER_": ["nanobot"],
}


def plugin_impact() -> tuple[dict, dict]:
    """`impact:` and `secret_impact:` rows the configured plugins declare.

    A plugin's settings and credentials have to offer the same "these services
    need a deploy" as everything else here. A setting that never reaches a
    container is worse than one never changed, and that is no less true of a
    service the household brought along than of one this package ships.
    """
    config_rows: dict[str, list] = {}
    secret_rows: dict[str, list] = {}
    for plugin in load_plugins():
        doc = plugin.get("doc") or {}
        for target, rows in ((config_rows, doc.get("impact") or {}),
                             (secret_rows, doc.get("secret_impact") or {})):
            for key, services in rows.items():
                if isinstance(services, str):
                    services = [services]
                target.setdefault(str(key), []).extend(str(x) for x in services)

        # A service that declares a secret is a service that has to be
        # redeployed when it changes -- that much is derivable, and asking a
        # plugin to write it twice is asking for the two to disagree.
        for name, spec in (doc.get("services") or {}).items():
            declared = spec.get("secrets") or {}
            for key in (list(declared.get("required") or [])
                        + list(declared.get("optional") or [])):
                row = secret_rows.setdefault(str(key), [])
                if name not in row:
                    row.append(str(name))
    return config_rows, secret_rows


def plugin_secret_groups() -> list[dict]:
    """The credentials each plugin's services declare, for the secrets page.

    A key nothing lists is a key nobody knows to fill in, and the deploy fails
    on it much later with the service simply not starting.
    """
    groups = []
    for plugin in load_plugins():
        required, optional = [], []
        for spec in ((plugin.get("doc") or {}).get("services") or {}).values():
            declared = spec.get("secrets") or {}
            required += [str(k) for k in (declared.get("required") or [])]
            optional += [str(k) for k in (declared.get("optional") or [])]
        keys = ([(k, "required") for k in dict.fromkeys(required)]
                + [(k, "optional") for k in dict.fromkeys(optional)
                   if k not in required])
        if keys:
            # `secret_keys`, not `keys`, for the reason spelled out where the
            # member rows are built: Jinja resolves attributes before items, so
            # `group.keys` in a template is the dict *method*, and iterating it
            # renders /secrets as a 500.
            groups.append({"name": plugin["name"], "secret_keys": keys})
    return groups


def secret_impact(key: str) -> list[str]:
    plugin_secrets = plugin_impact()[1]
    if key in SECRET_IMPACT or key in plugin_secrets:
        return list(SECRET_IMPACT.get(key, [])) + list(plugin_secrets.get(key, []))
    for prefix, services in SECRET_PREFIX_IMPACT.items():
        if key.startswith(prefix):
            return services
    return []


def _flatten(node, prefix=""):
    """Config as dotted path -> scalar, so two versions can be compared."""
    out = {}
    if isinstance(node, dict):
        for key, value in node.items():
            out.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(node, list):
        out[prefix] = repr([_flatten(v) if isinstance(v, (dict, list)) else v
                            for v in node])
    else:
        out[prefix] = node
    return out


def services_affected(before: dict, after: dict) -> set[str]:
    changed = set()
    flat_before, flat_after = _flatten(before), _flatten(after)
    for path in set(flat_before) | set(flat_after):
        if flat_before.get(path) == flat_after.get(path):
            continue
        # A service's own block affects that service, and anything that lists it
        # as a dependency.
        parts = path.split(".")
        if parts[0] == "services" and len(parts) > 1:
            changed.add(parts[1])
        # No `continue` after that: a `services.<name>.*` key can affect
        # *another* service too -- a setting one service owns and a second one
        # reads through a `{derived.*}` export -- and IMPACT is where that is
        # written down. Returning early here would deploy only the service that
        # owns the block and leave the one that reads it holding the old value.
        # There is no such entry under `services.` today; the loop is what
        # keeps adding one from being a silent half-deploy.
        for prefix, services in {**IMPACT, **plugin_impact()[0]}.items():
            if path == prefix or path.startswith(prefix + "."):
                changed.update(services)
    return changed


# Beside the config, which is the host's config directory: the list survives a
# redeploy of this page. It lived in the container's own /state until
# 2026-09-11, and every recreate forgot what was still waiting to go out.
PENDING_FILE = CONFIG.parent / "pending-deploy.json"


def load_pending() -> list[str]:
    try:
        return list(json.loads(PENDING_FILE.read_text()))
    except (OSError, ValueError):
        return []


def save_pending(names) -> None:
    PENDING_FILE.write_text(json.dumps(sorted(set(names))))


def note_pending(names) -> None:
    """Record services whose running containers no longer match the config.

    A file beside the config, not the session: the session is one browser's
    cookie, signed with a key that can be per-process. Saved on the kitchen
    laptop and read on the office PC -- or across a restart -- a session-held
    list simply was not there, which broke the one promise this feature makes.
    """
    pending = set(load_pending())
    pending.update(n for n in names if n)
    save_pending(pending)


def expand_pending(names, manifest: dict) -> list[str]:
    """`*` means everything the manifest knows about, plus dependents."""
    known = set(manifest.get("services", {})) | set(
        manifest.get("optional_services", {}))
    if "*" in names:
        return sorted(known)
    out = set(n for n in names if n in known)
    for name, spec in manifest.get("services", {}).items():
        if set(spec.get("depends_on", [])) & out:
            out.add(name)
        # `when_service:` is a dependency the other way round: this unit's
        # config is rewritten from another service's enabled state, so
        # toggling that service and deploying only it leaves the assistants
        # holding the wiring for the state it used to be in -- the skills
        # still disabled after switching search on, or still enabled and
        # pointed at containers that are gone after switching it off. Saving
        # is not deploying, and neither is deploying the wrong service.
        for unit in spec.get("units", []):
            if set(unit.get("when_service") or {}) & out:
                out.add(name)
    return sorted(out)


# --------------------------------------------------------------------------
# Phone enrollment codes
# --------------------------------------------------------------------------

def issue_phone_code(cfg: dict, member_id: str) -> tuple[str | None, str]:
    """Mint a one-time enrollment code for the phone app, via the proxy that
    owns the device database.

    Deliberately not an HTTP route on the proxy itself: a code is only useful
    to whoever can also run this command, which keeps enrollment in the hands
    of whoever holds this page. With the VPS on, the code has to come from the
    cloud proxy (that is the one the phone enrolls against from outside);
    otherwise from the local one.
    """
    vps = (cfg.get("cloud") or {}).get("vps") or {}
    if vps.get("enabled") and vps.get("host"):
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                f"{vps.get('user', 'root')}@{vps['host']}",
                "docker", "exec", "home-chat-proxy",
                "python", "manage_devices.py", "code", member_id]
    else:
        argv = ["docker", "exec", "home-chat-proxy-local",
                "python", "manage_devices.py", "code", member_id]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, str(exc)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:200]
        return None, detail or "the proxy container did not answer"
    match = re.search(r"\b(\d{6})\b", result.stdout or "")
    if not match:
        return None, (result.stdout or "").strip()[:200]
    return match.group(1), ""


# The audio.cpp package catalogue, as one `docker exec`. Cached for a minute
# because the page re-renders on every save and the container has to read 68
# spec files to answer.
_AUDIOCPP_CACHE: dict = {"at": 0.0, "rows": [], "error": ""}
_AUDIOCPP_TTL = 60.0

# Runs inside the container: it has the specs and the model manager, and this
# page has neither. Returns one JSON document rather than three calls, because
# each one costs a process start on a container that may be busy synthesising.
_AUDIOCPP_PROBE = r"""
import json, os, glob
os.chdir("/app/model_specs")
fam = {}
for f in glob.glob("*.json"):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    fam[d.get("family")] = {"tasks": d.get("tasks") or [],
                            "languages": d.get("languages") or [],
                            "status": d.get("status") or "",
                            "display": d.get("display_name") or ""}
pkgs = []
for f in glob.glob("*.json"):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    for p in d.get("packages") or []:
        pkgs.append({"id": p.get("id"), "family": d.get("family"),
                     "precision": p.get("precision") or "",
                     "format": p.get("format") or "",
                     "target": p.get("target_directory") or "",
                     # The package's *own* files. Several packages of one
                     # family land in one directory -- supertonic ships q8_0,
                     # f16 and orig into Supertonic-3-GGUF -- so "is there a
                     # .gguf under the target" answers yes for all three when
                     # one is installed, and reports the same size for each.
                     "files": [os.path.basename(f) for f in (p.get("files") or [])]})
root = os.environ.get("AUDIOCPP_MODELS", "/models")
installed = {}
for p in pkgs:
    d = os.path.join(root, p["target"]) if p["target"] else ""
    if not (d and os.path.isdir(d)):
        continue
    # Its own files, by name, wherever under the directory they landed. A
    # package is installed when they are all there; a partial download is not
    # something to offer a Remove button for.
    want = set(p.get("files") or [])
    if not want:
        continue
    found = {}
    for base, _dirs, names in os.walk(d):
        for n in names:
            if n in want:
                try:
                    found[n] = os.path.getsize(os.path.join(base, n))
                except OSError:
                    found[n] = 0
    if set(found) != want:
        continue
    installed[p["id"]] = sum(found.values())
# What the Listen button measured last time, if anybody has pressed it.
try:
    with open(os.path.join(root, ".timings.json")) as fh:
        timings = json.load(fh)
except Exception:
    timings = {}
print(json.dumps({"packages": pkgs, "families": fam,
                  "installed": installed, "timings": timings}))
"""


def audiocpp_catalogue(force: bool = False) -> tuple[list[dict], str]:
    """Every package audio.cpp knows, and whether its files are on disk.

    `installed` is decided by looking for a .gguf under the package's target
    directory rather than by asking `model_manager_v2.py installed`, which
    reaches Hugging Face to compare revisions -- a network call this page must
    not make to draw a table. The cost is that a half-downloaded package reads
    as installed; the manager's own install is what fixes that, and it is
    idempotent.
    """
    now = time.time()
    if not force and now - _AUDIOCPP_CACHE["at"] < _AUDIOCPP_TTL:
        return _AUDIOCPP_CACHE["rows"], _AUDIOCPP_CACHE["error"]
    try:
        out = subprocess.run(
            ["docker", "exec", "audio-cpp", "python3", "-c", _AUDIOCPP_PROBE],
            capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as exc:
        _AUDIOCPP_CACHE.update(at=now, rows=[], error=str(exc)[:200])
        return [], _AUDIOCPP_CACHE["error"]
    if out.returncode != 0:
        detail = (out.stderr or out.stdout or "").strip()[:200]
        _AUDIOCPP_CACHE.update(
            at=now, rows=[],
            error=detail or "the audio-cpp container did not answer")
        return [], _AUDIOCPP_CACHE["error"]
    try:
        data = json.loads(out.stdout)
    except ValueError:
        _AUDIOCPP_CACHE.update(at=now, rows=[],
                               error=(out.stdout or "")[:200])
        return [], _AUDIOCPP_CACHE["error"]
    families = data["families"]
    installed = data.get("installed") or {}
    timings = data.get("timings") or {}
    rows = []
    for p in data["packages"]:
        meta = families.get(p["family"]) or {}
        tasks = meta.get("tasks") or []
        # Only the two this stack has any use for. The zoo also does music,
        # separation, super-resolution and diarisation, and a page offering
        # those would be offering settings the voice path cannot read.
        if not ({"tts", "asr"} & set(tasks)):
            continue
        langs = [str(x) for x in (meta.get("languages") or [])]
        rows.append({
            "id": p["id"], "family": p["family"],
            "task": "tts" if "tts" in tasks else "asr",
            "precision": p["precision"], "format": p["format"],
            "status": meta.get("status", ""),
            "languages": langs,
            # What a Spanish-speaking household cares about first. "multi" and
            # "600+ languages" count: they are claims about coverage, and the
            # profile is what turns a claim into a number.
            "spanish": any(l.lower().startswith("es") or "multi" in l.lower()
                           or "+" in l for l in langs),
            "installed": p["id"] in installed,
            "bytes": installed.get(p["id"], 0),
            # The last time somebody pressed Listen on this package. Absent is
            # the ordinary state; the column says so rather than showing a
            # zero, which would read as a model that answers instantly.
            "timing": timings.get(p["id"]) or None,
        })
    rows.sort(key=lambda r: (r["task"], not r["spanish"], r["family"], r["id"]))
    _AUDIOCPP_CACHE.update(at=now, rows=rows, error="")
    return rows, ""


# What a preview says. A butler's line rather than "testing 1 2 3": what is
# being judged is whether this voice suits the thing it will actually be doing,
# and a sentence with the household's own accents in it is the test.
PREVIEW_LINE = "Buenas tardes. La cena está servida y ya encendí las luces."

_STYLE_CACHE: dict = {}


def _spec_voice_ids(package: str) -> list[str]:
    """Voices a package's spec declares as a `voice_id` enum, if any.

    A file read inside the container rather than a GGUF unpack, so it is the
    cheap half of the question and is asked first.
    """
    script = (
        "import json, glob, sys\n"
        "for f in glob.glob('/app/model_specs/*.json'):\n"
        "    d = json.load(open(f))\n"
        "    if not any(p.get('id') == sys.argv[1] for p in d.get('packages') or []):\n"
        "        continue\n"
        "    for o in (d.get('options') or {}).get('request') or []:\n"
        "        if o.get('name') == 'voice_id' and o.get('values'):\n"
        "            print('\\n'.join(str(v) for v in o['values']))\n"
    )
    try:
        out = subprocess.run(["docker", "exec", "audio-cpp", "python3", "-c",
                              script, package],
                             capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return []
    return [l.strip() for l in (out.stdout or "").splitlines() if l.strip()]


def audiocpp_voice_styles(package: str) -> list[str]:
    """The voices you can pick from inside one package, however it offers them.

    **Two mechanisms, and reading only one under-reported the zoo.** Some
    packages carry `voice_style_*` configs inside the GGUF -- supertonic ships
    M1-M5 and F1-F5 that way. Others declare a `voice_id` enum in their spec:
    magpie has Aria, Jason, John, Leo and Sofia, neutts nine, personaplex
    sixteen. Checking only the first said "no voices" about models that have
    five.

    The spec is checked first because it is a file read rather than a GGUF
    unpack.

    What neither mechanism covers is the larger answer: most packages here
    list `clone` as a task, which is unlimited voices from a reference clip
    rather than a list to choose from, and some list `design`, which is a
    voice described in words. Absent from this list does not mean one voice --
    it means no menu.

    A style is not a package. `audio.cpp` takes both -- `model` for the file
    and `voice` for the style within it -- and they were conflated once
    already, in the gateway, which made every style unreachable. The mistake
    survives review easily because the server answers 200 to a voice it does
    not know and returns the default.

    Cached for the life of the process: a package's styles are baked into its
    GGUF and cannot change without a different file. `--inspect` unpacks the
    model to a temporary directory, so it is not something to run per render.
    """
    if not re.fullmatch(r"[a-z0-9_]+", package or ""):
        return []
    if package in _STYLE_CACHE:
        return _STYLE_CACHE[package]
    declared = _spec_voice_ids(package)
    if declared:
        _STYLE_CACHE[package] = declared
        return declared
    script = (
        'import glob, os, subprocess, sys\n'
        'root = os.environ.get("AUDIOCPP_MODELS", "/models")\n'
        'import json\n'
        'spec = None\n'
        'for f in glob.glob("/app/model_specs/*.json"):\n'
        '    d = json.load(open(f))\n'
        '    for p in d.get("packages") or []:\n'
        '        if p.get("id") == %r:\n'
        '            spec = (d.get("family"), p.get("target_directory"))\n'
        'if not spec:\n'
        '    sys.exit(0)\n'
        'fam, target = spec\n'
        'g = glob.glob(os.path.join(root, target, "**", "*.gguf"), recursive=True)\n'
        'if not g:\n'
        '    sys.exit(0)\n'
        'out = subprocess.run(["audiocpp_cli", "--task", "tts", "--family", fam,\n'
        '                      "--model", g[0], "--inspect"],\n'
        '                     capture_output=True, text=True, timeout=120)\n'
        'for line in (out.stdout or "").splitlines():\n'
        '    if line.startswith("config=voice_style_"):\n'
        '        print(line.split("=", 1)[1].split(":", 1)[0][len("voice_style_"):])\n'
    ) % package
    try:
        out = subprocess.run(["docker", "exec", "audio-cpp", "python3", "-c", script],
                             capture_output=True, text=True, timeout=180)
    except (subprocess.TimeoutExpired, OSError):
        return []
    styles = sorted({l.strip() for l in (out.stdout or "").splitlines() if l.strip()})
    _STYLE_CACHE[package] = styles
    return styles


def audiocpp_backend(cfg: dict) -> str:
    """What the running container is actually using, or "" if it will not say."""
    try:
        with urllib.request.urlopen(f"{audiocpp_base(cfg)}/health", timeout=8) as r:
            return str(json.loads(r.read().decode()).get("backend") or "")
    except Exception:                                             # noqa: BLE001
        return ""


def audiocpp_base(cfg: dict) -> str:
    """Where audio.cpp answers, as this container can reach it."""
    svc = (cfg.get("services") or {}).get("audio-cpp") or {}
    role = str(svc.get("host") or "compute")
    address = str((((cfg.get("hosts") or {}).get(role)) or {})
                  .get("address") or "127.0.0.1")
    return f"http://{address}:{svc.get('port', 21014)}"


def audiocpp_loaded(package: str, url: str) -> bool | None:
    """Whether the server already has this model in memory.

    The difference between a cold and a warm number is the model load, and
    `lazy_load` means the first call after a start pays it. Asking rather than
    assuming is what makes it honest to *call* a figure cold: press Listen
    twice and the second run's first call is warm too, so a run that reported
    both as cold would be quietly wrong the second time.

    None when the question could not be asked, which is treated as "not cold"
    -- claiming a cold number on a guess is the failure worth avoiding.
    """
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/v1/models", timeout=10) as r:
            for m in (json.loads(r.read().decode()).get("data") or []):
                if m.get("id") == package:
                    return bool(m.get("loaded"))
    except Exception:                                             # noqa: BLE001
        return None
    return None


def remember_disk(package: str, mib: int) -> None:
    """Record how much a package held, before its files go.

    The size column reads the files, so it goes blank the moment they do --
    and "how big was that thing I just deleted" is exactly the question
    somebody asks afterwards. Kept with the timings, which already outlive the
    model for the same reason: a measurement is a fact about a package, not
    about a copy of it.
    """
    _write_timing_fields(package, {"disk_mib": int(mib)})


def _write_timing_fields(package: str, fields: dict) -> None:
    """Merge fields into one package's stored row. Best effort."""
    if not re.fullmatch(r"[a-z0-9_]+", package or "") or not fields:
        return
    script = (
        "import json, os, sys\n"
        "root = os.environ.get('AUDIOCPP_MODELS', '/models')\n"
        "path = os.path.join(root, '.timings.json')\n"
        "try:\n"
        "    all_of_them = json.load(open(path))\n"
        "except Exception:\n"
        "    all_of_them = {}\n"
        "row = all_of_them.get(sys.argv[1]) or {}\n"
        "row.update(json.loads(sys.argv[2]))\n"
        "all_of_them[sys.argv[1]] = row\n"
        "tmp = path + '.tmp'\n"
        "json.dump(all_of_them, open(tmp, 'w'))\n"
        "os.replace(tmp, path)\n"
    )
    try:
        subprocess.run(["docker", "exec", "audio-cpp", "python3", "-c", script,
                        package, json.dumps(fields)],
                       capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return
    audiocpp_catalogue(force=True)


def remember_timing(package: str, voice: str, warm: dict,
                    cold_s: float | None, backend: str = "") -> None:
    """Record what the Listen button measured, beside the models.

    The tester is the profiler: somebody comparing voices is already making
    the measurement, and asking them to press a second button to keep it is
    asking twice for one answer.

    Two numbers, because they answer different questions. **Cold** is what the
    first person to speak after a restart waits -- the model load, which for
    supertonic is most of seven seconds. **Warm** is what everybody else
    waits, and it is the one a voice should be judged on. Reporting only the
    first would libel every model that loads slowly and speaks quickly; only
    the second would promise a speed the first caller never sees.

    `cold_s` is None when the model was already in memory, and then the stored
    cold figure is left alone rather than overwritten with a warm one wearing
    its name.

    Written into `/models/.timings.json` because it describes a file and
    belongs with it -- remove the package and the numbers go too.

    Best effort. A preview that played and then failed to record is a working
    preview, and must not surface as an error.
    """
    if not re.fullmatch(r"[a-z0-9_]+", package or ""):
        return
    try:
        row = {"synth_s": round(float(warm["synth"]), 2),
               "audio_s": round(float(warm["audio"]), 2), "voice": voice,
               # Which backend produced it. A CPU number and a CUDA number for
               # one package differ by twenty times, and a column that showed
               # them side by side without saying which was which would be
               # worse than an empty one.
               "backend": backend, "source": "listen",
               "at": int(time.time())}
    except (TypeError, ValueError, KeyError):
        return
    if cold_s is not None:
        row["cold_s"] = round(float(cold_s), 2)
    row["rtf"] = (round(row["synth_s"] / row["audio_s"], 2)
                  if row["audio_s"] else None)
    script = (
        "import json, os, sys\n"
        "root = os.environ.get('AUDIOCPP_MODELS', '/models')\n"
        "path = os.path.join(root, '.timings.json')\n"
        "try:\n"
        "    all_of_them = json.load(open(path))\n"
        "except Exception:\n"
        "    all_of_them = {}\n"
        "row = json.loads(sys.argv[2])\n"
        "# A run that found the model already loaded has no cold number of\n"
        "# its own. Keep the last real one rather than dropping it: it is\n"
        "# still true of this file until the file changes.\n"
        "if 'cold_s' not in row:\n"
        "    old = (all_of_them.get(sys.argv[1]) or {}).get('cold_s')\n"
        "    if old is not None:\n"
        "        row['cold_s'] = old\n"
        "all_of_them[sys.argv[1]] = row\n"
        "tmp = path + '.tmp'\n"
        "json.dump(all_of_them, open(tmp, 'w'))\n"
        "os.replace(tmp, path)\n"
    )
    try:
        subprocess.run(["docker", "exec", "audio-cpp", "python3", "-c", script,
                        package, json.dumps(row)],
                       capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return
    audiocpp_catalogue(force=True)


# Two calls: the first loads the model and the second is the number worth
# keeping. Measuring the first would publish a figure nobody sees twice.
_PROFILE_SCRIPT = r"""
import io, json, os, sys, time, urllib.request, wave
pkg, line = sys.argv[1], sys.argv[2]
port = os.environ.get("AUDIOCPP_PORT", "8080")


# What a package needs in the request, tried in order. Most of these models
# clone rather than offering a voice: PocketTTS answers a plain request with
# "requires a session voice", Qwen3 Base with "requires voice clone reference
# audio". Profiling only the plain shape measured the six that speak and
# reported a 500 for the ten that clone.
REF = "/voices/reference-es.wav"
REF_TEXT = ("Hola, soy la voz de la casa. Esto es una muestra de referencia "
            "en espanol para clonar.")
SHAPES = [{}, {"voice_ref": REF, "reference_text": REF_TEXT},
          {"voice_id": "Sofia", "language": "es"}]
SHAPE = {"i": 0}


def say():
    last = None
    for i in range(SHAPE["i"], len(SHAPES)):
        body = dict(SHAPES[i])
        body.update({"model": pkg, "input": line, "response_format": "wav"})
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/audio/speech",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                # Remembered, so the second call does not re-walk the ladder
                # and time a shape the first one already ruled out.
                SHAPE["i"] = i
                return time.time() - t0, r.read()
        except Exception as exc:                                  # noqa: BLE001
            last = exc
    raise last


loaded = None
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models",
                                timeout=15) as r:
        for m in json.loads(r.read().decode()).get("data") or []:
            if m.get("id") == pkg:
                loaded = bool(m.get("loaded"))
except Exception:
    pass

try:
    first, _ = say()
    warm, blob = say()
except Exception as exc:
    print(json.dumps({"error": str(exc)[:200]}))
    sys.exit(0)

try:
    with wave.open(io.BytesIO(blob)) as w:
        audio = w.getnframes() / float(w.getframerate() or 1)
except Exception:
    audio = 0.0

row = {"synth_s": round(warm, 2), "audio_s": round(audio, 2),
       "rtf": round(warm / audio, 2) if audio else None,
       "source": "profile", "at": int(time.time())}
# Only call it cold when the model was not already in memory. Profiling twice
# in a row would otherwise record the second run's warm first call as a load.
if loaded is False:
    row["cold_s"] = round(first, 2)
print(json.dumps(row))
"""


def audiocpp_profile(package: str, cfg: dict) -> tuple[dict, str]:
    """Measure one package: warm, cold if it was cold, and what it cost the card.

    Against the container directly rather than through the voice gateway,
    because the question is what *this model* costs -- and the gateway only
    ever asks for the one package it is configured with, so profiling through
    it could measure that one and nothing else.

    VRAM is sampled here rather than inside, because `nvidia-smi` reports the
    whole card and the container cannot see it. Only meaningful on the CUDA
    backend; on the CPU it is left out rather than recorded as zero, which
    would read as "free" instead of "not applicable".
    """
    if not re.fullmatch(r"[a-z0-9_]+", package or ""):
        return {}, "not a package id"
    # The *running* backend, from the container's own /health -- not the
    # config, which is what the household has chosen and may be a deploy
    # ahead. Labelling a measurement from the config stamped a CPU number
    # `cuda` the first time this ran, which is worse than no label: the whole
    # point of the column is telling a 0.12 s from a 2.5 s apart.
    backend = audiocpp_backend(cfg) or "cpu"
    watch = _VramWatch() if backend == "cuda" else None
    if watch:
        watch.start()
    try:
        out = subprocess.run(
            ["docker", "exec", "audio-cpp", "python3", "-c", _PROFILE_SCRIPT,
             package, PREVIEW_LINE],
            capture_output=True, text=True, timeout=1800)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {}, str(exc)[:200]
    finally:
        peak = watch.stop() if watch else None
    if out.returncode != 0:
        return {}, (out.stderr or out.stdout or "").strip()[:200] or "profile failed"
    try:
        row = json.loads((out.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}, (out.stdout or "")[:200] or "no answer"
    if row.get("error"):
        return {}, str(row["error"])[:200]
    row["backend"] = backend
    if peak:
        row["vram_mib"] = peak
    row.pop("failed", None)
    _write_timing_fields(package, row)
    return row, ""


class _VramWatch:
    """Peak card use while one profile runs, minus what was there before."""

    def __init__(self):
        self.base = None
        self.peak = None
        self._stop = threading.Event()
        self._t = None

    @staticmethod
    def _used():
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10)
            return int(out.stdout.strip().splitlines()[0])
        except Exception:                                         # noqa: BLE001
            return None

    def start(self):
        self.base = self.peak = self._used()
        if self.base is None:
            return
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop.wait(0.3):
            now = self._used()
            if now is not None and now > self.peak:
                self.peak = now

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(timeout=5)
        if self.base is None:
            return None
        return max(0, self.peak - self.base) or None


def audiocpp_uninstall(package: str) -> str:
    """Delete one package's files. Returns "" or why it could not.

    The model manager's own `uninstall`, not `rm -rf`: a package is several
    files under a directory that other packages of the same family share, and
    the tool knows which are its own. `Qwen3-TTS-12Hz-0.6B-Base-GGUF` holding
    two precisions is the ordinary case.
    """
    if not re.fullmatch(r"[a-z0-9_]+", package or ""):
        # It goes into an argv, not a shell, so this is not injection -- it is
        # a guard against a typo deleting something by prefix.
        return "not a package id"
    try:
        out = subprocess.run(
            ["docker", "exec", "audio-cpp", "python3",
             "/app/tools/model_manager_v2.py", "uninstall", package,
             "--models-root", "/models"],
            capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return str(exc)[:200]
    if out.returncode != 0:
        return (out.stderr or out.stdout or "").strip()[:200] or "uninstall failed"
    return ""


def tts_choices(cfg: dict) -> list[dict]:
    """Engines and voices the gateway could actually use, for the picker.

    Read from the same two places the gateway reads rather than asked over
    HTTP: the piper voices directory, and `services.audio-cpp.tts_packages`.
    That keeps this page working when the gateway is down -- which is exactly
    when somebody might come here to change the voice -- and it needs no
    gateway credential in this container.

    The cost is that it describes what is *installed*, not what is answering.
    A voice offered here that the gateway cannot load is still possible; the
    gateway's own `/v1/tts/voices` is the live answer and this is the settable
    one. They read the same files, so they disagree only while a deploy is
    mid-flight.
    """
    voices_dir = os.path.join(
        str((cfg.get("paths") or {}).get("config") or "/var/lib/home-stack/config"),
        "home-voice", "voices")
    piper = []
    try:
        for name in sorted(os.listdir(voices_dir)):
            # Both halves or it is not a voice -- the gateway skips an .onnx
            # with no .onnx.json beside it and logs a warning nobody reads, so
            # offering it here would be offering a choice that silently is not
            # one.
            if name.endswith(".onnx") and os.path.isfile(
                    os.path.join(voices_dir, name + ".json")):
                piper.append(name[: -len(".onnx")])
    except OSError:
        pass
    engines = [{
        "id": "piper",
        "label": "Piper",
        # Always: it is baked into the gateway's image, so it cannot be absent
        # the way a separate service can.
        "available": True,
        "voices": [{"id": "", "label": "default"}]
                  + [{"id": v, "label": v} for v in piper],
    }]
    audio = (cfg.get("services") or {}).get("audio-cpp") or {}
    packages = [p.strip() for p in
                str(audio.get("tts_packages") or "").split(",") if p.strip()]
    # Which package it speaks with, and the styles inside *that* one. The
    # package is a file and the style is a voice within it -- audio.cpp takes
    # both and they are not interchangeable.
    model = str(audio.get("tts_model") or "").strip() or (packages[0] if packages else "")
    styles = audiocpp_voice_styles(model) if model else []
    engines.append({
        "id": "audiocpp",
        "label": "audio.cpp",
        # Offering it while the service is off would be offering a choice that
        # fails when taken: the deployer exports no URL for a disabled service,
        # and the gateway then refuses the engine outright.
        "available": bool(audio.get("enabled", True)) and bool(model),
        "model": model,
        "packages": packages,
        # Empty means the package has one voice and no way to choose -- which
        # is most of them. `default` is then the only honest entry.
        "voices": [{"id": "", "label": "default"}]
                  + [{"id": v, "label": v} for v in styles],
    })
    return engines


def finance_token(member_id: str) -> tuple[str | None, str]:
    """This member's finance-helper token: sha256("<secret>:<login id>").

    **Derived, not issued** -- which is the one way this differs from the
    Paperless button beside it. Paperless mints a token, so it has to be
    stored; nothing stores this one. `finance_helper` recomputes the same
    hash from `PROXY_SHARED_SECRET` on every request, exactly as the portal's
    own `_proxy_user_token()` does, so writing it into the secrets file would
    be a second copy of a value that is already implied by a secret the house
    has. It is shown, once, for pasting.

    **The login id, not the member id.** Measured against the live portal on
    2026-08-30 and written down in `derive_proxy_token`: member id and its
    token, 401; login id and a token derived from the login, 200. Both halves
    have to be the login. `user1` is what the deployer builds with; `session
    ['user']` is what a request carries, and this is a credential for a
    request.

    The alternative it replaces is `generate-homeweb-tokens.sh` on somebody's
    laptop, which hard-codes four members, is missing the fifth, and points at
    a host that no longer answers.
    """
    account = login_of(member_id) or {}
    login = str(account.get("username") or "").strip()
    if not login:
        return None, "no-login"
    secret = get_secret("PROXY_SHARED_SECRET")
    if not secret:
        return None, "no-secret"
    if ":" in secret:
        # The separator. A secret containing one makes the hash ambiguous --
        # `a:b` with login `c` and `a` with login `b:c` derive the same token.
        return None, "bad-secret"
    return hashlib.sha256(f"{secret}:{login}".encode()).hexdigest(), ""


def issue_paperless_token(cfg: dict, member_id: str) -> tuple[str | None, str]:
    """Create (or fetch) the member's Paperless API token, via the container.

    Paperless issues DRF tokens per Django user, so this creates the user on
    first use and returns the existing token on every later call -- pressing
    the button twice does not rotate anything. The token lands straight in the
    secrets file under the member's key; the assistant picks it up on the next
    nanobot deploy. Before this, the flow was: open the Paperless UI, create a
    user, create a token, paste it into the env file with exactly the right
    suffix -- four chances to get one string wrong.
    """
    script = (
        "from django.contrib.auth.models import User; "
        "from rest_framework.authtoken.models import Token; "
        f"u, _ = User.objects.get_or_create(username={member_id!r}); "
        "print(Token.objects.get_or_create(user=u)[0].key)"
    )
    storage = (cfg.get("hosts") or {}).get("storage") or {}
    address = storage.get("address", "127.0.0.1")
    docker_argv = ["docker", "exec", "paperless",
                   "python3", "manage.py", "shell", "-c", script]
    if address in ("127.0.0.1", "localhost", "::1"):
        argv = docker_argv
    else:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                f"{storage.get('user', 'root')}@{address}", *docker_argv]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, str(exc)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:200]
        return None, detail or "the paperless container did not answer"
    token = (result.stdout or "").strip().splitlines()[-1].strip() if result.stdout else ""
    if not re.fullmatch(r"[0-9a-f]{40}", token):
        return None, (result.stdout or "").strip()[:200]
    return token, ""


# --------------------------------------------------------------------------
# Locale
# --------------------------------------------------------------------------

# How a date is written where the reader lives, and the one place that is
# decided. `<input type="date">` renders in the *browser's* locale, not the
# page's -- so a household reading Spanish on an English-profile browser was
# shown 03/02/2000 for a birthday everyone here writes 02/03/2000. The stored
# value never changes: it is ISO, because it is read by the assistant, sorted,
# and compared.
#
# The pattern comes from the catalogue (`common.date_format`), so a locale says
# how its own dates look rather than this file guessing from a language code.
_DATE_PARTS = {"DD": "%d", "MM": "%m", "YYYY": "%Y"}


def _date_strf(pattern: str) -> str | None:
    """`DD/MM/YYYY` -> `%d/%m/%Y`, or None for a pattern this does not know."""
    out, rest = "", pattern
    while rest:
        for token, code in _DATE_PARTS.items():
            if rest.startswith(token):
                out, rest = out + code, rest[len(token):]
                break
        else:
            if rest[0] not in "/-. ":
                return None
            out, rest = out + rest[0], rest[1:]
    return out if all(c in out for c in ("%d", "%m", "%Y")) else None


def date_pattern(cfg: dict) -> str:
    return translator("common.date_format", locale=current_locale(cfg))


def format_date(iso: str, pattern: str) -> str:
    """ISO in, what this locale writes out. Anything unparseable comes back
    untouched: showing what is stored beats showing a blank box over it."""
    strf = _date_strf(pattern)
    if not iso or not strf:
        return iso or ""
    try:
        return datetime.datetime.strptime(iso.strip(), "%Y-%m-%d").strftime(strf)
    except ValueError:
        return iso


def parse_date(text: str, pattern: str) -> tuple[str, bool]:
    """(ISO, understood). Empty is understood and means "not set".

    Strictly this locale's order, plus ISO which is unambiguous. Deliberately
    not lenient: 02/03/2000 is two different birthdays and a parser that
    guesses is one that silently moves somebody's by nine months.
    """
    text = (text or "").strip()
    if not text:
        return "", True
    for strf in (_date_strf(pattern), "%Y-%m-%d"):
        if not strf:
            continue
        try:
            return datetime.datetime.strptime(text, strf).strftime("%Y-%m-%d"), True
        except ValueError:
            continue
    return "", False


# --- who somebody is to somebody else -----------------------------------------
#
# A row on this page says **what the other person is to the one whose page you
# are on**: on Ana's page, the box beside Bo holds what Bo is to Ana. That is
# the direction `build_member_profile` writes out ("## Family | Bo | daughter"
# under Ana's own profile) and the direction an imported `- **Family**: Ana
# (mum)` line means. It is stated here because the help text on the page once
# said the opposite, and a household filled four boxes the wrong way round --
# a father whose own page recorded each of his children as his `padre`.
#
# Every relationship therefore has a twin on another page, and the household
# types both. The half nobody goes back to is what makes an assistant say
# "your sister Bo" to Bo herself, so the other side is offered rather than
# demanded: a suggestion fills the box when somebody clicks it, and a box with
# something in it is never touched.
#
# The words themselves live in `i18n/` under `relation.*`, which does two jobs
# at once -- they are what a suggestion is rendered in, and every locale's
# vocabulary is what a typed answer is matched against. So a household typing
# `hija` and one typing `daughter` are both understood, and each is offered its
# own language back.
RECIPROCALS = {
    "father": ["son", "daughter"], "mother": ["son", "daughter"],
    "son": ["father", "mother"], "daughter": ["father", "mother"],
    "brother": ["brother", "sister"], "sister": ["brother", "sister"],
    "husband": ["wife"], "wife": ["husband"], "partner": ["partner"],
    "grandfather": ["grandson", "granddaughter"],
    "grandmother": ["grandson", "granddaughter"],
    "grandson": ["grandfather", "grandmother"],
    "granddaughter": ["grandfather", "grandmother"],
    "uncle": ["nephew", "niece"], "aunt": ["nephew", "niece"],
    "nephew": ["uncle", "aunt"], "niece": ["uncle", "aunt"],
    "cousin_m": ["cousin_m", "cousin_f"],
    "cousin_f": ["cousin_m", "cousin_f"],
}

# Which of the two a term says somebody is. Used only to narrow a suggestion
# that would otherwise be offered both ways -- never stored, never shown, and
# never guessed from a name.
RELATION_GENDER = {
    "father": "m", "son": "m", "brother": "m", "husband": "m",
    "grandfather": "m", "grandson": "m", "uncle": "m", "nephew": "m",
    "cousin_m": "m",
    "mother": "f", "daughter": "f", "sister": "f", "wife": "f",
    "grandmother": "f", "granddaughter": "f", "aunt": "f", "niece": "f",
    "cousin_f": "f",
}

CHILD_RELATIONS = ("son", "daughter")

# What a household actually types, beyond the catalogue's own word for each.
# Both languages and the informal forms, per the house rule for tables that
# match what a person wrote: a household that has been typing `papá` for years
# must not have to stop.
RELATION_ALIASES = {
    "dad": "father", "daddy": "father", "papa": "father", "papá": "father",
    "papi": "father", "pa": "father", "viejo": "father",
    # `mum` resolved to `"mummy"` -- another alias, not a relation key. Nothing
    # resolves transitively, so `relation_key("mum")` returned a value that is
    # in neither RECIPROCALS nor RELATION_GENDER: a pair recorded as
    # daughter/mum showed a permanent "these two disagree" warning while being
    # perfectly consistent, and no reciprocal was ever offered from it.
    "mom": "mother", "mum": "mother",
    "mummy": "mother", "mama": "mother", "mamá": "mother", "mami": "mother",
    "ma": "mother", "vieja": "mother",
    "marido": "husband", "mujer": "wife", "esposa": "wife", "esposo": "husband",
    "hermanito": "brother", "hermanita": "sister", "ñaño": "brother",
    "ñaña": "sister", "sis": "sister", "bro": "brother",
    "abuelito": "grandfather", "abuelita": "grandmother",
    "tio": "uncle", "tia": "aunt",
}


def _relation_words() -> dict:
    """`{typed text: relation key}`, over every locale the package ships.

    Built from the catalogue rather than written twice, so adding a language
    teaches this table that language's words for free -- and so a suggestion
    and the thing it is matched against can never drift apart.
    """
    words = {}
    for key in RECIPROCALS:
        for locale in translator.available:
            word = translator(f"relation.{key}", locale=locale)
            if word and not word.startswith("relation."):
                words.setdefault(word.strip().casefold(), key)
    words.update({k: v for k, v in RELATION_ALIASES.items()})
    return words


def relation_key(typed: str) -> str | None:
    """What somebody wrote -> a relation, or None for free text this does not
    recognise. Free text is perfectly allowed in the box; it just cannot have
    its other half worked out."""
    return _relation_words().get(str(typed or "").strip().casefold())


def relation_word(key: str, locale: str) -> str:
    return translator(f"relation.{key}", locale=locale)


def _known_gender(cfg: dict, member_id: str) -> str | None:
    """`f`, `m` or None, from what other people call this person.

    Not stored anywhere and not asked for. If somebody has been called
    `daughter` by one person and `sister` by another, the household has already
    said which of father/mother to suggest on a third page -- and offering one
    right answer beats offering two. Contradictory input is not something to
    resolve; it is a reason to offer both.
    """
    seen = set()
    for other in (cfg.get("members") or []):
        key = relation_key((other.get("relationships") or {}).get(member_id))
        if key and RELATION_GENDER.get(key):
            seen.add(RELATION_GENDER[key])
    return seen.pop() if len(seen) == 1 else None


def _narrow(cfg: dict, about: str, keys: list) -> list:
    """Drop the options that contradict what the household has already said
    about which of the two this person is. Both stay when it has not said."""
    gender = _known_gender(cfg, about)
    if not gender or len(keys) < 2:
        return keys
    return [k for k in keys if RELATION_GENDER.get(k) == gender] or keys


def suggest_relationships(cfg: dict, member_id: str, locale: str) -> dict:
    """`{other_id: [word, ...]}` for the boxes on this member's page that are
    empty and whose other half is already answered somewhere."""
    people = {m["id"]: m for m in (cfg.get("members") or [])}
    person = people.get(member_id) or {}
    mine = {k: str(v or "").strip()
            for k, v in (person.get("relationships") or {}).items()}
    keys: dict = {}

    for other_id, other in people.items():
        if other_id == member_id or mine.get(other_id):
            continue
        # What this member is to the other one, said on the other one's page.
        theirs = relation_key((other.get("relationships") or {}).get(member_id))
        if theirs and RECIPROCALS.get(theirs):
            keys[other_id] = _narrow(cfg, other_id, RECIPROCALS[theirs])

    # Two people the same person calls their child are each other's siblings.
    # Not a reciprocal -- nobody has said anything about this pair -- but it is
    # the inference that fills the most boxes in an ordinary family, and the
    # one a household is least likely to type twice: the parents' pages get
    # filled in first and the children's are what nobody goes back to.
    for parent in people.values():
        kids = [k for k, v in (parent.get("relationships") or {}).items()
                if relation_key(v) in CHILD_RELATIONS and k in people]
        if member_id not in kids:
            continue
        for sibling in kids:
            if sibling == member_id or mine.get(sibling) or sibling in keys:
                continue
            keys[sibling] = _narrow(cfg, sibling, ["brother", "sister"])

    # Rendered in the language of the page it is offered on, not the language
    # the other half happened to be typed in. An imported profile is in
    # whatever the old machine wrote; the household reading this one should not
    # have to meet it there.
    return {other: [relation_word(k, locale) for k in ks]
            for other, ks in keys.items() if ks}


def relationship_conflicts(cfg: dict, member_id: str, locale: str) -> dict:
    """`{other_id: what the other page implies}` where the two halves disagree.

    Not a suggestion -- both boxes have answers -- and not something to fix
    from here. Said out loud because it is invisible otherwise: each page reads
    fine on its own, and the assistants are handed both.
    """
    people = {m["id"]: m for m in (cfg.get("members") or [])}
    person = people.get(member_id) or {}
    out = {}
    for other_id, other in people.items():
        if other_id == member_id:
            continue
        ours = relation_key((person.get("relationships") or {}).get(other_id))
        theirs = relation_key((other.get("relationships") or {}).get(member_id))
        if not ours or not theirs:
            continue
        expected = _narrow(cfg, other_id, RECIPROCALS.get(theirs, []))
        if expected and ours not in expected:
            out[other_id] = [relation_word(k, locale) for k in expected]
    return out


def current_locale(cfg: dict) -> str:
    available = set(translator.available)
    chosen = request.args.get("lang") or session.get("lang")
    if chosen in available:
        session["lang"] = chosen
        return chosen
    default = (cfg.get("locale") or {}).get("default", "en")
    return default if default in available else "en"


# Common time zones, grouped by region. The full IANA database is ~600 entries,
# most of them historical aliases nobody lives in; this is the set a household
# actually picks from, and the free-text field it replaces was a typo away from
# a container that logs in the wrong century.
def timezone_choices():
    try:
        import zoneinfo
        zones = sorted(zoneinfo.available_timezones())
    except Exception:
        return {"UTC": ["UTC"]}
    keep = ("Africa/", "America/", "Asia/", "Atlantic/", "Australia/",
            "Europe/", "Indian/", "Pacific/")
    grouped: dict[str, list[str]] = {}
    for zone in zones:
        if zone.startswith(keep) and zone.count("/") <= 2:
            grouped.setdefault(zone.split("/")[0], []).append(zone)
    grouped["UTC"] = ["UTC", "Etc/UTC"]
    return dict(sorted(grouped.items()))


@app.context_processor
def inject():
    cfg = load_config()
    locale = current_locale(cfg)
    manifest = load_manifest()
    return {
        "pending_count": len(expand_pending(load_pending(), manifest)),
        "t": lambda key, **params: translator(key, locale=locale, **params),
        "locale": locale,
        "locales": [
            {"code": code, "name": translator.name_of(code)}
            for code in translator.available
            if code in ((cfg.get("locale") or {}).get("available")
                        or translator.available)
        ],
        "site_name": (cfg.get("site") or {}).get("name", "Home"),
    }


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

@app.get("/favicon.ico")
def favicon():
    """Answer the browser's automatic request without touching the session.

    Unrouted, it fell through to the login guard: a redirect, a rendered login
    page, and a session cookie of its own -- for a request the person never
    made. Nothing that is not a page a person asked for should be able to
    change what is in their session.
    """
    return ("", 204)


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


# The credentials a stack cannot run without. `OPENCODE_API_KEY` is the
# only one on this list and the only one that leaves the network -- everything
# else is generated by the installer. Kept beside the same list on the secrets
# page rather than derived, because the manifest's `required:` is per service
# and says what a *container* needs, which is a different question from what a
# person still has to go and find.
REQUIRED_SECRETS = ("OPENCODE_API_KEY",)


def anything_deployed(cfg: dict, manifest: dict) -> bool:
    """Whether this stack has containers on the box, at all.

    `DEPLOY_LOG` used to answer this, and it answers a different question: that
    log is written when a deploy is started *from this page*. Deploying with
    `./home-stack deploy` -- which is how the README does it and how this
    household does it -- leaves it absent forever. So the landing page greeted a
    house running forty-four containers with "nothing has been deployed yet,
    nothing works until you do": the front door telling a working household it
    is broken, above a to-do list whose whole job is to be believed.

    Cheap on purpose: one `docker ps` (0.03s here) against the same project
    names `service_status()` matches on, `compose_project` included. Asking
    `service_status()` would cost 1.3s on the one page that must open at once.
    """
    projects = set()
    for name, spec in (manifest.get("services") or {}).items():
        if not ((cfg.get("services") or {}).get(name) or {}).get("enabled", True):
            continue
        for unit in (spec.get("units") or []):
            if unit.get("name"):
                projects.add(unit.get("compose_project") or f"{name}-{unit['name']}")
    return any(c["project"] in projects for c in _docker_ps())


def setup_todo(cfg: dict, manifest: dict) -> list[dict]:
    """What still needs a person, most blocking first.

    The overview used to be a service count and a table of hosts -- true, and
    no help at all on a fresh install, where the three things that actually
    stop the house working are a missing API key, nobody in the household, and
    changes that were saved but never deployed. Saving is not deploying, so the
    last one is the failure this whole page is written against.
    """
    todo: list[dict] = []
    present = secret_keys_present()

    missing = [k for k in REQUIRED_SECRETS if not present.get(k)]
    if missing:
        todo.append({
            "key": "admin.overview.todo_secret",
            # No `key` in here: `t(item.key, **item.params)` would hand the
            # translator two values for its first argument and 500 the page.
            "params": {},
            "endpoint": "secrets_page",
            "action": "admin.overview.todo_secret_do",
            "level": "warn",
        })

    if not (cfg.get("members") or []):
        todo.append({
            "key": "admin.overview.todo_members",
            "params": {},
            "endpoint": "members",
            "action": "admin.overview.todo_members_do",
            "level": "warn",
        })

    pending = expand_pending(load_pending(), manifest)
    if pending:
        todo.append({
            "key": "admin.overview.todo_pending",
            "params": {"n": len(pending), "names": ", ".join(sorted(pending))},
            "endpoint": "deploy",
            "action": "admin.overview.todo_pending_do",
            "level": "attn",
        })
    elif not anything_deployed(cfg, manifest):
        todo.append({
            "key": "admin.overview.todo_never",
            "params": {},
            "endpoint": "deploy",
            "action": "admin.overview.todo_never_do",
            "level": "attn",
        })

    return todo


@app.get("/")
def overview():
    cfg = load_config()
    manifest = load_manifest()
    services = manifest.get("services", {})
    enabled = [
        n for n in services
        if ((cfg.get("services") or {}).get(n) or {}).get("enabled", True)
    ]
    return render_template(
        "overview.html",
        cfg=cfg,
        hosts=cfg.get("hosts", {}),
        # True only when the roles do not all point at the same machine. On the
        # shipped install they do, and the card is three identical rows.
        hosts_split=len({(h or {}).get("address")
                         for h in (cfg.get("hosts") or {}).values()}) > 1,
        enabled_count=len(enabled),
        total_count=len(services),
        member_count=len(cfg.get("members") or []),
        todo=setup_todo(cfg, manifest),
    )


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
# Which model answers as which persona, what it costs, and what this house has
# measured. The choice lives in `assistant.models` in the site config, and the
# deployer renders it into the assistant's own config — so this page edits one
# place and nothing has to be kept in step by hand.

# What each model source is called on the page, and the order they read in.
# Keyed by the `provider` a fetched model carries, so the picker's groups and
# the roster's "where" column cannot drift apart -- and so an added source is
# one entry here rather than three template edits.
#
# The two Ollamas are deliberately separate. They speak the same API and mean
# opposite things about where a household's data goes: one is hardware you own
# and nothing leaves, the other is ollama.com. Under a single "Ollama" heading
# there was no way to tell which one you had picked.
# What faster-whisper can be asked to load, smallest first. The names are
# whisper's own; `large-v3-turbo` is the deployer's default for a GPU install
# and `small` is what a CPU-only compute host can keep up with.
WHISPER_MODELS = ("tiny", "base", "small", "medium",
                  "large-v2", "large-v3", "large-v3-turbo")
# What the deployer fills in when `services.faster-whisper.model` is unset
# (CONFIG_DEFAULTS in deploy.py). Shown as the current choice in that case:
# with nothing selected the browser picks the first option, and saving the
# Speech card for its TTS field would quietly move the house to `tiny`.
WHISPER_DEFAULT = "large-v3-turbo"

MODEL_GROUPS = (
    {"provider": "opencode_zen", "key": "admin.models.source_opencode_zen"},
    {"provider": "openrouter", "key": "admin.models.source_openrouter"},
    {"provider": "together", "key": "admin.models.source_together"},
    {"provider": "openai", "key": "admin.models.source_openai"},
    {"provider": "ollama", "key": "admin.models.source_ollama_local"},
    {"provider": "ollama_vision", "key": "admin.models.source_ollama_vision"},
    {"provider": "ollama_cloud", "key": "admin.models.source_ollama_cloud"},
    {"provider": "openai_compatible", "key": "admin.models.source_openai_compatible"},
    {"provider": "freetoken", "key": "admin.models.source_freetoken"},
)

# The three that have nothing to configure but a key. Filling one in is the
# whole of switching it on, so there is no checkbox for them: a switch beside a
# key that is already set would be a second thing to get wrong.
KEYED_SOURCES = (
    ("openrouter", "OPENROUTER_API_KEY"),
    ("together", "TOGETHER_API_KEY"),
    ("openai", "OPENAI_API_KEY"),
    # Zen's catalogue comes from models.dev and needs no key, but deciding
    # which of it *this* account can route does: models.dev lists the whole
    # roster and an account is offered a subset, with nothing in any field to
    # say which. `fetch_opencode_zen` asks, once per model, and caches.
    ("opencode_zen", "OPENCODE_API_KEY"),
)


def model_groups(cfg: dict) -> list[dict]:
    """The groups, labelled. An OpenAI-compatible server uses the household's
    own name for it when they gave one -- "the GPU box" is more use on this
    page than a category, and it is the only source whose identity this package
    cannot know."""
    compat = (cfg.get("cloud") or {}).get("openai_compatible") or {}
    label = str(compat.get("label") or "").strip()
    ft_label = str(((cfg.get("cloud") or {}).get("freetoken") or {})
                   .get("label") or "").strip()
    out = []
    for group in MODEL_GROUPS:
        name = t(group["key"])
        if group["provider"] == "openai_compatible" and label:
            name = label
        if group["provider"] == "freetoken" and ft_label:
            name = ft_label
        out.append({"provider": group["provider"], "label": name})
    return out


def _source_settings(cfg: dict) -> dict:
    """The three configurable sources, with whatever the config holds today.

    Defaults are filled in here rather than in the template, so a config that
    has never carried the block still renders a usable form. The alternative is
    a page that shows blanks and then writes them back.
    """
    conf = (cfg.get("cloud") or {}).get("ollama") or {}
    local = conf.get("local") or {}
    cloud = conf.get("cloud") or {}
    compat = (cfg.get("cloud") or {}).get("openai_compatible") or {}
    freetoken = (cfg.get("cloud") or {}).get("freetoken") or {}
    return {
        "ollama_local": {"enabled": bool(local.get("enabled", True)),
                         "host": local.get("host", "compute"),
                         "port": local.get("port", 11434)},
        # Off and empty by default: an absent second endpoint means the vision
        # model rides the main ollama, which is what every household did before
        # this existed.
        "ollama_vision": {"enabled": bool((conf.get("vision") or {}).get("enabled")),
                          "url": (conf.get("vision") or {}).get("url", "")},
        # `enabled` here is a *report*, not a setting: it says whether the key
        # is there, so the page can show the source as on without offering a
        # second way to switch it.
        "ollama_cloud": {"enabled": bool(get_secret("OLLAMA_API_KEY")),
                         "url": cloud.get("url", "https://ollama.com")},
        "compat": {"enabled": bool(compat.get("enabled")),
                   "url": compat.get("url", ""),
                   "label": compat.get("label", "")},
        # Its own row beside compat, not instead of it: a household may point
        # at FreeToken and at something else that speaks the same API.
        "freetoken": {"enabled": bool(freetoken.get("enabled")),
                      "url": freetoken.get("url", ""),
                      "label": freetoken.get("label", "")},
    }


def _from_this_container(host: str) -> str:
    """An address as *this* container can dial it.

    The page runs in a container, so a role's 127.0.0.1 is this container and
    not the machine the model server is on. Every catalogue refresh failed that
    way once, and the Models page offered a household a choice it could not
    see. Same rule as add_container_addresses() in deploy.py.
    """
    # The other direction. A URL typed for bridge containers -- the vision
    # Ollama's `host.docker.internal:11435`, which is how the assistants and
    # Paperless reach it -- names nothing from a container on the host's own
    # network, which is what this one is on the default install. There the
    # host is loopback. Measured 2026-09-10: every vision refresh failed with
    # "Name or service not known" and the picker offered nothing from :11435.
    if host == "host.docker.internal":
        return ("127.0.0.1" if os.environ.get("HOME_STACK_HOST_NETWORK") == "1"
                else host)
    if host not in ("127.0.0.1", "::1", "localhost"):
        return host
    # On host networking there is nothing to translate: 127.0.0.1 already is
    # the machine, and `host.docker.internal` does not resolve there at all --
    # so rewriting would turn a working address into a name that fails.
    if os.environ.get("HOME_STACK_HOST_NETWORK") == "1":
        return host
    return "host.docker.internal"


def local_model(value: str) -> tuple[str, str]:
    """`ollama-vision:qwen3-vl:4b` -> ("ollama:qwen3-vl:4b", "vision").

    The picker lists a local model once, under `ollama:`; the instance is
    shown beside it. Anything that is not a local model comes back as given,
    with no instance.
    """
    text = str(value or "").strip()
    prefix, sep, rest = text.partition(":")
    provider = OI.provider_of_prefix(prefix) if sep and rest else ""
    if not provider:
        return text, ""
    return f"ollama:{rest}", OI.id_of_provider(provider)


def model_endpoints(cfg: dict) -> dict:
    """Every model source this house has switched on, and how to reach it.

    Three kinds, and each is a separate row on the Models page because each
    fails separately and costs differently:

      * **local ollama** -- hardware you own; nothing leaves the house.
      * **cloud ollama** -- ollama.com; a key, and what you send it leaves.
      * **openai-compatible** -- anything that speaks `/v1/models` and
        `/v1/chat/completions` at a URL you give it: another box on the LAN, a
        vLLM or llama.cpp server, a provider this package has never heard of.

    This used to read `cloud.ollama.mode`, `cloud.ollama.host` and
    `cloud.ollama.cloud_url` -- a schema the config has not used since local
    and cloud could both be on at once. It happened to work for the local case
    because every lookup missed and fell through to its default, and it could
    never reach a cloud Ollama at all: `mode` is absent, so the branch that
    reads the key was dead code. Read from the same block the deployer reads.
    """
    conf = (cfg.get("cloud") or {}).get("ollama") or {}
    hosts = cfg.get("hosts") or {}
    out = {}

    # `mode: local|cloud` was the shape before both could be on. Translated
    # rather than ignored, the same way deploy.py translates it, so a config
    # written back then still shows the right thing here.
    if "mode" in conf and "local" not in conf and "cloud" not in conf:
        legacy = str(conf.get("mode", "local")).lower()
        conf = {"local": {"enabled": legacy == "local",
                          "host": conf.get("host", "compute"),
                          "port": conf.get("port", 11434)},
                "cloud": {"enabled": legacy == "cloud",
                          "url": conf.get("cloud_url", "https://ollama.com")}}

    # Every local instance (cloud.ollama.instances; a config from before the
    # list is read as the list it means), under its provider name. The
    # benchmark's is left out: no role runs on it.
    try:
        local_instances = OI.serving(cfg)
    except OI.InstanceError:
        local_instances = []           # the instances card says why
    for inst in local_instances:
        url = inst["url"] or inst.get("legacy_url") or ""
        role = inst["host"]
        if not url:
            host_cfg = hosts.get(role) or {}
            host = _from_this_container(
                host_cfg.get("from_container") or host_cfg.get("address", "127.0.0.1"))
            url = f"http://{host}:{inst['port']}"
        else:
            parsed = urllib.parse.urlsplit(url)
            if parsed.hostname:
                fixed = _from_this_container(parsed.hostname)
                if fixed != parsed.hostname:
                    netloc = fixed + (f":{parsed.port}" if parsed.port else "")
                    url = urllib.parse.urlunsplit(parsed._replace(netloc=netloc))
        # The literal "ollama", never empty: nanobot resolves ${VAR} in every
        # string in its config and refuses to start on an unset reference.
        out[OI.provider_of(inst["id"])] = {"url": url.rstrip("/"), "key": "ollama",
                                           "role": role if inst["id"] == "main" else "",
                                           "instance": inst["id"]}

    cloud = conf.get("cloud") or {}
    cloud_key = get_secret("OLLAMA_API_KEY")
    if cloud_key:
        out["ollama_cloud"] = {
            "url": str(cloud.get("url") or "https://ollama.com").rstrip("/"),
            "key": cloud_key, "role": ""}

    # A key is the switch for these three. There is no url and no toggle: an
    # account either can reach openrouter.ai or it cannot.
    for source, key_name in KEYED_SOURCES:
        key = get_secret(key_name)
        if key:
            out[source] = {"url": "", "key": key, "role": ""}

    # Both self-hosted slots, and the same rule for each. A LAN address here is
    # somebody else's box far more often than it is this one, but when it *is*
    # this one the container translation applies -- that is what made a
    # catalogue refresh offer a household a choice it could not see.
    for source, block, secret in (
            ("openai_compatible", "openai_compatible", "OPENAI_COMPATIBLE_API_KEY"),
            ("freetoken", "freetoken", "FREETOKEN_API_KEY")):
        conf = (cfg.get("cloud") or {}).get(block) or {}
        if not (conf.get("enabled") and str(conf.get("url") or "").strip()):
            continue
        url = str(conf["url"]).strip().rstrip("/")
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname:
            fixed = _from_this_container(parsed.hostname)
            if fixed != parsed.hostname:
                netloc = fixed + (f":{parsed.port}" if parsed.port else "")
                url = urllib.parse.urlunsplit(parsed._replace(netloc=netloc))
        out[source] = {"url": url, "key": get_secret(secret),
                       "label": str(conf.get("label") or "").strip(), "role": ""}

    return out


# Where a model written `together:foo` in assistant.models is actually reached.
# The prefixes are deploy.py's MODEL_PROVIDERS; the bases are the ones the
# catalogue already fetches from, so a probe goes exactly where a turn goes.
_PROBE_BASES = {
    "opencode_zen": model_catalogue.ZEN_API_BASE,
    "together": "https://api.together.xyz/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
    "ollama_cloud": "https://ollama.com/v1",
}
# The Models-page roles whose consumer calls the provider itself, over
# /v1/chat/completions only (DIRECT_ROLES in deploy.py).
DIRECT_ROLES = ("titles", "documents")


def _responses_only_for(role: str, value: str) -> bool:
    """A Zen model this role's consumer cannot call.

    home-core's titler and Paperless speak chat completions only, and Zen
    serves its GPT-5-class models on /v1/responses only. The rule itself is
    `_wants_responses` in models.py -- one of the three existing copies, not a
    new one. Refused here and in the deployer (`check_direct_models`), and
    kept out of these two pickers, so a hand-edited config still meets it.
    """
    if role not in DIRECT_ROLES or not value:
        return False
    prefix, sep, _ = value.partition(":")
    on_zen = not (sep and prefix in _PROBE_PREFIXES)
    return on_zen and model_catalogue._wants_responses(value)


_PROBE_PREFIXES = {
    "ollama": "ollama", "ollama-cloud": "ollama_cloud", "openrouter": "openrouter",
    "together": "together", "openai": "openai",
    "openai-compatible": "openai_compatible", "freetoken": "freetoken",
    # The second ollama. Without it `ollama-vision:x` fell through to
    # OpenCode Zen, and the Test button failed every vision/documents model.
    "ollama-vision": "ollama_vision",
}


def _probe_target(cfg: dict, value: str) -> tuple[str, str, str, str]:
    """(model id, base url, api key, source) for a configured model string.

    Split on the first colon only: an Ollama model name carries one of its own
    (`qwen3-vl:4b`), and splitting greedily makes the tag into the provider.
    """
    text = str(value or "").strip()
    prefix, sep, rest = text.partition(":")
    source = (_PROBE_PREFIXES.get(prefix) or OI.provider_of_prefix(prefix) or None) if sep and rest else None
    model_id = rest if source else text
    source = source or "opencode_zen"
    endpoints = model_endpoints(cfg)
    endpoint = endpoints.get(source) or {}
    base = _PROBE_BASES.get(source) or ""
    if not base and endpoint.get("url"):
        # A URL the household gave us: local Ollama, FreeToken, a vLLM box.
        base = f"{str(endpoint['url']).rstrip('/')}/v1"
    return model_id, base, str(endpoint.get("key") or ""), source


@app.post("/models/test")
def models_test():
    """Run one role's model through a real turn and report what happened.

    A page that only lists prices cannot tell you the model is broken, and on
    2026-09-02 three separate faults each broke a role while this page looked
    entirely healthy. `probe_model` reproduces the shapes that failed.
    """
    role = (request.form.get("role") or "").strip()
    cfg = load_config()
    chosen = ((cfg.get("assistant") or {}).get("models") or {})
    if role not in chosen and role not in model_catalogue.IMAGE_SLOTS:
        return jsonify({"ok": False, "error": t("admin.models.test_unknown_role")}), 400
    raw = chosen.get(role)
    if isinstance(raw, list):
        return jsonify(_probe_chain(cfg, role, [str(v).strip() for v in raw if str(v or "").strip()])), 200
    value = str(raw or "").strip()
    if not value:
        return jsonify({"ok": False, "error": t("admin.models.test_no_model")}), 400
    # The benchmark's own cases for this role, through the assistant loop.
    if role in ROLE_BENCH_GROUPS:
        groups = ROLE_BENCH_GROUPS[role]
        try:
            model, _note = model_bench.resolve_model(value)
        except model_bench.ResolveError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        effort = str(((cfg.get("assistant") or {}).get("reasoning_effort") or {}).get(role) or "")
        launched = _bench_launch([model], groups, 1, effort if effort in model_bench.EFFORTS else "",
                                 "", cfg)
        if not launched.get("ok"):
            return jsonify({"ok": False, "error": launched.get("reason", "")}), 409
        counts = model_bench.case_counts(ROOT / "services" / "nanobot" / "bench" / "cases.json")
        n = sum(counts.get(g, 0) for g in groups)
        detail = (_t_or("admin.models.test_bench_queued", "queued behind the running benchmark "
                        "(place {n})", n=launched.get("position"))
                  if launched.get("queued") else
                  _t_or("admin.models.test_bench_started", "running now"))
        return jsonify({"ok": None, "bench": True, "role": role, "configured": value,
                        "model": model, "cases": n, "groups": ",".join(groups),
                        "source": "benchmark", "steps": [{
                            "step": _t_or("admin.models.test_bench_step",
                                          "benchmark: {cases} cases ({groups})", cases=n,
                                          groups=", ".join(groups)),
                            "ok": None, "detail": detail}]}), 200
    harness = (cfg.get("assistant") or {}).get("harness") or {}
    if role in ("subagent", "subagent_powerful") and harness.get("enabled"):
        result = _probe_on_pi(role, value)
        result["role"], result["configured"], result["source"] = role, value, "pi"
        return jsonify(result), 200
    model_id, base, key, source = _probe_target(cfg, value)
    effort = str(((cfg.get("assistant") or {}).get("reasoning_effort")
                  or {}).get(role) or "")
    # A role with a probe of its own gets asked what it is actually for, and
    # the answer is scored. The rest keep the generic reachability check --
    # which is not a lesser test, it is a different question, and answering
    # the wrong one with a number would be worse than not scoring at all.
    if role in model_catalogue.ROLE_PROBES:
        result = model_catalogue.score_model(role, model_id, base, key, effort)
        model_catalogue.record_score(SCORES_CACHE, role, value, result)
    else:
        result = model_catalogue.probe_model(model_id, base, key, effort)
    result["role"] = role
    result["source"] = source
    result["configured"] = value
    return jsonify(result), 200


def _probe_chain(cfg: dict, role: str, chain: list[str]) -> dict:
    """An ordered chain (the rescue models), tested place by place: each is a
    model a failing turn may land on, so each has to answer. The whole list
    handed over as one name was a model called "['ollama-text:…', …]" that
    no provider has, and the test failed whatever the chain held."""
    if not chain:
        return {"ok": False, "error": t("admin.models.test_no_model")}
    effort = str(((cfg.get("assistant") or {}).get("reasoning_effort") or {}).get(role) or "")
    steps, oks, total_ms = [], [], 0
    for i, value in enumerate(chain, 1):
        model_id, base, key, source = _probe_target(cfg, value)
        res = model_catalogue.probe_model(model_id, base, key, effort)
        oks.append(res.get("ok"))
        total_ms += int(res.get("ms") or 0)
        detail = (f"{res['ms'] / 1000:.1f}s" if res.get("ms") else "")
        if res.get("ok") is False:
            bad = next((st.get("detail") or st.get("step") for st in res.get("steps") or []
                        if st.get("ok") is False), "") or res.get("error", "")
            detail = (detail + " — " if detail else "") + str(bad)[:160]
        steps.append({"step": f"{i}. {value}", "ok": res.get("ok"), "detail": detail})
    return {"ok": all(o is True for o in oks) if all(o is not None for o in oks) else None,
            "steps": steps, "ms": total_ms, "role": role, "configured": ", ".join(chain),
            "why": _t_or("admin.models.chain_probe_why",
                         "each rescue model in order: every one a failing turn may land on has to answer")}


def _probe_on_pi(role: str, configured: str = "") -> dict:
    """A sub-agent role's Test with the pi switch on: one short background task
    run through pi inside an assistant (nanobot/harness/probe.py), because that
    is where the model will run -- a direct question says it answers, not that
    it can use pi's tools and hand back a file. Bench mode: the document is
    stubbed, nothing reaches the share. An OpenCode Go model is reached from
    pi, the one place it may be; this page never calls Go itself."""
    containers = bench_containers()
    if not containers:
        return {"ok": False, "error": "no assistant container is running"}
    started = time.monotonic()
    try:
        out = subprocess.run(["docker", "exec", containers[0], "python", "-m",
                              "nanobot.harness.probe", role],
                             capture_output=True, text=True, timeout=240)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"pi did not finish: {exc}"[:200]}
    line = next((ln for ln in out.stdout.splitlines() if ln.startswith("PROBE_JSON ")), "")
    try:
        doc = json.loads(line[len("PROBE_JSON "):]) if line else {}
    except ValueError:
        doc = {}
    if not doc:
        tail = (out.stderr or out.stdout or "").strip().splitlines()[-1:] or ["no answer"]
        return {"ok": False, "error": tail[0][:200]}
    steps = []
    if doc.get("model"):
        steps.append({"step": _t_or("admin.models.pi_probe_model", "ran on pi with {model}",
                                    model=doc["model"]), "ok": None})
        # The assistant runs the config it was deployed with. A choice saved
        # since is not what was tested -- same name on another endpoint (Zen
        # where Go was picked) included -- and the page says so.
        want_go = configured.startswith("opencode-go/")
        want = configured.split("/", 1)[1] if want_go else local_model(configured)[0].split(":", 1)[-1] \
            if local_model(configured)[0].startswith("ollama:") else configured
        on_go = "/zen/go/" in str(doc.get("base") or "")
        if configured and (want != doc["model"] or want_go != on_go):
            steps.append({"step": _t_or("admin.models.pi_probe_stale",
                                        "the assistants still run {model}; deploy nanobot to test {configured}",
                                        model=doc["model"] + (" (Go)" if on_go else ""),
                                        configured=configured), "ok": None})
    if doc.get("error") and doc.get("ok") is not True:
        steps.append({"step": _t_or("admin.models.pi_probe_error", "stopped"),
                      "ok": False if doc.get("ok") is False else None, "detail": doc["error"]})
    if "rounds" in doc:
        used = [t["name"] + (" ✗" if t.get("error") else "") for t in doc.get("tools") or []]
        steps.append({"step": _t_or("admin.models.pi_probe_tools", "tools it used"),
                      "ok": bool(used) or None, "detail": ", ".join(used) or "none"})
        steps.append({"step": _t_or("admin.models.pi_probe_file", "handed back the file"),
                      "ok": bool(doc.get("links")),
                      "detail": ", ".join(doc.get("links") or []) or "no download link"})
        steps.append({"step": _t_or("admin.models.pi_probe_rounds", "rounds and turns"),
                      "ok": None, "detail": f"{doc['rounds']} round(s), {doc.get('turns', 0)} turn(s)"})
    return {"ok": doc.get("ok"), "steps": steps, "reply": doc.get("reply") or "",
            "ms": int((time.monotonic() - started) * 1000),
            "why": _t_or("admin.models.pi_probe_why",
                         "a short background task in pi: look something up and make a one-page PDF")}


def _t_or(key: str, fallback: str, **params) -> str:
    """A translation, or *fallback* when the catalogue has never heard of it.

    `Translator.__call__` returns the key itself for a miss, which on a page is
    a visible `admin.models.role_everyday` where a sentence should be. The
    persona strings live in `PERSONA_NEEDS` as well as in the catalogue -- they
    are the code's own documentation of what each role is for -- so the literal
    is the honest last resort rather than the key name.

    `params` are substituted into whichever of the two is used. Without that
    this signature was a 500 waiting for one branch to be taken: the caller
    that needed `{roles}` filled in raised TypeError instead, so the models
    page died with Werkzeug's own error text at the moment it had something to
    say. Same `{name}` replacement the translator does, rather than `.format`,
    so a stray brace in a translation is text and not a crash.
    """
    text = t(key, **params)
    if text == key:
        text = fallback
        for name, value in params.items():
            text = text.replace("{" + name + "}", str(value))
    return text


def refresh_catalogue(cfg: dict, recheck: bool = False) -> dict:
    return model_catalogue.refresh(
        MODELS_CACHE, model_endpoints(cfg), recheck,
        # Where the usage store lives, so the roster can be priced against what
        # this house actually spends while it is being fetched.
        state_dir=str((cfg.get("paths") or {}).get("state") or ""))


def _price_check_interval(cfg: dict) -> int | None:
    """Seconds between refreshes, or None when it is switched off."""
    how = str(((cfg.get("assistant") or {}).get("price_check") or "daily")).lower()
    return {"daily": 24 * 3600, "weekly": 7 * 24 * 3600}.get(how)


def _price_watcher() -> None:
    """Refresh the catalogue on the configured schedule.

    A thread rather than a cron entry: this page is the only thing that reads
    the cache, so the refresh belongs to it and stops when it stops. It fetches
    once at startup only if the cache is already stale — a container restart
    should not be a reason to call models.dev.
    """
    while True:
        try:
            cfg = load_config()
            interval = _price_check_interval(cfg)
            if interval is None:
                time.sleep(3600)
                continue
            cached = model_catalogue.load_cache(MODELS_CACHE)
            # By age, or because older code built it. The second is why a fix
            # to the roster could ship and change nothing the household saw.
            if model_catalogue.cache_is_stale(cached, interval):
                refresh_catalogue(cfg)
            time.sleep(min(interval, 3600))
        except Exception:            # never take the page down for a price
            time.sleep(3600)


# One refresh at a time, and never on the request thread. A stale cache used to
# be refreshed *during* the page load, which put models.dev, together, ollama
# and both OpenCode rosters between somebody pressing a link and seeing a page
# -- two to three seconds when every provider answered, and the full timeout
# when one did not. The cache is what the page renders from either way, so
# waiting bought nothing except a page that was old *and* slow.
#
# The flag is not a lock held across the fetch: two loads arriving together
# should produce one refresh and two immediate pages, not one page waiting on
# the other's network call.
_refresh_flag = threading.Lock()
_refreshing = False


def refresh_in_background(cfg: dict) -> bool:
    """Start a refresh unless one is already running. Returns immediately."""
    global _refreshing
    with _refresh_flag:
        if _refreshing:
            return False
        _refreshing = True

    def run():
        global _refreshing
        try:
            refresh_catalogue(cfg)
        except Exception:            # never take the page down for a roster
            pass
        finally:
            with _refresh_flag:
                _refreshing = False

    threading.Thread(target=run, daemon=True).start()
    return True


@app.route("/models", methods=["GET", "POST"])
@app.route("/models/catalog", endpoint="models_catalog")
def models_page():
    cfg = load_config()
    # The benchmark and the roster with its prices are their own page, over
    # the same context (see the tabs at the top of models.html).
    view = "catalog" if request.endpoint == "models_catalog" else "settings"

    if request.method == "POST":
        action = request.form.get("action", "save")
        if action == "refresh":
            # Pressing Refresh re-asks together.ai which models a serverless
            # key can actually call. The scheduled refresh reuses what it
            # learned, so this is the only path that pays for it.
            cat = refresh_catalogue(cfg, recheck=True)
            if cat.get("opencode_zen_error"):
                flash(translator("admin.models.refresh_error",
                                 locale=current_locale(cfg),
                                 detail=cat["opencode_zen_error"]), "error")
            else:
                flash(translator("admin.models.refresh_ok",
                                 locale=current_locale(cfg),
                                 n=len(cat.get("opencode_zen") or [])), "ok")
            return redirect(url_for("models_page"))

        before = load_config()
        assistant = cfg.setdefault("assistant", {})
        chosen = assistant.setdefault("models", {})
        # The zero-cost rule, enforced here as well as by keeping those
        # models out of the picker. The picker is what a person sees; this is
        # what a POST gets, and the two are only the same until somebody edits
        # the form, replays a save, or hand-edits the config and presses save.
        refused = []
        refused_go = []
        refused_responses = []
        # The cached catalogue, not `cat`: that name is only bound inside the
        # refresh branch above, which returns before reaching here. Read from
        # the cache rather than fetched -- a save is not the moment to go to
        # the network, and a price this page has never seen is an unknown one,
        # which `zero_cost_ok` treats as allowed rather than as free.
        priced = model_catalogue.load_cache(MODELS_CACHE)
        by_id = {m.get("id"): m for m in model_catalogue.all_models(priced)
                 if isinstance(m, dict)}
        refused_local = []
        local_best = None            # the benchmark's ranking, read on first need
        # The rescue chain: one picker per place, in order (CHAIN_SLOTS).
        for persona in CHAIN_ROLES:
            if f"model:{persona}:0" not in request.form:
                continue
            chain_now = chosen.get(persona)
            chain_now = list(chain_now) if isinstance(chain_now, list) else (
                [chain_now] if chain_now else [])
            new_chain = []
            for i in range(CHAIN_SLOTS):
                value = (request.form.get(f"model:{persona}:{i}") or "").strip()
                if not value:
                    continue
                if value.startswith("__local__:"):
                    value = _setup_value(cfg, value.split(":", 1)[1], persona)
                    if not value:
                        refused_local.append(persona)
                        continue
                elif value == "__local__":
                    # Keep the local entry this place had; else the first local
                    # one the chain had; else the default local pick.
                    here = str(chain_now[i]) if i < len(chain_now) else ""
                    olds = [str(v) for v in chain_now if local_model(str(v))[0].startswith("ollama:")]
                    value = (here if local_model(here)[0].startswith("ollama:")
                             else next((v for v in olds if v not in new_chain), "")
                             or _default_local(cfg, persona))
                    if not value:
                        refused_local.append(persona)
                        continue
                elif model_catalogue.is_go_model(value):
                    refused_go.append(f"{persona} ({value})")
                    continue
                if value not in new_chain:
                    new_chain.append(value)
            if new_chain:
                chosen[persona] = new_chain
        for persona in model_catalogue.PERSONA_NEEDS:
            if persona in CHAIN_ROLES:
                continue
            value = (request.form.get(f"model:{persona}") or "").strip()
            if not value:
                continue
            # "Local Ollama": which setup is the Ollama card's. A role that
            # is already local keeps its setup; one moving to local gets the
            # setup the benchmark ranks best for its kind (_default_local).
            # A setup picked by name, straight from the picker: that setup's
            # address, whatever its engine.
            if value.startswith("__local__:"):
                value = _setup_value(cfg, value.split(":", 1)[1], persona)
                if not value:
                    refused_local.append(persona)
                    continue
            elif value == "__local__":
                current_value = str(chosen.get(persona) or "")
                if local_model(current_value)[0].startswith("ollama:"):
                    continue
                if local_best is None:
                    local_best = _bench_local_best(_bench_views(cfg))
                value = _default_local(cfg, persona, by_id, local_best)
                if not value:
                    refused_local.append(persona)
                    continue
            # The flat plan is for the Code space and nothing else. The
            # picker cannot offer these -- Go is not in SOURCES -- so reaching
            # here means an edited form, a replayed save, or a hand-edited
            # config, which is exactly when a rule has to hold.
            if model_catalogue.is_go_model(value):
                # The one exception: the sub-agent roles, when the household
                # allowed Go for pi in this same save. pi runs them; nanobot's
                # own loop never does (it falls back to the everyday model).
                if not (persona in ("subagent", "subagent_powerful")
                        and request.form.get("harness_allow_go") == "on"):
                    refused_go.append(f"{persona} ({value})")
                    continue
            if not model_catalogue.zero_cost_ok(persona, by_id.get(local_model(value)[0])):
                refused.append(f"{persona} ({value})")
                continue
            if _responses_only_for(persona, value):
                refused_responses.append(f"{persona} ({value})")
                continue
            chosen[persona] = value
        if refused_local:
            flash(_t_or("admin.models.local_refused",
                        "Not saved for {roles}: no local model can do it yet. Add a "
                        "model setup on the Ollama card first.",
                        roles=", ".join(refused_local)), "warn")
        if refused_go:
            flash(_t_or("admin.models.go_refused",
                        "Not saved for {roles}: a flat-plan model is only for "
                        "the Code space.", roles=", ".join(refused_go)), "warn")
        if refused_responses:
            flash(_t_or("admin.models.responses_refused",
                        "Not saved for {roles}: that service calls "
                        "/v1/chat/completions, and OpenCode Zen serves this "
                        "model on /v1/responses only.",
                        roles=", ".join(refused_responses)), "warn")
        if refused:
            flash(_t_or("admin.models.zero_cost_refused",
                        "Not saved for {roles}: a model that costs nothing is "
                        "not for the assistant's unattended traffic.",
                        roles=", ".join(refused)), "warn")
        # The two image slots. Blank is a real answer here -- a household that
        # does not want the assistant drawing leaves them empty and the skill
        # says it cannot rather than falling back to whatever it shipped with.
        #
        # Which is exactly why the marker is needed: "blank" and "this card was
        # not on screen" are the same absence in a POST, and this page is three
        # forms to one endpoint. Without `scope:images` a save of the persona
        # card or the sources card cleared both slots, silently, and the next
        # deploy stopped exporting IMAGE_MODEL at all.
        if request.form.get("scope:images"):
            for slot in model_catalogue.IMAGE_SLOTS:
                value = (request.form.get(f"model:{slot}") or "").strip()
                if value:
                    chosen[slot] = value
                else:
                    chosen.pop(slot, None)
        # The Code space's model. Behind its own marker for the same reason as
        # the image slots: this page is several forms to one endpoint, and an
        # absent field means "that card was not on screen", not "clear it".
        #
        # Validated against the roster the card was drawn from rather than
        # accepted as typed: a model this account cannot reach fails as
        # `Model is disabled` in the middle of somebody's question, which reads
        # as the Programmer being broken. Blank is allowed and means "opencode
        # decides" -- worth naming, because on a Zen account that is a free
        # model whose terms allow training on what it reads, and what the
        # Programmer reads is this house's code.
        if request.form.get("scope:code"):
            wanted = (request.form.get("code_model") or "").strip()
            offerable = {m["id"] for g in model_catalogue.code_model_choices(
                model_catalogue.load_cache(MODELS_CACHE)) for m in g["models"]}
            opencode = cfg.setdefault("cloud", {}).setdefault("opencode", {})
            if not wanted:
                opencode["model"] = ""
            elif wanted in offerable or wanted == str(opencode.get("model") or ""):
                opencode["model"] = wanted
            else:
                flash(_t_or("admin.models.code_unknown",
                            "Not saved: {model} is not on this account's "
                            "roster.", model=wanted), "warn")

        # The Speech card: which whisper listens and which audio.cpp package
        # speaks. Both already lived in the config -- `services.faster-whisper
        # .model` had no page at all, `services.audio-cpp.tts_model` was on
        # the Voice page -- and both are models the house runs, which is what
        # this page is for. Behind a marker for the same reason as the image
        # slots: this page is several forms to one endpoint.
        if request.form.get("scope:speech"):
            services = cfg.setdefault("services", {})
            whisper = (request.form.get("whisper_model") or "").strip()
            if whisper in WHISPER_MODELS:
                services.setdefault("faster-whisper", {})["model"] = whisper
            # Same rule as the Voice page: only a package the service actually
            # ships, or the container 404s on the first sentence.
            audio = services.setdefault("audio-cpp", {})
            tts = (request.form.get("tts_model") or "").strip()
            served = [x.strip() for x in str(audio.get("tts_packages") or "").split(",")
                      if x.strip()]
            if tts in served:
                audio["tts_model"] = tts

        how = (request.form.get("price_check") or "").strip().lower()
        if how in ("daily", "weekly", "off"):
            assistant["price_check"] = how

        # Where the models come from. Edited here rather than on the Site page:
        # this is where somebody is standing when they notice a source is
        # missing from the picker.
        #
        # Behind the same marker as the image slots, and for the same reason:
        # an unchecked box sends nothing, so a save of either card above read
        # as "local Ollama off, OpenAI-compatible off" and blanked the URL and
        # label somebody had typed. Only the card that was on screen may speak
        # for these.
        # Background tasks on the pi harness, beside the sub-agent models it
        # runs. Behind its own marker: an unchecked box sends nothing, and a
        # save of any other card must not read as "harness off".
        if request.form.get("scope:harness"):
            harness = assistant.setdefault("harness", {})
            harness["enabled"] = request.form.get("harness_enabled") == "on"
            harness["allow_go"] = request.form.get("harness_allow_go") == "on"

        if request.form.get("scope:sources"):
            cloud = cfg.setdefault("cloud", {})
            ollama = cloud.setdefault("ollama", {})
            # The house's own Ollama servers are the instances card's
            # (POST /models/ollama), not this one's.

            # Only the address. Whether ollama.com is on is decided by whether
            # its key is set, so there is no checkbox here to disagree with it.
            cloud_ollama = ollama.setdefault("cloud", {})
            url = (request.form.get("ollama_cloud_url") or "").strip()
            if url:
                cloud_ollama["url"] = url

            compat_url = (request.form.get("compat_url") or "").strip()
            parsed = urllib.parse.urlsplit(compat_url)
            if compat_url and (parsed.scheme not in ("http", "https")
                               or not parsed.hostname):
                flash(translator("admin.models.invalid_url",
                                 locale=current_locale(cfg)), "error")
                return redirect(url_for("models_page"))

            ft_url = (request.form.get("freetoken_url") or "").strip()
            parsed = urllib.parse.urlsplit(ft_url)
            if ft_url and (parsed.scheme not in ("http", "https")
                           or not parsed.hostname):
                flash(translator("admin.models.invalid_url",
                                 locale=current_locale(cfg)), "error")
                return redirect(url_for("models_page"))

            compat = cloud.setdefault("openai_compatible", {})
            compat["enabled"] = request.form.get("compat") == "on"
            compat["url"] = compat_url
            compat["label"] = (request.form.get("compat_label") or "").strip()[:60]

            freetoken = cloud.setdefault("freetoken", {})
            freetoken["enabled"] = request.form.get("freetoken") == "on"
            freetoken["url"] = ft_url
            freetoken["label"] = (request.form.get("freetoken_label") or "").strip()[:60]

        # A setup a role now uses starts on the next Apply; one on Auto needs
        # a card first, as the Ollama card's own save would give it.
        unplaced = _place_new_setups(cfg)
        if unplaced:
            flash(_t_or("admin.models.ollama_unplaced",
                        "Does not fit on any card: {ids}. Saved on the card with most "
                        "room; lower a window or the slots, or remove a setup.",
                        ids=", ".join(unplaced)), "warn")
        save_config(cfg)
        note_pending(services_affected(before, cfg))
        flash(translator("admin.models.saved", locale=current_locale(cfg)), "ok")
        return redirect(url_for("models_page"))

    catalogue = model_catalogue.load_cache(MODELS_CACHE)
    if not catalogue.get("opencode_zen") and not catalogue.get("ollama"):
        # First visit on a fresh install: fetch once rather than showing an
        # empty page that looks broken.
        catalogue = refresh_catalogue(cfg)
    elif model_catalogue.cache_is_stale(catalogue, model_catalogue.STALE_AFTER_S):
        # Started, not waited for. What this page shows is the cache; a refresh
        # changes what the *next* load shows, and "checked at" already says how
        # old this one is. See refresh_in_background().
        refresh_in_background(cfg)

    chosen = ((cfg.get("assistant") or {}).get("models") or {})
    every = {m["id"]: m for m in model_catalogue.all_models(catalogue)}
    image_choices = model_catalogue.image_models(catalogue)
    image_slots = [{"key": key,
                    "label": _t_or(f"admin.models.slot_{key}", spec["label"]),
                    "why": _t_or(f"admin.models.slot_{key}_why", spec["why"]),
                    "current": chosen.get(key, "")}
                   for key, spec in model_catalogue.IMAGE_SLOTS.items()]
    # Which role a sub-agent follows when it names none of its own. Read from
    # the deployer, which is the thing that actually does the inheriting --
    # a second copy here could disagree, and a blank picker showing the wrong
    # inherited model is worse than one showing nothing.
    dep = _deployer()
    inherits = dict(getattr(dep, "SUBAGENT_INHERITS", {}) or {}) if dep else {}

    rows = []
    bench_views = _bench_views(cfg)
    local_by_id = {i["id"]: i for i in _safe_serving(cfg)}
    for persona in model_catalogue.PERSONA_NEEDS:
        advice = model_catalogue.recommend(persona, catalogue)
        raw_current = chosen.get(persona, "")
        # `fallback` may be an ordered chain rather than one name, and a single
        # <select> cannot express one. Rendering its first entry selected would
        # be worse than not rendering it: this form submits every role on every
        # save, so changing the everyday model would silently collapse the
        # chain to whichever entry happened to be showing. The row states the
        # chain instead and its picker is disabled -- a disabled select submits
        # nothing, and the save path skips a persona with no value, so the
        # config is left exactly as the household wrote it.
        chain = ([str(v).strip() for v in raw_current if str(v or "").strip()]
                 if isinstance(raw_current, (list, tuple)) else [])
        current = "" if chain else str(raw_current or "")
        # A local model is one choice in the picker ("Local Ollama"); which
        # setup runs it is the Ollama card's, and the row says which.
        _raw_current = current
        current, runs_on = local_model(current)
        is_local = current.startswith("ollama:")
        local_setup = ""
        if is_local:
            _inst = local_by_id.get(runs_on or "main")
            local_setup = (OI.display(_inst) if _inst and _inst.get("model")
                           else f"{current.split(':', 1)[1]} · {OI.display(_inst)}" if _inst
                           else _raw_current)
        # A blank sub-agent is not unset, it is inherited -- so say what it
        # will actually run. The select itself stays empty: filling it in would
        # turn "follows Everyday" into a pin the next save makes permanent, and
        # a household that changed the parent would find the child had quietly
        # stopped following it.
        parent = inherits.get(persona, "")
        inherited = (chosen.get(parent, "") or "") if (parent and not current) else ""
        # An explicit value on an inheriting role is an override, and the page
        # should say so rather than showing it like any other choice: the two
        # here differ from their parents on purpose, and somebody reading the
        # page should be able to tell that from the page.
        best = (advice["local"] if advice["prefer_local"] and advice["local"]
                else advice["hosted"])
        rows.append({
            "id": persona,
            "label": _t_or(f"admin.models.role_{persona}", advice["label"]),
            "why": _t_or(f"admin.models.role_{persona}_why", advice["why"]),
            # Only the roles that still carry one. The bake-off notes were
            # removed: they named particular models on particular dates, and
            # several of those models are not on offer any more. What is left
            # names no model at all -- it is this role's own traffic, which is
            # what somebody choosing a model for it actually needs.
            "measured": (_t_or(f"admin.models.role_{persona}_measured",
                               advice["measured"]) if advice["measured"] else ""),
            "current": current,
            "is_local": is_local,
            "local_setup": local_setup,
            "local_sid": (runs_on or "main") if is_local else "",
            "needs_vision": bool((model_catalogue.PERSONA_NEEDS.get(persona) or {})
                                 .get("requires", {}).get("vision")),
            # What a blank field will actually run, and whose setting it comes
            # from. Both empty for every role that does not inherit.
            "inherited": inherited,
            # Whenever there is a parent, not only when the field is blank:
            # the override branch names it too.
            "inherits_from": (_t_or(f"admin.models.role_{parent}",
                                    model_catalogue.PERSONA_NEEDS.get(parent, {})
                                    .get("label", parent))
                              if parent else ""),
            # A model that is set but no longer on offer is worth saying out
            # loud: it is how a deploy ends up asking for something the
            # provider retired.
            "current_known": (not current) or is_local or current in every,
            # The entries in order, with whether each is still on offer, so a
            # chain naming a retired model says so rather than looking fine.
            "chain": [{"id": c, "known": local_model(c)[0] in every} for c in chain],
            # The rescue chain, one picker per place (CHAIN_SLOTS).
            "slots": ([{"current": local_model(c)[0], "is_local": local_model(c)[0].startswith("ollama:"),
                        "local_sid": local_model(c)[1] or "main",
                        "known": local_model(c)[0].startswith("ollama:") or local_model(c)[0] in every}
                       for c in (chain or ([str(raw_current)] if raw_current else []))[:CHAIN_SLOTS]]
                      + [{"current": "", "is_local": False, "known": True, "local_sid": ""}] * CHAIN_SLOTS)[:CHAIN_SLOTS]
                     if persona in CHAIN_ROLES else [],
            "recommended": best[0] if best else None,
            "hosted": advice["hosted"],
            "local": advice["local"],
            # What each provider would put here, best two. The flat lists rank
            # on price across all of them at once, which answers "what is
            # cheapest" and hides "what does each of these offer" -- and with
            # four providers configured the cheapest three can all come from
            # one, so a household never learns the provider they already pay
            # for has a model that fits.
            "by_provider": [
                {"source": src,
                 "label": _t_or(f"admin.models.source_{src}", src),
                 "models": group["picks"],
                 # False when the provider publishes nothing to rank on, so
                 # the page can say "price is all this one tells us" rather
                 # than leaving one entry looking like a missing second.
                 "ranked": group["ranked"]}
                for src, group in advice["by_provider"].items()],
            "prefer_local": advice["prefer_local"],
            # Where the role belongs, and why. Carried through because the
            # template asks for it by name: an absent key is Undefined, which
            # is falsy, so the sentence simply never renders and the whole
            # setting reads as a feature nobody switched on.
            "placement": advice["placement"],
            "group": advice["group"],
            "model_type": advice["model_type"],
            "requires": advice["requires"],
        })

    # Needed by the per-role pickers below, so it is read before them rather
    # than beside `checked_at` further down.
    groups = model_groups(cfg)

    # Read, not recomputed: the roster, the prices and the five suggestions all
    # move on the refresh button and `assistant.price_check`, so what somebody
    # compared a minute ago is still there when they come back. A catalogue
    # from before this existed simply has no priced roles, and the page says
    # so rather than guessing.
    priced = catalogue.get("priced_roles") or {}
    # Missing *any* role, not only all of them. The guard below was `if not
    # priced`, which catches a catalogue written before pricing existed and
    # nothing else -- so adding a role to PERSONA_NEEDS left that one role
    # unpriced until the roster next went stale, and an unpriced role renders
    # a select with no options in it at all. Exactly the failure the comment
    # below describes, one picker instead of thirteen, and harder to see for
    # it: twelve rows work and the new one is empty.
    #
    # `heartbeat` landed on 2026-09-09 and did this. The cache had been
    # priced five days earlier, so it held thirteen roles and the fourteenth
    # picker was blank.
    if set(model_catalogue.PERSONA_NEEDS) - set(priced):
        # ...with one exception, and it is the upgrade path. A catalogue
        # written before pricing existed has no `priced_roles`, and nothing
        # re-fetches until the roster is a day old (`_price_watcher` refreshes
        # on age, `models_page` only on an empty cache) -- so every picker
        # below would render with *no options at all*: `row.choices` is empty
        # and the "not on offer" fallback does not fire either, because the
        # configured model is still in the roster. Thirteen empty selects, and
        # no model changeable, until somebody happened to press Refresh.
        #
        # Priced here from the roster already on disk -- no network -- and
        # written back beside it, so this costs one page load once rather than
        # a recomputation per render.
        priced = model_catalogue.price_roles(
            catalogue, str((cfg.get("paths") or {}).get("state") or ""))
        catalogue["priced_roles"] = priced
        catalogue["priced_at"] = catalogue.get("checked_at")
        model_catalogue.save_cache(MODELS_CACHE, catalogue)
    # The standing score for the model each role points at, if it has ever
    # been tested. Keyed by the configured string rather than the bare model
    # id, so `freetoken:x` and `ollama:x` are two answers and not one.
    stored_scores = model_catalogue.load_scores(SCORES_CACHE)
    try:
        # Older runs recorded a local model as "Ollama:x"; the key is the
        # canonical "ollama:x" either way.
        bench_by_model = {re.sub(r"^ollama:", "ollama:", local_model(str(v.get("model") or ""))[0],
                                 flags=re.I): v for v in _bench_views(cfg)}
    except Exception:                                           # noqa: BLE001
        bench_by_model = {}
    for row in rows:
        row["score"] = None
        bench_groups = ROLE_BENCH_GROUPS.get(row["id"])
        bench_view = bench_by_model.get(row["current"]) if bench_groups else None
        if bench_view:
            got = [r for r in bench_view.get("roles") or [] if r["role"] in bench_groups]
            if got:
                passed, total = sum(r["passed"] for r in got), sum(r["total"] for r in got)
                row["score"] = {"score": passed, "max": total, "ok": passed == total,
                                "tok_s": bench_view.get("tok_s_median"), "bench": True}
        elif not bench_groups:
            row["score"] = (stored_scores.get(row["id"]) or {}).get(row["current"])
        row["scorable"] = bool(bench_groups) or row["id"] in model_catalogue.ROLE_PROBES
        advice = priced.get(row["id"]) or {}
        row["assumed"] = bool(advice.get("assumed"))
        row["priced"] = bool(advice)
        models = advice.get("models") or []
        by_provider: dict = {}
        for m in models:
            by_provider.setdefault(m["provider"], []).append(m)
        if row["id"] in DIRECT_ROLES:
            # Offered only what the consumer can call -- see _responses_only_for.
            by_provider = {p: [m for m in ms
                               if not (p == "opencode_zen"
                                       and model_catalogue._wants_responses(m["id"]))]
                           for p, ms in by_provider.items()}
        row["choices"] = [{"label": g["label"], "models": by_provider[g["provider"]],
                           "provider": g["provider"]}
                          for g in groups if by_provider.get(g["provider"])
                          and g["provider"] != "ollama"]
        row["suggestions"] = models[:5]

        # What the picker renders straight away, against what it can reveal.
        #
        # Every role's <select> used to carry the whole roster it can run: 809
        # <option> elements across thirteen roles, 83 for notifications alone.
        # Nothing about that is slow on this side -- the cache loads in 5ms and
        # every recommendation in 5ms more -- it is the browser building a
        # thousand nodes for a page where somebody changes at most one of them.
        #
        # So the shortlist is what a person actually chooses between: what the
        # role runs now, and what this page would put there. The rest is one
        # JSON blob for the whole page, expanded per role on request -- ids and
        # prices rather than markup, and once rather than thirteen times.
        keep = {row["current"]} if row["current"] else set()
        keep |= {sl["current"] for sl in row.get("slots") or [] if sl["current"]}
        keep |= {m["id"] for g in (row.get("by_provider") or [])
                 for m in g["models"]}
        row["shortlist"] = [
            {"label": g["label"],
             "models": [m for m in g["models"] if m["id"] in keep]}
            for g in row["choices"]]
        row["shortlist"] = [g for g in row["shortlist"] if g["models"]]
        row["deferred_count"] = sum(len(g["models"]) for g in row["choices"]) \
            - sum(len(g["models"]) for g in row["shortlist"])

    # Whether the catalogue has been priced at all is a property of the last
    # check, not of any one role, so the card says it once. Thirteen copies of
    # the same sentence is thirteen readings to learn one fact -- and it is
    # the state the page is in until the first check runs, so it was what the
    # page mostly consisted of.
    any_priced = any(r["priced"] for r in rows)

    # The models each role can run that the shortlist does not already show,
    # as data rather than as markup. Built once for the page: the same model
    # appears under several roles, and thirteen copies of an <option> is
    # thirteen copies of its price string too.
    deferred = {
        r["id"]: [[g["label"],
                   [[m["id"], m.get("cost")] for m in g["models"]
                    if m["id"] not in {s["id"]
                                       for sg in r.get("shortlist") or []
                                       for s in sg["models"]}]]
                  for g in r.get("choices") or []]
        for r in rows}
    deferred = {role: [g for g in groups if g[1]]
                for role, groups in deferred.items()}

    # Grouped for reading. `PERSONA_GROUPS` is the declared order; anything
    # whose group is unknown sorts to the end rather than vanishing, because a
    # role missing from the page is worse than one in the wrong section.
    _order = {name: i for i, name in enumerate(model_catalogue.PERSONA_GROUPS)}
    rows.sort(key=lambda r: _order.get(r["group"], len(_order)))

    checked = catalogue.get("checked_at")
    # Which rows of the roster this house is actually paying for. The table
    # below it is the whole catalogue -- 125 rows on this household, four
    # hundred with OpenRouter on -- and the question somebody opens it with is
    # almost always "which of these am I on?", which until now could only be
    # answered by reading the twelve pickers above and then searching the
    # table for each one by hand. Keyed on the configured string because that
    # is what `every` is keyed on: `together:<id>` and a bare OpenCode Zen name
    # are different rows, and the same model served by two providers is two
    # rows that must not both light up.
    in_use: dict[str, list[str]] = {}
    for role, value in chosen.items():
        name = str(value or "").strip()
        if not name:
            continue
        # The roster's "in use by" column, and it names the same roles the
        # card above does -- so it reads from the same catalogue, or a Spanish
        # page listed Spanish personas above and English ones here.
        if role in model_catalogue.PERSONA_NEEDS:
            spec, key = model_catalogue.PERSONA_NEEDS[role], f"admin.models.role_{role}"
        else:
            spec, key = model_catalogue.IMAGE_SLOTS.get(role) or {}, f"admin.models.slot_{role}"
        in_use.setdefault(name, []).append(_t_or(key, spec.get("label", role)))
    # The Code card. Its roster is the flat plan plus the Zen models this key is
    # actually offered, and it is the ONLY picker on this page that may show a
    # Go model -- see models.code_model_choices(). `cloud.opencode.model` is a
    # household setting rather than a role, so it is read and written here
    # rather than through `assistant.models`.
    _oc = (cfg.get("cloud") or {}).get("opencode") or {}
    # No extra filter: fetch_opencode_zen() already trims that roster to what
    # the key is offered, so filtering again here would be a second place to be
    # right about the same thing.
    code_choices = model_catalogue.code_model_choices(catalogue)
    _svc = cfg.get("services") or {}
    _audio = _svc.get("audio-cpp") or {}
    _tts_served = [x.strip() for x in str(_audio.get("tts_packages") or "").split(",")
                   if x.strip()]
    speech = {
        "whisper": str((_svc.get("faster-whisper") or {}).get("model") or "")
                   or WHISPER_DEFAULT,
        "whisper_choices": WHISPER_MODELS,
        "tts": str(_audio.get("tts_model") or "").strip() or (_tts_served[0] if _tts_served else ""),
        "tts_choices": _tts_served,
    }
    _harness = (cfg.get("assistant") or {}).get("harness") or {}
    _live_sources = model_endpoints(cfg)
    return render_template(
        "models.html",
        view=view,
        speech=speech,
        harness={"enabled": bool(_harness.get("enabled")),
                 "allow_go": bool(_harness.get("allow_go"))},
        bench_runs=bench_views,
        ollama=_ollama_card(cfg),
        local_instances=[{"id": i["id"], "display": OI.display(i)}
                         for i in _safe_serving(cfg)],
        local_setups=_picker_setups(cfg),
        **_bench_kind_args(),
        bench_roles=model_bench.ROLES,
        bench_counts=model_bench.case_counts(ROOT / "services" / "nanobot" / "bench" / "cases.json"),
        bench_queue=[m for e in _bench_queue() for m in e.get("models") or []],
        bench_containers=bench_containers(),
        bench_efforts=model_bench.EFFORTS,
        code_model=str(_oc.get("model") or ""),
        code_choices=code_choices,
        code_enabled=bool(_oc.get("enabled")),
        rows=rows,
        any_priced=any_priced,
        deferred_models=deferred,
        image_slots=image_slots,
        image_choices=image_choices,
        in_use=in_use,
        options=sorted(every.values(), key=lambda m: (m["provider"], m["id"])),
        model_groups=groups,
        provider_labels={g["provider"]: g["label"] for g in groups},
        sources=_source_settings(cfg),
        roles=list((cfg.get("hosts") or {}).keys()),
        checked_at=(time.strftime("%Y-%m-%d %H:%M", time.localtime(checked))
                    if checked else ""),
        price_check=((cfg.get("assistant") or {}).get("price_check") or "daily"),
        # Every source that failed, named. One combined "could not refresh"
        # left a household guessing which of four it was. Only sources still
        # switched on: an error cached while FreeToken was on went on saying
        # it could not be reached after the household turned it off.
        errors=[e for e in (catalogue.get(f"{s}_error")
                            for s in model_catalogue.SOURCES
                            if s == "opencode_zen" or s in _live_sources) if e],
    )


@app.route("/services", methods=["GET", "POST"])
def services():
    cfg = load_config()
    manifest = load_manifest()

    if request.method == "POST":
        before = load_config()
        action = request.form.get("action", "toggle")

        if action == "add_custom":
            name = request.form.get("name", "").strip()
            url = request.form.get("url", "").strip()
            if name and url:
                # Exactly the fields the dashboard draws. It collected
                # `health_url` before, for a status dot the portal no longer
                # paints -- a field somebody fills in that reaches nothing is
                # worse than one that is not offered.
                entry = {"name": name, "url": url}
                for field in ("description", "icon"):
                    value = request.form.get(field, "").strip()
                    if value:
                        entry[field] = value
                if request.form.get("lan_only"):
                    entry["lan_only"] = True
                cfg.setdefault("custom_services", [])
                cfg["custom_services"] = list(cfg.get("custom_services") or []) + [entry]
        elif action == "add_plugin":
            entry = request.form.get("plugin", "").strip()
            why = check_plugin(cfg, entry)
            if why:
                flash(why, "error")
            else:
                cfg["plugins"] = list(cfg.get("plugins") or []) + [entry]
                # A plugin outside `paths.plugins` is mounted into this page's
                # container by the admin unit's generated overlay, which only
                # exists after the next deploy of it. Until then the deployer
                # on the host can see the plugin and this page cannot -- so say
                # that here rather than let the next screen look broken.
                root = (cfg.get("paths") or {}).get("plugins") or ""
                # `is_relative_to`, not a string prefix: `.../plugins-old` is
                # not inside `.../plugins`, and a prefix test says it is -- so
                # the one case this notice exists for is the one it stayed
                # silent on. Same containment test `compose_admin.py` uses to
                # decide whether to mount it, so the notice and the mount can
                # never disagree.
                if root and not Path(entry).resolve().is_relative_to(
                        Path(root).resolve()):
                    flash(t("admin.plugins.deploy_admin"), "ok")
                note_pending({"admin"})
        elif action == "edit_plugin":
            was = request.form.get("plugin", "").strip()
            entry = request.form.get("entry", "").strip()
            existing = [str(e).strip() for e in (cfg.get("plugins") or [])]
            if was not in existing:
                # The row this form was rendered from is gone -- the other
                # half of a browser tab left open while somebody removed the
                # plugin, or two people on the page at once. Writing the new
                # value anyway would re-add a plugin that was deliberately
                # taken out, so this says so instead.
                flash(t("admin.plugins.err_vanished"), "error")
            elif entry == was:
                pass                      # nothing typed; not worth a message
            else:
                why = check_plugin(cfg, entry, replacing=was)
                if why:
                    flash(why, "error")
                else:
                    # In place, not append-after-remove: `plugins:` is ordered
                    # and the deployer reads it in order, so a household that
                    # arranged three plugins deliberately should not find one
                    # of them at the bottom because its path was corrected.
                    cfg["plugins"] = [entry if str(e).strip() == was else e
                                      for e in (cfg.get("plugins") or [])]
                    # Same notice the add path gives, for the same reason: a
                    # plugin outside `paths.plugins` is only visible to this
                    # page once the admin unit's generated overlay mounts it,
                    # and an *edited* path can move a plugin across that line
                    # in either direction.
                    root = (cfg.get("paths") or {}).get("plugins") or ""
                    if root and not Path(entry).resolve().is_relative_to(
                            Path(root).resolve()):
                        flash(t("admin.plugins.deploy_admin"), "ok")
                    note_pending({"admin"})
        elif action == "remove_plugin":
            victim = request.form.get("plugin", "")
            cfg["plugins"] = [e for e in (cfg.get("plugins") or [])
                              if str(e).strip() != victim]
            # The directory is left exactly where it is. Removing a plugin from
            # this list is "stop deploying it", not "delete somebody's project".
            note_pending({"admin"})
        elif action == "remove_custom":
            victim = request.form.get("name", "")
            cfg["custom_services"] = [
                c for c in (cfg.get("custom_services") or [])
                if c.get("name") != victim
            ]
        else:
            cfg.setdefault("services", {})
            # Only the services *this form* rendered. The included services and
            # the plugin ones are two cards and therefore two forms, and an
            # unchecked box sends nothing -- so a loop over every known service
            # would read "absent" as "switch it off" and quietly disable every
            # plugin service each time somebody saved the other card. Each row
            # carries a `scope:` marker saying it was on screen.
            governed = {k.split(":", 1)[1] for k in request.form
                        if k.startswith("scope:")}
            for name in all_services_merged(cfg):
                if governed and name not in governed:
                    continue
                cfg["services"].setdefault(name, {})
                # The admin page cannot disable itself -- ever. Its checkbox is
                # rendered disabled (and a disabled control never submits), but
                # this is the enforcement, not the checkbox: a crafted POST or a
                # hand-edited form must not be able to turn off the only way to
                # turn anything back on.
                if name == "admin":
                    cfg["services"][name]["enabled"] = True
                else:
                    cfg["services"][name]["enabled"] = (
                        request.form.get(f"enabled:{name}") == "on")
                    # Only for the services it can mean something for -- see
                    # `house_only_applies` above. A checkbox that silently does
                    # nothing is worse than one that is not offered.
                    if name in house_only_capable():
                        cfg["services"][name]["house_only"] = (
                            request.form.get(f"house_only:{name}") == "on")
                # Every port key the service has is editable in place -- plus
                # any the form offered that the config does not hold yet.
                #
                # The union is what makes this work for a plugin service. Their
                # config entry is usually nothing but `enabled`, so iterating
                # the config alone would render an input and then silently drop
                # whatever was typed into it: the first save of a port would do
                # nothing while looking like it worked, which is the failure
                # this page exists to avoid.
                offered = {k[len(f"port:{name}:"):] for k in request.form
                           if k.startswith(f"port:{name}:")}
                for key in dict.fromkeys(list(cfg["services"][name]) + sorted(offered)):
                    if not (key == "port" or key.endswith("_port")
                            or key.endswith("port_base")):
                        continue
                    raw = request.form.get(f"port:{name}:{key}", "").strip()
                    if raw.isdigit() and 1 <= int(raw) <= 65535:
                        cfg["services"][name][key] = int(raw)


        save_config(cfg)
        # Custom tiles are written into the portal's dashboard file, and only
        # the portal reads it. This named `homepage` while the dashboard was
        # gethomepage; it is home-core's own page now, and pointing a household
        # at a service that no longer exists is a Deploy button that fails.
        note_pending(["home-core"] if action in ("add_custom", "remove_custom")
                     else services_affected(before, cfg))
        flash("saved", "ok")
        return redirect(url_for("services"))

    rows = []
    # Once per render, not once per row: reading it loads the deployer.
    _house_only_names = house_only_capable()
    for name, spec in manifest.get("services", {}).items():
        svc = (cfg.get("services") or {}).get(name) or {}
        port = next(
            (svc[k] for k in svc if k.endswith("port") or k == "port"), None
        )
        rows.append({
            "name": name,
            "description": spec.get("description", ""),
            "host": spec.get("role", svc.get("host", "?")),
            "ports": {k: v for k, v in svc.items()
                      if (k == "port" or k.endswith("_port")
                          or k.endswith("port_base")) and isinstance(v, int)},
            "enabled": True if name == "admin" else svc.get("enabled", True),
            "locked": name == "admin",
            # Whether "house only" means anything for this service. It is not a
            # free-for-all: the switch works by keeping a *prefix* off the public
            # copy of the proxy, so it only applies to a service the portal
            # actually serves. Everything else is linked by address and port and
            # is house-only whatever anybody ticks, because the VPS forwards the
            # portal and nothing else.
            "house_only_applies": name in _house_only_names,
            "house_only": svc.get("house_only", True),
        })
    return render_template("services.html", rows=rows,
                           custom=household_services(cfg),
                           plugins=plugins_view(cfg),
                           plugin_services=plugin_service_rows(cfg))


@app.route("/voice/profile", methods=["POST"])
def voice_profile():
    """Measure one package and hand the numbers back, without redrawing."""
    cfg = load_config()
    package = request.form.get("package", "").strip()
    rows, _err = audiocpp_catalogue()
    known = {r["id"]: r for r in rows}
    if package not in known or not known[package]["installed"]:
        return jsonify({"error": "not installed"}), 400
    if known[package]["task"] != "tts":
        # An ASR package cannot be asked to speak. Profiling one would need a
        # clip to transcribe and a reference transcript to score it against,
        # which is a different measurement and not this button.
        return jsonify({"error": "tts only"}), 400
    row, why = audiocpp_profile(package, cfg)
    if why:
        return jsonify({"error": why}), 502
    return jsonify({"ok": True, **row})


@app.route("/voice/remove", methods=["POST"])
def voice_remove():
    """Delete one package's files and say so, without redrawing the page.

    The form-post version reloaded, which threw away the filter, the sort and
    the scroll position -- so removing three models meant finding your place
    three times. The row goes from the table instead.

    Same rules as the form path, because a JSON door is still a door: only a
    package this household serves, and never the last of its kind.
    """
    cfg = load_config()
    package = request.form.get("package", "").strip()
    svc = cfg.setdefault("services", {}).setdefault("audio-cpp", {})
    for key in ("tts_packages", "asr_packages"):
        listed = [x.strip() for x in str(svc.get(key) or "").split(",") if x.strip()]
        if listed == [package]:
            return jsonify({"error": translator(
                "admin.voice.remove_last", locale=current_locale(cfg),
                id=package)}), 400
    before = load_config()
    # What it held, recorded before the files go: the size column reads the
    # files, and "how big was the thing I just deleted" is the question people
    # ask afterwards.
    rows, _err = audiocpp_catalogue()
    held = next((r["bytes"] for r in rows if r["id"] == package), 0)
    for key in ("tts_packages", "asr_packages"):
        listed = [x.strip() for x in str(svc.get(key) or "").split(",")
                  if x.strip() and x.strip() != package]
        svc[key] = ",".join(listed)
    why = audiocpp_uninstall(package)
    if not why and held:
        remember_disk(package, round(held / 1048576))
    if why:
        return jsonify({"error": why}), 502
    save_config(cfg)
    note_pending(services_affected(before, cfg) | {"audio-cpp"})
    audiocpp_catalogue(force=True)
    return jsonify({"ok": True, "package": package})


@app.route("/voice/styles")
def voice_styles():
    """The voices inside one package, for the picker to follow the package.

    The voice list is rendered for the package that was *saved*, so changing
    the package select on the page left the voices behind until somebody saved
    and reloaded -- a combo box that lies about what it is offering. This is
    what the browser asks when the package changes.

    Only a package this household serves: `audiocpp_voice_styles` unpacks a
    GGUF to read it, and doing that for an arbitrary name from a query string
    is a way to make this page slow from outside.
    """
    cfg = load_config()
    package = request.args.get("package", "").strip()
    served = [x.strip() for x in
              str(((cfg.get("services") or {}).get("audio-cpp") or {})
                  .get("tts_packages") or "").split(",") if x.strip()]
    if package not in served:
        return jsonify({"styles": []}), 400
    return jsonify({"styles": audiocpp_voice_styles(package)})


@app.route("/voice/preview", methods=["POST"])
def voice_preview():
    """Speak a line in the chosen voice and hand back the audio.

    Through the **gateway**, not the engine: a preview that bypassed it would
    prove the model can talk and not that the house can. What is being tested
    is the whole path -- the engine that was picked, the voice within it, and
    the fallback underneath if that engine is down. A preview that quietly
    came back in piper is a true answer to "what will I hear".

    Returns the WAV straight to the browser. Nothing is written and nothing is
    saved: pressing this must not be a way to change the house voice by
    accident.
    """
    cfg = load_config()
    engine = request.form.get("engine", "").strip()
    voice = request.form.get("voice", "").strip()
    offered = {e["id"]: e for e in tts_choices(cfg) if e["available"]}
    if engine not in offered:
        return jsonify({"error": "engine"}), 400
    # Resolve the package *first*, and validate the voice against the package
    # that will actually be used.
    #
    # Two bugs lived in doing this the other way round. The allow-list was
    # built from the raw form field, so an arbitrary name from a request
    # reached `audiocpp_voice_styles()` -- which docker-execs into the
    # container and unpacks a GGUF to answer, which is a way to make this page
    # slow from outside. Its sibling `/voice/styles` already refuses a name
    # this household does not serve, and says why.
    #
    # And the two halves disagreed: a voice was checked against the *asked
    # for* package while the request below fell back to the *configured* one
    # when that package was not served, so the gateway could be handed a style
    # belonging to a model it was not going to load.
    served = [x.strip() for x in
              str(((cfg.get("services") or {}).get("audio-cpp") or {})
                  .get("tts_packages") or "").split(",") if x.strip()]
    asked_model = request.form.get("model", "").strip()
    model = asked_model if asked_model in served else str(
        offered[engine].get("model") or "")
    # Against the package being auditioned rather than the saved one, or
    # previewing another package could only ever use its default voice.
    allowed = {v["id"] for v in offered[engine]["voices"]}
    if engine == "audiocpp" and model:
        allowed = {""} | set(audiocpp_voice_styles(model))
    if voice and voice not in allowed:
        return jsonify({"error": "voice"}), 400

    token = get_secret("VOICE_GATEWAY_TOKEN")
    port = ((cfg.get("services") or {}).get("home-voice") or {}).get("port", 21011)
    role = str(((cfg.get("services") or {}).get("home-voice") or {})
               .get("host") or "compute")
    address = str((((cfg.get("hosts") or {}).get(role)) or {})
                  .get("address") or "127.0.0.1")
    # `model` was resolved above, before the voice was checked against it --
    # a name from a form is not a reason to make the container load an
    # arbitrary file, and the check and the request must agree on which
    # package they mean.
    body = json.dumps({
        "text": request.form.get("text", "").strip() or PREVIEW_LINE,
        "engine": engine, "voice": voice or None,
        "model": model or None}).encode()

    def speak():
        req = urllib.request.Request(
            f"http://{address}:{port}/v1/tts", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return (r.read(), r.headers.get("X-Engine", engine),
                    r.headers.get("X-Synth-Seconds", ""),
                    r.headers.get("X-Audio-Seconds", ""))

    # Was the model in memory before this? `lazy_load` means the first call
    # after a start pays for loading it, and that is a real number somebody
    # waits for -- but only the first person to speak. Asking rather than
    # assuming is what makes it honest to call the figure cold: press Listen
    # twice and the second run's first call is warm too.
    was_cold = (engine == "audiocpp" and model
                and audiocpp_loaded(model, audiocpp_base(cfg)) is False)
    try:
        # Twice, always. The first is cold or not depending on the answer
        # above; the second is warm by construction, and warm is the number a
        # voice should be judged on because it is what everybody after the
        # first person waits.
        audio, spoke, synth, audio_s = speak()
        cold_s = float(synth) if (was_cold and synth) else None
        audio, spoke, synth, audio_s = speak()
    except urllib.error.HTTPError as exc:
        return jsonify({"error": exc.read().decode(errors="replace")[:200]}), 502
    except Exception as exc:                                      # noqa: BLE001
        return jsonify({"error": str(exc)[:200]}), 502
    # Keep the number, but only when the engine that answered is the one that
    # was asked: a fallback measured piper, and filing piper's speed under an
    # audio.cpp package would be a fast number beside the wrong name.
    if spoke == engine == "audiocpp" and synth and model:
        remember_timing(model, voice, {"synth": synth, "audio": audio_s}, cold_s,
                        audiocpp_backend(cfg) or "cpu")

    # `X-Engine` is what actually spoke. If the chosen engine was down the
    # gateway fell back, and the person listening has to be told that rather
    # than left thinking they just heard their choice.
    return Response(audio, mimetype="audio/wav",
                    headers={"X-Engine": spoke,
                             "X-Synth-Seconds": synth,
                             "X-Audio-Seconds": audio_s,
                             "X-Cold-Seconds": ("" if cold_s is None
                                                else f"{cold_s:.2f}")})


@app.route("/voice", methods=["GET", "POST"])
def voice_page():
    """Alfred's voice, and the models the speech runtime has on disk.

    Separate from the Models page on purpose: that one is about which *LLM*
    answers as which persona, priced per token and enumerated from an account.
    These are files on this machine, and the questions are different -- what is
    installed, what is served, what can be deleted to get the disk back.
    """
    cfg = load_config()
    if request.method == "POST":
        before = load_config()
        action = request.form.get("action", "save")

        if action == "remove":
            package = request.form.get("package", "").strip()
            # Unlisted first, then deleted. The other order leaves a config
            # naming a package that is gone, and the next deploy downloads it
            # again -- a remove button that does not remove is worse than none.
            svc = cfg.setdefault("services", {}).setdefault("audio-cpp", {})
            # Never the last one of a kind. A runtime with no TTS or no ASR is
            # a container that starts and refuses half its requests, and the
            # button that did it was labelled "get the disk back". This
            # happened: removing the only ASR package emptied the key and left
            # the service unable to deploy at all.
            emptied = [key for key in ("tts_packages", "asr_packages")
                       if [x.strip() for x in str(svc.get(key) or "").split(",")
                           if x.strip()] == [package]]
            if emptied:
                flash(translator("admin.voice.remove_last",
                                 locale=current_locale(cfg), id=package), "error")
                return redirect(url_for("voice_page"))
            for key in ("tts_packages", "asr_packages"):
                listed = [x.strip() for x in str(svc.get(key) or "").split(",")
                          if x.strip() and x.strip() != package]
                svc[key] = ",".join(listed)
            why = audiocpp_uninstall(package)
            if why:
                flash(translator("admin.voice.remove_error",
                                 locale=current_locale(cfg), detail=why), "error")
            else:
                save_config(cfg)
                note_pending(services_affected(before, cfg) | {"audio-cpp"})
                flash(translator("admin.voice.removed",
                                 locale=current_locale(cfg), id=package), "ok")
            audiocpp_catalogue(force=True)
            return redirect(url_for("voice_page"))

        # --- the house voice ------------------------------------------------
        voice_cfg = cfg.setdefault("services", {}).setdefault("home-voice", {})
        chosen = request.form.get("tts_engine", "").strip()
        offered = {e["id"]: e for e in tts_choices(cfg) if e["available"]}
        if chosen in offered:
            voice_cfg["tts_engine"] = chosen
            want = request.form.get("tts_voice", "").strip()
            voice_cfg["tts_voice"] = (
                want if want in {v["id"] for v in offered[chosen]["voices"]} else "")

        # --- how audio.cpp runs ---------------------------------------------
        svc = cfg.setdefault("services", {}).setdefault("audio-cpp", {})
        # Which package speaks. Only one it actually serves: a model it never
        # installed exports a name the container cannot load, and the first
        # request 404s.
        model = request.form.get("tts_model", "").strip()
        served = [x.strip() for x in str(svc.get("tts_packages") or "").split(",")
                  if x.strip()]
        if model in served:
            svc["tts_model"] = model
        backend = request.form.get("backend", "").strip()
        if backend in ("cpu", "cuda"):
            svc["backend"] = backend
        svc["gpu"] = bool(request.form.get("gpu"))
        try:
            threads = int(request.form.get("threads", "") or 0)
        except ValueError:
            threads = 0
        if 1 <= threads <= 64:
            svc["threads"] = threads
        # `backend: cuda` without the card reservation starts and then cannot
        # see a device, which reads as a broken model rather than a setting
        # half-made. Said here rather than left to be discovered in a log.
        if svc.get("backend") == "cuda" and not svc.get("gpu"):
            flash(translator("admin.voice.cuda_needs_gpu",
                             locale=current_locale(cfg)), "error")

        # --- which packages it serves ---------------------------------------
        rows, _err = audiocpp_catalogue()
        known = {r["id"]: r for r in rows}
        for key, task in (("tts_packages", "tts"), ("asr_packages", "asr")):
            picked = [x for x in request.form.getlist(key)
                      if x in known and known[x]["task"] == task]
            if picked:
                svc[key] = ",".join(picked)
            # An empty list is not saved. Serving no TTS at all is a container
            # that starts and refuses every request, and an empty checkbox
            # column is far more likely to be a mis-click than an intention.
            elif key in svc and not svc.get(key):
                svc.pop(key)

        # Anything installed and no longer served loses its files. Keeping a
        # package the runtime is not told to load is paying gigabytes for a
        # menu entry -- and unticking it is the household saying it does not
        # want it. Said in the flash, with the size, because a save that
        # quietly deleted 2 GB would be a nasty surprise.
        served = set()
        for key in ("tts_packages", "asr_packages"):
            served |= {x.strip() for x in str(svc.get(key) or "").split(",")
                       if x.strip()}
        freed, pruned = 0, []
        for row in audiocpp_catalogue()[0]:
            if not row["installed"] or row["id"] in served:
                continue
            if audiocpp_uninstall(row["id"]):
                continue
            remember_disk(row["id"], round(row["bytes"] / 1048576))
            freed += row["bytes"]
            pruned.append(row["id"])

        save_config(cfg)
        note_pending(services_affected(before, cfg))
        if pruned:
            flash(translator("admin.voice.pruned", locale=current_locale(cfg),
                             n=len(pruned), mb=round(freed / 1048576)), "ok")
            audiocpp_catalogue(force=True)
        flash("saved", "ok")
        return redirect(url_for("voice_page"))

    rows, cat_error = audiocpp_catalogue()
    svc = (cfg.get("services") or {}).get("audio-cpp") or {}
    serving = set()
    for key in ("tts_packages", "asr_packages"):
        serving |= {x.strip() for x in str(svc.get(key) or "").split(",") if x.strip()}
    return render_template(
        "voice.html",
        page="voice_page",
        tts_engines=tts_choices(cfg),
        tts_engine=str(((cfg.get("services") or {}).get("home-voice") or {})
                       .get("tts_engine") or "piper"),
        tts_voice=str(((cfg.get("services") or {}).get("home-voice") or {})
                      .get("tts_voice") or ""),
        audio=svc,
        # What the container is running, against what the config asks for.
        # They differ between saving and deploying, and the numbers in the
        # table come from whichever is actually running.
        running_backend=audiocpp_backend(cfg),
        models=rows,
        serving=serving,
        catalogue_error=cat_error,
        voices_dir=os.path.join(
            str((cfg.get("paths") or {}).get("config") or ""), "home-voice", "voices"),
    )


@app.route("/site", methods=["GET", "POST"])
def site():
    cfg = load_config()
    if request.method == "POST":
        before = load_config()
        cfg.setdefault("site", {})
        cfg["site"]["name"] = request.form.get("name", "").strip() or "Home"
        # Blank keeps whatever is configured. It used to fall back to a
        # literal suffix, so saving this form with the field empty
        # silently renamed a household's domain to a suffix it does not
        # use -- and `dns:` and every certificate name are derived from
        # it. There is no sensible invented value for this one: the
        # setting is the household's, so the only safe default is the
        # one already on disk.
        cfg["site"]["domain"] = (request.form.get("domain", "").strip()
                                 or (before.get("site") or {}).get("domain", ""))
        cfg["site"]["timezone"] = request.form.get("timezone", "").strip() or "UTC"
        # What the assistant calls itself. Applied to its prompts and to the
        # pages that mention it on the next deploy -- so this needs the deploy,
        # which `services_affected` works out from the path below.
        cfg["site"]["assistant_name"] = (
            request.form.get("assistant_name", "").strip() or "Alfred")

        cfg.setdefault("locale", {})
        chosen = request.form.getlist("available")
        # Only locales with a catalogue, and the default has to be one of them:
        # otherwise the portal falls back on every page and the setting looks
        # broken rather than unset.
        available = [c for c in chosen if c in translator.available] or ["en"]
        default = request.form.get("default_locale", "en")
        cfg["locale"]["available"] = available
        cfg["locale"]["default"] = default if default in available else available[0]

        # Hosts, by role. Blank leaves the role where it was.
        cfg.setdefault("hosts", {})
        for role in ("hub", "compute", "storage"):
            cfg["hosts"].setdefault(role, {})
            address = request.form.get(f"host:{role}", "").strip()
            user = request.form.get(f"host_user:{role}", "").strip()
            if address:
                cfg["hosts"][role]["address"] = address
            if user:
                cfg["hosts"][role]["user"] = user

        # The names people type. Empty removes the name; nothing depends on it.
        cfg.setdefault("dns", {})
        for alias in list(cfg["dns"]) + [a for a in request.form
                                         if a.startswith("dns:")]:
            key = alias[4:] if alias.startswith("dns:") else alias
            if f"dns:{key}" in request.form:
                value = request.form.get(f"dns:{key}", "").strip()
                if value:
                    cfg["dns"][key] = value
                elif key in cfg["dns"]:
                    del cfg["dns"][key]

        # Where state lives on the targets. All five `paths:` names, not the
        # three this used to offer: `backups:` is where ./home-stack backup
        # writes and `plugins:` is where a household's own services live, and a
        # path you cannot see is one you cannot move off a filling disk.
        cfg.setdefault("paths", {})
        # What the role boxes below were rendered with. They are pre-filled
        # with each machine's effective path, which is the base value wherever
        # there is no override -- so after the base loop below changes one,
        # every untouched role box is holding the *old* base and looks exactly
        # like four machines that disagree with it.
        was = {kind: cfg["paths"].get(kind) for kind in PATH_KINDS}
        had = {role: dict(over or {}) for role, over
               in ((cfg["paths"].get("by_host") or {}).items())}
        for kind in PATH_KINDS:
            value = request.form.get(f"path:{kind}", "").strip()
            # Absolute only. These are read on the target host, where this
            # process's working directory means nothing. Said out loud rather
            # than dropped: a box that keeps the text you typed and saves the
            # old value is the worst of the three outcomes.
            if not value:
                continue
            if not value.startswith("/"):
                flash(t("admin.site.paths_absolute"), "error")
                continue
            cfg["paths"][kind] = value

        # One machine at a time, chosen with the combo above the fields.
        #
        # `all` is the base set every machine uses unless it says otherwise;
        # each role is that machine's own answer. All of them post -- the combo
        # only decides which is on screen -- so switching it never loses an
        # edit somebody made on another machine's tab.
        #
        # A value equal to the base is not an override, it is the absence of
        # one: that is how a machine is put back, and it keeps `by_host` down
        # to the machines that genuinely differ instead of four copies of the
        # same five paths.
        overrides = {}
        for role in PATH_ROLES:
            for kind in PATH_KINDS:
                value = request.form.get(f"path:{role}:{kind}", "").strip()
                if not value:
                    continue
                if not value.startswith("/"):
                    flash(t("admin.site.paths_absolute"), "error")
                    continue
                if value == cfg["paths"].get(kind):
                    continue
                # A box nobody touched still holds the base value as it was
                # *before* this save, and every role box posts whether or not
                # it is on screen. Without this, changing the shared `state`
                # box wrote four overrides pinning all four machines to the
                # path being moved away from -- the deploy then bind-mounted
                # the directory migrate_paths had just emptied, and the page
                # reported the change saved.
                if value == was.get(kind) and kind not in had.get(role, {}):
                    continue
                overrides.setdefault(role, {})[kind] = value
        if overrides:
            cfg["paths"]["by_host"] = overrides
        else:
            cfg["paths"].pop("by_host", None)

        cfg.setdefault("cloud", {})
        vps = cfg["cloud"].setdefault("vps", {})
        vps["enabled"] = request.form.get("vps") == "on"
        # A separate question from `enabled`. Deploying that machine means
        # rsyncing a tree and running compose there, which needs an account
        # with a shell and the docker socket -- a VPS set up by hand often has
        # only a `nologin` tunnel account. Unticked, the proxy stays configured
        # and this stack stops trying, which beats a deploy that is red for a
        # reason somebody has already accepted.
        vps["managed"] = request.form.get("vps_managed") == "on"
        vps["host"] = request.form.get("vps_host", vps.get("host", "")).strip()
        vps["user"] = request.form.get("vps_user", vps.get("user", "")).strip()
        try:
            vps["tunnel_port"] = int(request.form.get("vps_tunnel_port")
                                     or vps.get("tunnel_port") or 21100)
        except ValueError:
            pass
        vps["serve_entry_page"] = request.form.get("vps_entry_page") == "on"

        notifications = cfg["cloud"].setdefault("notifications", {})
        notifications["mode"] = request.form.get("notifications", "local")
        notifications["external_url"] = request.form.get(
            "notifications_url", notifications.get("external_url", "")).strip()

        certificates = cfg["cloud"].setdefault("certificates", {})
        certificates["mode"] = request.form.get("certificates", "local-ca")
        certificates["acme_dns_provider"] = request.form.get(
            "acme_provider", certificates.get("acme_dns_provider", "")).strip()

        save_config(cfg)
        note_pending(services_affected(before, cfg))
        flash("saved", "ok")
        return redirect(url_for("site"))

    # Normalised before rendering: the config is documented as hand-editable,
    # and a missing or empty block must degrade to defaults on this page of all
    # pages -- a config without `locale:` used to 500 the exact page you would
    # use to set it.
    view = {
        # "Home" and "UTC" are safe things to invent; a domain is not.
        # Showing one in the form is how a household that never set it
        # ends up saving somebody else's suffix into its own config.
        "site": {**{"name": "Home", "domain": "", "timezone": "UTC"},
                 **(cfg.get("site") or {})},
        "locale": {**{"default": "en", "available": []},
                   **(cfg.get("locale") or {})},
        "hosts": {role: {**{"address": "127.0.0.1", "user": ""},
                         **((cfg.get("hosts") or {}).get(role) or {})}
                  for role in ("hub", "compute", "storage")},
        "dns": dict((cfg.get("dns") or {})),
        "dns_targets": dns_targets(cfg),
        "paths": {**{k: f"/var/lib/home-stack/{k}" for k in PATH_KINDS},
                  **{k: v for k, v in (cfg.get("paths") or {}).items()
                     if k in PATH_KINDS}},
        # What each machine will actually use: its own answer where it has
        # one, the shared set otherwise. Every box is filled in, so a machine's
        # tab reads as where its data goes rather than as a blank meaning
        # "somewhere else on this page".
        "paths_by_host": {
            role: {kind: (((cfg.get("paths") or {}).get("by_host") or {})
                          .get(role, {}).get(kind)
                          or (cfg.get("paths") or {}).get(kind)
                          or f"/var/lib/home-stack/{kind}")
                   for kind in PATH_KINDS}
            for role in PATH_ROLES},
        # Which machines genuinely differ, so the combo can say so without
        # somebody having to click through four of them to find out.
        "paths_differ": sorted(
            r for r, k in (((cfg.get("paths") or {}).get("by_host") or {})).items()
            if k and r in PATH_ROLES),
        "cloud": {
            "vps": {**{"enabled": False, "host": "", "user": "",
                       "managed": True,
                       "tunnel_port": 21100, "serve_entry_page": False},
                    **((cfg.get("cloud") or {}).get("vps") or {})},
            "notifications": {**{"mode": "local", "external_url": ""},
                              **((cfg.get("cloud") or {}).get("notifications") or {})},
            "certificates": {**{"mode": "local-ca", "acme_dns_provider": ""},
                             **((cfg.get("cloud") or {}).get("certificates") or {})},
        },
    }
    return render_template(
        "site.html",
        cfg=view,
        path_kinds=PATH_KINDS,
        path_roles=PATH_ROLES,
        timezones=timezone_choices(),
        all_locales=[
            {"code": c, "name": translator.name_of(c)} for c in translator.available
        ],
    )


@app.route("/members", methods=["GET", "POST"])
def members():
    cfg = load_config()

    if request.method == "POST":
        before = load_config()
        action = request.form.get("action")
        people = cfg.get("members", []) or []

        _ratchet_member_seq(cfg)

        if action == "add":
            # An opaque generated id, never anything a person carries in a
            # wallet. The stack this came from used national identity numbers
            # as login names; that is the one thing not to reproduce.
            #
            # Monotonic, and ids are never reused. Filling the lowest free slot
            # handed a new person the departed member's id -- and with it their
            # still-valid API secret, their derived task token (a pure function
            # of the shared secret and the id), their assistant state directory
            # and their backups. A retired id stays retired.
            seq = int(cfg["next_member_seq"]) + 1
            new_id = f"user{seq}"
            new_name = request.form.get("display_name", "").strip() or f"Member {seq}"
            # Before the counter moves, so a refused name does not retire an id.
            clash = folder_clash(cfg, new_id, new_name)
            if clash:
                flash(translator("admin.members.folder_clash",
                                 locale=current_locale(cfg), name=clash), "error")
                return redirect(url_for("members"))
            cfg["next_member_seq"] = seq
            people.append({
                "id": new_id,
                "display_name": new_name,
                "admin": False,
                "active": True,
                "locale": (cfg.get("locale") or {}).get("default", "en"),
            })
            # The member's own credentials, minted here. This page is the only
            # place members are managed, so it is also the only place their
            # secrets come into existence -- install.sh seeds the first members
            # on a fresh install and never touches them again.
            for key, kind in member_keys(new_id):
                if kind == "generated" and not secret_keys_present().get(key):
                    set_secret(key, generate_secret(key))
            # Their Paperless token too, so a registered person arrives able to
            # search their own documents rather than able to once somebody
            # remembers to press a button. It is the one member key this stack
            # cannot mint itself — Paperless issues it — so it is attempted
            # here and never allowed to fail the registration: a document
            # archive that is not deployed yet, or not running, is the ordinary
            # case on a fresh install. The per-member button stays for exactly
            # that retry.
            token, err = issue_paperless_token(cfg, new_id)
            if token:
                key = f"PAPERLESS_API_TOKEN_{member_env_suffix(new_id)}"
                set_secret(key, token)
                note_pending(secret_impact(key))
            else:
                flash(translator("admin.members.paperless_deferred",
                                 locale=current_locale(cfg), id=new_id,
                                 detail=err), "warn")
        # `add` is the only thing this page still does. Editing somebody,
        # switching them off, minting their codes and removing them all live on
        # that person's own page: doing it from a row meant five hidden forms
        # per member and four buttons in the last column, and every
        # irreversible action one misclick from the row above it.

        cfg["members"] = people
        # One assistant instance per *active* member. The two lists have to
        # agree, or the deployer allocates ports and credentials for people who
        # are not here -- and a deactivated member keeps neither.
        cfg.setdefault("services", {}).setdefault("nanobot", {})["members"] = [
            p["id"] for p in people if p.get("active", True)
        ]
        save_config(cfg)
        note_pending(services_affected(before, cfg))
        flash("saved", "ok")
        return redirect(url_for("members"))

    return render_template(
        "members.html",
        people=cfg.get("members", []) or [],
        # The list shows a language rather than offering one, so it needs the
        # names and not the options.
        locale_names={c: translator.name_of(c) for c in translator.available},
    )


# ---------------------------------------------------------------------------
# Reaching the other machines
# ---------------------------------------------------------------------------
# Everything here is about `{paths.config}/ssh`, which is this stack's own ssh
# home -- not the deploying account's. Two identities exist on purpose: a
# deploy run from a shell uses ~/.ssh, and one run from this page uses that
# directory, because the container has no business reading somebody's personal
# keys. The cost is that they can disagree, and until now nothing showed you
# either of them.
#
# It stayed empty because nothing could write it. A split install therefore
# worked from a terminal and not from here, and the failure was an ssh error
# inside a deploy log.
SSH_DIR = os.environ.get("SSH_KEY_DIR") or os.path.join(
    os.environ.get("HOME_STACK_CONFIG_DIR", "/var/lib/home-stack/config"), "ssh")
SSH_KEY = os.path.join(SSH_DIR, "id_ed25519")
KNOWN_HOSTS = os.path.join(SSH_DIR, "known_hosts")


def _ssh(args: list[str], stdin: str | None = None, timeout: int = 20):
    return subprocess.run(args, capture_output=True, text=True,
                          input=stdin, timeout=timeout)


def deploy_key() -> dict:
    """The key this page deploys with: present, and its public half.

    The private half is never read, never shown and never leaves the file. What
    a person needs from this page is the *public* line, because getting it onto
    the other machine is the whole job.
    """
    if not os.path.isfile(SSH_KEY + ".pub"):
        return {"present": False, "public": "", "fingerprint": ""}
    try:
        public = open(SSH_KEY + ".pub", encoding="utf-8").read().strip()
    except OSError:
        return {"present": False, "public": "", "fingerprint": ""}
    fp = _ssh(["ssh-keygen", "-lf", SSH_KEY + ".pub"])
    return {"present": True, "public": public,
            "fingerprint": (fp.stdout or "").strip()}


def make_deploy_key() -> tuple[bool, str]:
    """Mint one, once. Never over an existing key.

    Replacing it would lock this page out of every machine that already trusts
    the old one, from a button whose label says "generate".
    """
    if os.path.exists(SSH_KEY):
        return False, "key_exists"
    try:
        os.makedirs(SSH_DIR, mode=0o700, exist_ok=True)
    except OSError as exc:
        return False, str(exc)
    r = _ssh(["ssh-keygen", "-t", "ed25519", "-N", "", "-C",
              "home-stack admin", "-f", SSH_KEY], timeout=60)
    if r.returncode != 0:
        return False, (r.stderr or "").strip()[:200]
    try:
        os.chmod(SSH_KEY, 0o600)
    except OSError:
        pass
    return True, "key_made"


def import_deploy_key(text: str) -> tuple[bool, str]:
    """Take a private key somebody already has, instead of minting a new one.

    The case this exists for: the machine already trusts a key, and it lives
    somewhere this stack cannot read -- a CI credential store, another
    account's ~/.ssh. Generating a fresh one then means getting it authorised
    on a machine you may only reach *with the key you are trying to replace*.

    Validated by deriving the public half, which is also the only way to tell a
    real key from a paste that lost its newlines. A passphrase-protected key
    fails the same check and is refused for a second reason: every connection
    here is BatchMode, so nothing could ever unlock it.

    An existing key is moved aside, never overwritten. Replacing one is the
    point of this button, and it still locks this page out of every machine
    that trusts the old one -- so the old one stays on disk.
    """
    text = (text or "").strip()
    if not text:
        return False, "key_empty"
    text += "\n"
    os.makedirs(SSH_DIR, mode=0o700, exist_ok=True)
    staged = SSH_KEY + ".incoming"
    try:
        with open(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                  "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        return False, str(exc)
    try:
        pub = _ssh(["ssh-keygen", "-y", "-P", "", "-f", staged], timeout=30)
        if pub.returncode != 0 or not (pub.stdout or "").strip():
            return False, "key_invalid"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for suffix in ("", ".pub"):
            live = SSH_KEY + suffix
            if os.path.exists(live):
                os.replace(live, f"{SSH_KEY}.replaced-{stamp}{suffix}")
        os.replace(staged, SSH_KEY)
        with open(os.open(SSH_KEY + ".pub", os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                          0o644), "w", encoding="utf-8") as fh:
            fh.write(pub.stdout.strip() + " home-stack admin (imported)\n")
        os.chmod(SSH_KEY, 0o600)
        return True, "key_imported"
    finally:
        if os.path.exists(staged):
            os.unlink(staged)


def set_remote_user(cfg: dict, role: str, user: str) -> bool:
    """Which account this stack logs in as, on one machine.

    Lives in two places because the roles do: `hosts.<role>.user` for the three
    under `hosts:`, and `cloud.vps.user` for the VPS, whose address is under
    `cloud.vps` because it is not a role anything is assigned to.
    """
    user = (user or "").strip()
    if not user:
        return False
    if role == "vps":
        cfg.setdefault("cloud", {}).setdefault("vps", {})["user"] = user
    else:
        hosts = cfg.setdefault("hosts", {})
        if role not in hosts:
            return False
        hosts[role]["user"] = user
    return True


def ssh_reachable(address: str, user: str, timeout: int = 12) -> dict:
    """Can this stack open an ssh session there, right now.

    Three answers, because they need three different things done about them and
    a single "failed" sends people to the wrong one. Getting the key onto the
    VPS, trusting a host key and a machine being down all present as the same
    red box otherwise, and the deploy log is where that used to be discovered.
    """
    if not address:
        return {"state": "unset", "detail": ""}
    if not os.path.isfile(SSH_KEY):
        return {"state": "nokey", "detail": SSH_KEY}
    r = _ssh(["ssh", "-i", SSH_KEY,
              "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
              "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
              "-o", "StrictHostKeyChecking=accept-new",
              f"{user}@{address}", "true"], timeout=timeout + 8)
    if r.returncode == 0:
        return {"state": "ok", "detail": ""}
    err = ((r.stderr or "") + (r.stdout or "")).strip()
    low = err.lower()
    # "Permission denied (publickey)" is the *good* failure: the machine is
    # there and answering, and the only thing missing is this key in its
    # authorized_keys -- which the public line on this page is for.
    if "permission denied" in low or "publickey" in low:
        return {"state": "auth", "detail": err[:200]}
    if "host key verification" in low or "remote host identification" in low:
        return {"state": "hostkey", "detail": err[:200]}
    return {"state": "unreachable", "detail": err[:200] or f"exit {r.returncode}"}


@app.post("/cloud/vps-test")
def cloud_vps_test():
    """Try the VPS from the page that configures it.

    In place rather than as a form post: this answers a question, it does not
    change anything, and a page reload would throw away the settings somebody is
    part-way through typing.
    """
    cfg = load_config()
    vps = ((cfg.get("cloud") or {}).get("vps") or {})
    result = ssh_reachable(str(vps.get("host") or "").strip(),
                           vps.get("user") or "root")
    return jsonify(**result)


# --- shared ntfy topics ------------------------------------------------------
#
# A member's own topic is derived from their name and used for *sending*
# (`NTFY_TOPIC_<MEMBER>`). `Parents` is the other kind: a topic several people
# receive, which is a question about *subscription* — and subscription lives in
# the chat proxy's `ntfy_config.json`, which nothing in this stack maintained.
#
# That is what cost this household its notifications. The VPS proxy had three of
# four members registered by hand, the local proxy had none, and the one member
# who was missing simply never got anything: the app asks `/api/ntfy-config`,
# was told 404, and subscribed to nothing. Silence, with every other part of the
# chain working and reporting success.
#
# So the checkbox writes the topic list to both proxies. The token is never
# touched: it belongs to that person's ntfy account, this page cannot mint one,
# and rewriting the list while dropping the token would unsubscribe them just as
# thoroughly as having no entry at all.
SHARED_TOPIC_PARENTS = "Parents"
FAMILY_TOPIC = "Family"


def member_ntfy_topics(cfg: dict, member: dict) -> list[str]:
    """The topics one member's phone should be subscribed to, in order."""
    own = str(member.get("ntfy_topic") or member.get("display_name")
              or member.get("id") or "").strip()
    topics = [own] if own else []
    if member.get("parents"):
        topics.append(SHARED_TOPIC_PARENTS)
    topics.append(FAMILY_TOPIC)
    seen, out_ = set(), []
    for t in topics:
        if t and t not in seen:
            seen.add(t)
            out_.append(t)
    return out_


# Run inside a proxy: set one login's topics, keep whatever token is stored.
_NTFY_SET = """
import json, os, sys
path = os.environ.get("NTFY_CONFIG_FILE", "/app/data/ntfy_config.json")
login, topics = sys.argv[1], [t for t in sys.argv[2].split(",") if t]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    data = data if isinstance(data, dict) else {}
except Exception:
    data = {}
# The token is that person's ntfy credential. This page cannot mint one, so
# rewriting the entry without it would unsubscribe them as completely as
# having no entry at all -- which is the failure this whole feature exists
# to stop repeating.
token = (data.get(login) or {}).get("token", "")
data[login] = {"topics": topics, "token": token}
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False)
os.replace(tmp, path)
print("ok", login, ",".join(topics), "token" if token else "no-token")
"""


def _proxy_ntfy_write(cfg: dict, login: str, topics: list[str]) -> dict:
    """Set that login's topics on both chat proxies, keeping each token.

    Both, because the household reaches the same name from two places: `dns`
    points the public name at this machine on the LAN, so a phone at home talks
    to the local proxy and the same phone in the street talks to the VPS. They
    hold separate stores, and this house had them disagree -- three members
    registered on one and none on the other.

    A proxy that cannot be reached is reported, not raised: losing the VPS must
    not stop the setting being saved, and the person needs to be told which half
    took it.
    """
    out_ = {"ok": [], "failed": []}
    if not login or not topics:
        return out_
    payload = ",".join(topics)

    r = subprocess.run(["docker", "exec", "-i", "home-chat-proxy-local",
                        "python3", "-", login, payload],
                       input=_NTFY_SET, capture_output=True, text=True, timeout=30)
    (out_["ok"] if r.returncode == 0 else out_["failed"]).append("local")

    vps = ((cfg.get("cloud") or {}).get("vps") or {})
    host = str(vps.get("host") or "").strip()
    if vps.get("enabled") and host and os.path.isfile(SSH_KEY):
        remote = ("docker exec -i home-chat-proxy python3 - "
                  f"{shlex.quote(login)} {shlex.quote(payload)}")
        r2 = subprocess.run(
            ["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
             "-o", "StrictHostKeyChecking=accept-new",
             f"{vps.get('user') or 'root'}@{host}", remote],
            input=_NTFY_SET, capture_output=True, text=True, timeout=45)
        (out_["ok"] if r2.returncode == 0 else out_["failed"]).append("vps")
    return out_


def remote_roles(cfg: dict) -> list[dict]:
    """The machines this stack has to reach over ssh, and nothing else.

    A role pointing at loopback is this machine: `Target.is_local`
    short-circuits ssh entirely, so listing it here would offer a fingerprint
    for a connection that never happens.
    """
    loopback = {"127.0.0.1", "::1", "localhost", ""}
    out_ = []
    for role, host in (cfg.get("hosts") or {}).items():
        address = str((host or {}).get("address") or "").strip()
        if address not in loopback:
            out_.append({"role": role, "address": address,
                         "user": (host or {}).get("user") or "homestack"})
    vps = ((cfg.get("cloud") or {}).get("vps") or {})
    if vps.get("enabled") and str(vps.get("host") or "").strip():
        out_.append({"role": "vps", "address": str(vps["host"]).strip(),
                     "user": vps.get("user") or "root"})
    return out_


def host_key_state(address: str) -> dict:
    """What that host presents, against what this stack currently trusts.

    Three answers, and they are three different situations:

    * **trusted** -- what it presents is what we have.
    * **unknown** -- we have nothing for it. Ordinary, on a host never reached.
    * **changed** -- we have something *else*. That is either a machine rebuilt
      or somebody in the middle, and this page will not guess which. It shows
      both fingerprints and makes a person say which one they mean.
    """
    scan = _ssh(["ssh-keyscan", "-T", "8", "-t", "ed25519,rsa", address])
    presented = [l for l in (scan.stdout or "").splitlines()
                 if l.strip() and not l.startswith("#")]
    if not presented:
        return {"state": "unreachable", "presented": "", "fingerprint": "",
                "detail": (scan.stderr or "").strip().splitlines()[:1]}

    def fingerprint(line: str) -> str:
        r = _ssh(["ssh-keygen", "-lf", "-"], stdin=line + "\n")
        return (r.stdout or "").strip()

    fp_now = fingerprint(presented[0])
    if not os.path.isfile(KNOWN_HOSTS):
        return {"state": "unknown", "presented": presented[0],
                "fingerprint": fp_now, "trusted_fingerprint": ""}
    have = _ssh(["ssh-keygen", "-F", address, "-f", KNOWN_HOSTS])
    lines = [l for l in (have.stdout or "").splitlines()
             if l.strip() and not l.startswith("#")]
    if not lines:
        return {"state": "unknown", "presented": presented[0],
                "fingerprint": fp_now, "trusted_fingerprint": ""}
    same = any(fingerprint(l).split()[1:2] == fp_now.split()[1:2]
               for l in lines if fingerprint(l))
    return {"state": "trusted" if same else "changed",
            "presented": presented[0], "fingerprint": fp_now,
            "trusted_fingerprint": fingerprint(lines[0])}


def trust_host(address: str) -> tuple[bool, str]:
    """Replace whatever we hold for *address* with what it presents now.

    Deliberately not additive: a changed key means the old entry is wrong, and
    leaving both would let either satisfy a future connection -- which is the
    property the check exists to deny.
    """
    scan = _ssh(["ssh-keyscan", "-T", "8", "-t", "ed25519,rsa", address])
    lines = [l for l in (scan.stdout or "").splitlines()
             if l.strip() and not l.startswith("#")]
    if not lines:
        return False, "unreachable"
    os.makedirs(SSH_DIR, mode=0o700, exist_ok=True)
    if os.path.isfile(KNOWN_HOSTS):
        _ssh(["ssh-keygen", "-R", address, "-f", KNOWN_HOSTS], timeout=30)
    with open(KNOWN_HOSTS, "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")
    os.chmod(KNOWN_HOSTS, 0o600)
    return True, "trusted"


# ---------------------------------------------------------------------------
# The portal login
# ---------------------------------------------------------------------------
# Who lives here is decided on this page. Until now, whether they could *sign
# in* was not: `install --create-user` made exactly one account and there was
# no second way to make another, so every member added afterwards existed in
# the config, had an assistant, had a folder on the share -- and no way to
# reach any of it.
#
# The store is the portal's state, at `{paths.state}/home-core/users.json`,
# and this page reaches it through the same identical-path mount the deploy
# uses. It holds `username`, a bcrypt `hash`, the `nanobot_id` that says which
# assistant instance answers, and `member`, which is the join back to this
# config. See services/home-core/local/app.py, "Who lives here".
USERS_FILE = os.environ.get("HOMECORE_USERS_FILE") or os.path.join(
    os.environ.get("HOME_STACK_STATE_DIR", "/var/lib/home-stack/state"),
    "home-core", "users.json")
# The portal's per-member conversation history, beside the store. Renaming a
# login has to carry this or every past conversation is orphaned -- the files
# are named after the login id, not the member.
HISTORY_DIR = os.path.join(os.path.dirname(USERS_FILE), "history")
MIN_PORTAL_PASSWORD = 8


def load_users() -> list:
    try:
        with open(USERS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_users(users: list) -> None:
    """Atomically, and readable by nobody else.

    A temporary in the same directory and a rename: the portal reads this file
    on nearly every request, and a half-written user store is a house where
    nobody can sign in.
    """
    os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(users, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, USERS_FILE)


def login_of(member_id: str) -> dict | None:
    """The account belonging to *member_id*, if there is one.

    Matched on `member`, falling back to the login id itself -- which is not a
    guess: in a fresh install the two are the same string, and a record written
    before this field existed says nothing else about who it belongs to.
    """
    users = load_users()
    for user in users:
        if user.get("member") == member_id:
            return user
    for user in users:
        if not user.get("member") and user.get("username") == member_id:
            return user
    return None


def member_nanobot_id(cfg: dict, member_id: str) -> int:
    """Which assistant instance answers for this member.

    The *position* in the deployed member list, 1-based, because that is what
    the deployer adds to `api_port_base` when it publishes the instances. Not
    the digits in the id: ids are monotonic and never reused, so a household
    that has seen departures runs `user2` beside `user15`, and `15` is a port
    nothing is listening on.

    `0` for somebody who has no instance -- a deactivated member, whom
    `services.nanobot.members` deliberately leaves out. It used to be `1`,
    which is not "no assistant", it is *the first active member's* assistant:
    setting a deactivated person's portal password signed them into somebody
    else's conversation, memory and tools. The portal already treats a falsy
    `nanobot_id` as "no assistant here", which is the true answer.
    """
    members = ((cfg.get("services") or {}).get("nanobot") or {}).get("members")
    if not members:
        members = [m["id"] for m in (cfg.get("members") or [])]
    return (members.index(member_id) + 1) if member_id in members else 0


def _carry_history(old_login: str, new_login: str) -> bool:
    """Move a renamed account's conversations. True if there were any.

    Renamed rather than copied, and only into a name nothing is using: two
    history directories for one person is worse than one, because the portal
    reads exactly one of them and nothing says which.
    """
    src = os.path.join(HISTORY_DIR, old_login)
    dst = os.path.join(HISTORY_DIR, new_login)
    if not os.path.isdir(src) or os.path.exists(dst):
        return False
    os.rename(src, dst)
    return True


def set_portal_login(cfg: dict, member_id: str, username: str,
                     password: str | None) -> tuple[bool, str]:
    """Create or change a member's portal account. (ok, message).

    Refuses rather than guesses in the two cases that would hand somebody
    another person's house: a login already belonging to a different member,
    and a password too short to be one.
    """
    username = (username or "").strip()
    if not username:
        # Bare, without the `login_` the caller prefixes: it flashes
        # `admin.members.login_{why}`, so "login_required" here asked the
        # catalogue for `admin.members.login_login_required`, which does not
        # exist -- and a missing key renders as the key, so a blank login name
        # put that literal string on the page as its error message.
        return False, "required"
    # One list, and the record has to be an element *of it*. `login_of()` does
    # its own `load_users()`, so taking the record from there and saving this
    # list wrote back the copy that was never edited -- a password change that
    # reported success and changed nothing. It also made the duplicate check
    # below compare two objects loaded separately, which are never the same
    # object, so a member re-saving their own login was told it was taken.
    users = load_users()
    record = next(
        (u for u in users if u.get("member") == member_id), None) or next(
        (u for u in users if not u.get("member")
         and u.get("username") == member_id), None)
    for user in users:
        if user.get("username") == username and user is not record:
            return False, "taken"
    if record is None:
        if not password:
            # A record with no hash is an account nobody can use and every
            # bcrypt check has to defend against. Ask for the password once.
            return False, "password_required"
        record = {"username": username}
        users.append(record)
    if password:
        if len(password) < MIN_PORTAL_PASSWORD:
            return False, "password_short"
        bcrypt = _bcrypt()
        record["hash"] = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    old = record.get("username")
    record["username"] = username
    record["member"] = member_id
    record["nanobot_id"] = member_nanobot_id(cfg, member_id)
    save_users(users)
    if old and old != username:
        _carry_history(old, username)
        return True, "renamed"
    return True, "saved"


def drop_portal_login(member_id: str) -> bool:
    """Take the account away when the member goes.

    Removing somebody left their login in the store, so they could still sign
    in to a house they are no longer part of -- with an assistant that is no
    longer deployed and a folder nothing lists. Their history is left on disk:
    an id is never reissued, so nothing can inherit it, and deleting a person's
    conversations is not a side effect of tidying a config.
    """
    users = load_users()
    keep = [u for u in users if u.get("member") != member_id
            and not (not u.get("member") and u.get("username") == member_id)]
    if len(keep) == len(users):
        return False
    save_users(keep)
    return True


def _ratchet_member_seq(cfg: dict) -> None:
    """Raise the id counter over everyone who exists right now.

    Called before *any* member is removed, and on every add. Ids are monotonic
    and never reused: filling a freed slot hands a new person the departed
    member's derived task token, their assistant state directory and their
    backups, all of which are pure functions of the id.

    It has to run on the removal path too, not only on the add. It used to live
    in the members handler, which did both -- so when removing moved to the
    member's own page the counter stopped being raised, and the next add
    recomputed it from the survivors and handed out the id that had just been
    freed. Caught by test_members.py, which is why that check exists.
    """
    highest = max(
        (int(m.group(1)) for q in (cfg.get("members") or [])
         if (m := re.fullmatch(r"user(\d+)", q["id"]))),
        default=0,
    )
    cfg["next_member_seq"] = max(int(cfg.get("next_member_seq") or 0), highest)


# The one service this page cannot deploy from inside itself.
SELF_SERVICE = "admin"


def running_in_container() -> bool:
    """Is this process inside a container at all?

    Separate from `cannot_deploy_from_here`, which asks whether deploying would
    be *wrong*. This asks whether the deployer and this page share a fate --
    they do whenever this runs in a container, however well set up it is.
    """
    return os.path.exists("/.dockerenv")


def cannot_deploy_from_here(cfg: dict) -> str:
    """Why the Deploy button would refuse, or "" if it would work.

    Asked before the button is offered rather than after it is pressed. The
    deployer's own guard is the authority and stays where it is -- this is the
    same question asked early, so a page that cannot deploy says so instead of
    handing somebody a button whose only outcome is a failed job.

    The guard: a role at 127.0.0.1 is "this machine", and inside a container it
    is not. Containers do start, because the Docker socket is mounted, so the
    failure is not "nothing happened" -- it is that the work lands on one
    machine and the checking on another. This container has its own network
    namespace and mounts neither the state tree nor the deploy root, so the
    override is not the answer here either.
    """
    if not os.path.exists("/.dockerenv"):
        return ""                      # a shell on the host: nothing to warn about
    if os.environ.get("HOME_STACK_ALLOW_CONTAINER_LOCAL") == "1":
        return ""                      # somebody has said this container is the host
    loopback = {"127.0.0.1", "::1", "localhost"}
    local_roles = sorted(
        role for role, host in (cfg.get("hosts") or {}).items()
        if (host or {}).get("address") in loopback
    )
    return ", ".join(local_roles)


def _backup_module():
    """`deploy/backup.py`, loaded the way the deployer is.

    Guarded for the same reason: a syntax error there must not take out the
    page somebody would use to fix it. Returns None when it cannot be loaded.
    """
    try:
        path = ROOT / "deploy" / "backup.py"
        spec = importlib.util.spec_from_file_location("backupmod", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("backupmod", module)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 - any failure here is non-fatal
        return None


def backup_inventory(cfg: dict) -> dict:
    """What a backup would keep, what it would skip, and what it cannot reach.

    Derived from `deploy/backup.py`, which derives it from the manifest's
    `state:` entries -- not a list kept here. A second copy would be a second
    thing to keep in step, and the whole point of this page is telling somebody
    what to point their own backup at.

    Three groups, because they are three different answers:

    * **essential** -- copied by every run.
    * **bulk** -- recordings and scanned documents, hundreds of gigabytes and
      unchanged between runs. Never in an archive, and listed here precisely
      because "not in the archive" is the thing worth being able to see.
    * **skipped** -- regenerable, and named anyway: "not in the archive" is
      something somebody should be able to see rather than infer.

    And `uncaptured`, which is state a run knowingly cannot reach at all.
    Naming a hole is not filling it, but an archive that quietly omits
    somebody's assistant memory reads exactly like one that does not.
    """
    mod = _backup_module()
    if mod is None:
        return {"error": "deploy/backup.py could not be loaded",
                "groups": [], "uncaptured": [], "root": ""}

    def _size(path: str, local: bool) -> int | None:
        # Only for a path on this machine, and only a directory walk -- no
        # `du` subprocess per row. None means "not measured here", which a
        # remote role legitimately is.
        if not local:
            return None
        p = Path(path)
        try:
            if p.is_file():
                return p.stat().st_size
            if not p.is_dir():
                return None
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        except OSError:
            return None

    hosts = cfg.get("hosts") or {}
    loopback = {"127.0.0.1", "::1", "localhost"}

    try:
        every = mod.entries(cfg, include_bulk=True)
        essential_only = {e["path"] for e in mod.entries(cfg)}
        uncaptured = mod.uncaptured(cfg)
        root = str(mod.backups_root(cfg))
    except Exception as exc:  # noqa: BLE001 - the page still has to render
        return {"error": str(exc), "groups": [], "uncaptured": [], "root": ""}

    rows = []
    for e in every:
        addr = (hosts.get(e.get("role", "hub")) or {}).get("address", "127.0.0.1")
        local = addr in loopback
        rows.append({**e, "address": addr, "local": local,
                     "group": "essential" if e["path"] in essential_only else "bulk",
                     "exists": Path(e["path"]).exists() if local else None,
                     "bytes": _size(e["path"], local)})

    # The skipped ones are not in `entries()` at all -- it drops them -- so they
    # are read back off the manifest. Shown because "regenerable" is a claim
    # worth being able to check.
    skipped = []
    deployer = _deployer()
    if deployer is not None:
        try:
            manifest = deployer.load_yaml(deployer.MANIFEST)
            manifest["_plugins"] = deployer.load_plugins(cfg)
            for service, spec in deployer.all_services(manifest, cfg).items():
                for unit in spec.get("units", []):
                    for st in deployer.interpolate(unit.get("state") or [], cfg):
                        if st.get("backup") == "skip":
                            skipped.append({"service": service, "unit": unit["name"],
                                            "path": st["path"], "group": "skipped",
                                            "role": spec.get("role", "hub"),
                                            "kind": st.get("kind", "dir")})
        except Exception:  # noqa: BLE001 - a bad plugin must not blank the page
            pass

    order = {"essential": 0, "bulk": 1, "skipped": 2}
    groups = []
    for name in ("essential", "bulk", "skipped"):
        members = [r for r in rows + skipped if r["group"] == name]
        members.sort(key=lambda r: (r["service"], r["path"]))
        if members:
            groups.append({"name": name, "rows": members})
    return {"error": "", "groups": groups, "uncaptured": uncaptured, "root": root}


def _sync_nanobot_members(cfg: dict) -> None:
    """One assistant instance per *active* member.

    The two lists have to agree, or the deployer allocates ports and
    credentials for people who are not here -- and a deactivated member keeps
    neither.
    """
    cfg.setdefault("services", {}).setdefault("nanobot", {})["members"] = [
        p["id"] for p in (cfg.get("members") or []) if p.get("active", True)
    ]
    # And the user store, which records the same position under `nanobot_id`.
    # Removing or deactivating somebody shifts everybody after them, and the
    # portal dials `api_port_base + nanobot_id - 1`: a stored rank that nobody
    # renumbered sent one member to the next member's assistant and the last
    # one to a port with nothing on it. This page is the only place members
    # change, so it is the only place the two lists can be kept in step.
    users = load_users()
    moved = False
    for user in users:
        mid = user.get("member")
        if not mid:
            continue
        want = member_nanobot_id(cfg, mid)
        if user.get("nanobot_id") != want:
            user["nanobot_id"] = want
            moved = True
    if moved:
        save_users(users)


# ---------------------------------------------------------------- WhatsApp
#
# Pairing a phone was a terminal job and nothing else: the bridge prints a QR to
# its own stdout and the only way to see it was `docker logs -f` on the host. So
# the one step a household actually has to repeat -- WhatsApp expires linked
# devices, and the only symptom is the assistant going quiet -- was the one step
# that needed somebody at a shell.
#
# The QR is read from the container's log rather than from the bridge's
# WebSocket. The bridge does broadcast it (`onQR` -> `{type:'qr'}`), but reading
# that needs a WebSocket client and a QR encoder, neither of which is in this
# image, and the log already carries exactly what a phone scans.


def wa_state_dir(member_id: str) -> str:
    """Where that member's bridge keeps its linked-device credentials."""
    return os.path.join(
        os.environ.get("HOME_STACK_STATE_DIR", "/var/lib/home-stack/state"),
        "nanobot", member_id, "whatsapp-auth")


def wa_container(member_id: str) -> str:
    return f"whatsapp-bridge-{member_id}"


def wa_linked(member_id: str) -> bool:
    """Whether a phone is actually linked.

    `creds.json` and nothing else. `bridge-token` is written when the bridge
    first starts and says only that the bridge ran -- a directory holding just
    that one file is an *unpaired* bridge, which is exactly the state that had
    the assistant silent on WhatsApp while everything looked present.
    """
    return os.path.isfile(os.path.join(wa_state_dir(member_id), "creds.json"))


def wa_container_state(member_id: str) -> str:
    """running / stopped / absent, from docker itself."""
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", wa_container(member_id)],
            capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return "absent"
        return (out.stdout or "").strip() or "absent"
    except Exception:
        return "absent"


def wa_qr(member_id: str) -> str:
    """The most recent QR block the bridge printed, or "".

    Only the last one: it prints a fresh code roughly every minute and the old
    ones are expired the moment the next appears. Showing an expired QR is worse
    than showing none -- it scans, and then fails.
    """
    try:
        out = subprocess.run(
            ["docker", "logs", "--tail", "400", wa_container(member_id)],
            capture_output=True, text=True, timeout=15)
    except Exception:
        return ""
    text = (out.stdout or "") + (out.stderr or "")
    marker = "Scan this QR code"
    if marker not in text:
        return ""
    tail = text[text.rindex(marker):]
    lines = []
    for line in tail.splitlines()[1:]:
        # The block is the run of lines made only of the half-block glyphs
        # qrcode-terminal draws with. Stop at the first line that is not, so a
        # log message printed underneath is never pulled in as part of the code.
        stripped = line.strip()
        if not stripped:
            if lines:
                break
            continue
        if set(stripped) <= set("\u2580\u2584\u2588\u258c\u2590 \u00a0"):
            lines.append(line.rstrip())
        elif lines:
            break
    return "\n".join(lines)


@app.post("/members/<member_id>/wa-restart")
def member_wa_restart(member_id):
    """Restart the bridge and answer with the code, without leaving the page.

    The form version redirected, which threw away the page to deliver one
    string. Asking for a fresh code is not a navigation.
    """
    if wa_container_state(member_id) == "absent":
        return jsonify(ok=False, reason="absent"), 409
    subprocess.run(["docker", "restart", wa_container(member_id)],
                   capture_output=True, timeout=60)
    return jsonify(ok=True)


@app.get("/members/<member_id>/wa-qr")
def member_wa_qr(member_id):
    """The pairing code as it stands *now*.

    The page rendered a snapshot taken when it loaded, and Baileys draws a new
    code roughly every twenty seconds -- so by the time somebody opened WhatsApp
    and pointed it at the screen, the code on the screen was dead and the phone
    said "couldn't link the device". Which reads as the pairing being broken
    rather than as the picture being stale.
    """
    return jsonify(
        qr=wa_qr(member_id),
        linked=wa_linked(member_id),
        state=wa_container_state(member_id),
    )


@app.route("/members/<member_id>", methods=["GET", "POST"])
def member_profile(member_id):
    """One person: who they are, what the assistant knows about them, and the
    handful of actions that only make sense for one member at a time.

    All of this used to be a row in the members table -- name, language and
    admin inline, then Off, a phone code, a Paperless token and Remove crammed
    into a last column, each behind its own hidden form. Six columns wide with
    four buttons in the last one, and the destructive one adjacent to the
    routine ones on a row you might have scrolled to by mistake.
    """
    cfg = load_config()
    people = cfg.get("members", []) or []
    person = next((m for m in people if m["id"] == member_id), None)
    if person is None:
        return redirect(url_for("members"))

    phone_code = None
    # Set only by the finance-token button, and only for that one
    # response. Initialised here so every other path through this view
    # renders without it rather than 500ing on a name that exists on
    # one branch.
    shown_token = None
    if request.method == "POST":
        before = load_config()
        action = request.form.get("action", "save")

        # A separate field, not a second `action` button. Two submits sharing
        # one name makes `request.form["action"]` ambiguous, and the Enter key
        # picks the first submit button in the form -- which would put "delete
        # these files" one keystroke away from anybody typing in the filter
        # box. `bulk` is only ever set by pressing that button.
        if request.form.get("bulk") == "remove":
            packages = [x.strip() for x in request.form.getlist("remove")
                        if x.strip()]
            svc = cfg.setdefault("services", {}).setdefault("audio-cpp", {})
            # The whole selection against each list at once. Checking one at a
            # time would let a set that empties a kind through, since no single
            # removal is the last one until the others have gone.
            doomed = set(packages)
            for key in ("tts_packages", "asr_packages"):
                listed = [x.strip() for x in str(svc.get(key) or "").split(",")
                          if x.strip()]
                if listed and not set(listed) - doomed:
                    flash(translator("admin.voice.remove_last_bulk",
                                     locale=current_locale(cfg)), "error")
                    return redirect(url_for("voice_page"))
            # Uninstall first, and only then rewrite the served lists -- from
            # what actually went, never from what was attempted. The other
            # order dropped a package from the config whether or not its
            # files left the disk, so a failed uninstall inside a successful
            # batch stopped the household serving a package it still stores:
            # the flash said it errored, and the list quietly disagreed.
            # `voice_remove`, the single-package path, returns 502 before it
            # ever reaches save_config and never had this.
            gone, failed = [], []
            for package in packages:
                why = audiocpp_uninstall(package)
                (failed if why else gone).append(package)
            for key in ("tts_packages", "asr_packages"):
                listed = [x.strip() for x in str(svc.get(key) or "").split(",")
                          if x.strip() and x.strip() not in set(gone)]
                svc[key] = ",".join(listed)
            if gone:
                save_config(cfg)
                note_pending(services_affected(before, cfg) | {"audio-cpp"})
                flash(translator("admin.voice.removed_many",
                                 locale=current_locale(cfg), n=len(gone)), "ok")
            if failed:
                flash(translator("admin.voice.remove_error",
                                 locale=current_locale(cfg),
                                 detail=", ".join(failed)), "error")
            audiocpp_catalogue(force=True)
            return redirect(url_for("voice_page"))

        if action == "remove":
            # Before the removal, or the counter is recomputed from the
            # survivors and the freed id is handed to the next person.
            _ratchet_member_seq(cfg)
            cfg["members"] = [p for p in people if p["id"] != member_id]
            # Their secrets go with them. The id is never reissued, but a live
            # bearer token with no owner is still a live bearer token.
            for key, _ in member_keys(member_id):
                delete_secret(key)
            # And their way in. This did not happen, so removing somebody left
            # a working login for a house they were no longer part of.
            drop_portal_login(member_id)
            _sync_nanobot_members(cfg)
            save_config(cfg)
            note_pending(services_affected(before, cfg))
            flash(translator("admin.members.removed", locale=current_locale(cfg),
                             name=person.get("display_name", member_id)), "ok")
            return redirect(url_for("members"))

        if action == "wa_restart":
            # A fresh QR now, rather than waiting out the current one. The
            # bridge draws a new code on connect, so a restart is the whole of
            # "show me one I can actually scan".
            st = wa_container_state(member_id)
            if st == "absent":
                flash(translator("admin.profile.wa_no_bridge",
                                 locale=current_locale(cfg)), "error")
            else:
                subprocess.run(["docker", "restart", wa_container(member_id)],
                               capture_output=True, timeout=60)
                flash(translator("admin.profile.wa_restarted",
                                 locale=current_locale(cfg)), "ok")
            return redirect(url_for("member_profile", member_id=member_id))

        if action == "wa_unlink":
            # Destructive on purpose and only from here: it throws away the
            # linked-device credentials, and the phone has to be scanned again.
            #
            # The directory, not just creds.json: Baileys keeps identity and
            # app-state keys beside it, and half a session left behind is a
            # bridge that fails in ways that look like the network rather than
            # like a wiped pairing.
            d = wa_state_dir(member_id)
            removed = 0
            if os.path.isdir(d):
                removed = len(os.listdir(d))
                shutil.rmtree(d, ignore_errors=True)
            if wa_container_state(member_id) != "absent":
                subprocess.run(["docker", "restart", wa_container(member_id)],
                               capture_output=True, timeout=60)
            flash(translator("admin.profile.wa_unlinked",
                             locale=current_locale(cfg), count=removed), "ok")
            return redirect(url_for("member_profile", member_id=member_id))

        if action == "phone_code":
            code, err = issue_phone_code(cfg, member_id)
            if code:
                phone_code = code
            else:
                flash(translator("admin.members.phone_code_error",
                                 locale=current_locale(cfg), detail=err), "error")
            # Falls through to render, so the code is on the page it was asked
            # from rather than on a list the person has to find themselves in.

        elif action == "portal_login":
            ok, why = set_portal_login(
                cfg, member_id,
                request.form.get("login", ""),
                request.form.get("password", "") or None)
            flash(translator(f"admin.members.login_{why}",
                             locale=current_locale(cfg),
                             n=MIN_PORTAL_PASSWORD),
                  "ok" if ok else "error")
            return redirect(url_for("member_profile", member_id=member_id))

        elif action == "finance_token":
            # Rendered, not redirected, and not stored: the same shape as the
            # secrets page's `reveal`. A POST, so the value is absent from the
            # URL, the browser history and the access log -- and gone from the
            # page on the next navigation, because nothing here keeps it.
            shown_token, err = finance_token(member_id)
            if not shown_token:
                flash(translator(f"admin.members.finance_{err}",
                                 locale=current_locale(cfg)), "error")
            # falls through to render, with the value in hand

        elif action == "paperless_token":
            token, err = issue_paperless_token(cfg, member_id)
            if token:
                key = f"PAPERLESS_API_TOKEN_{member_env_suffix(member_id)}"
                set_secret(key, token)
                note_pending(secret_impact(key))
                flash(translator("admin.members.paperless_ok",
                                 locale=current_locale(cfg), id=member_id), "ok")
            else:
                flash(translator("admin.members.paperless_error",
                                 locale=current_locale(cfg), detail=err), "error")
            return redirect(url_for("member_profile", member_id=member_id))

        else:
            # Who they are. `id` is not here and never will be: it is woven
            # through paths, topics, derived tokens and backups, and the whole
            # reason ids are monotonic is that one cannot become another's.
            new_name = request.form.get(
                "display_name", person.get("display_name", "")).strip() or member_id
            clash = folder_clash(cfg, member_id, new_name)
            if clash:
                flash(translator("admin.members.folder_clash",
                                 locale=current_locale(cfg), name=clash), "error")
                return redirect(url_for("member_profile", member_id=member_id))
            person["display_name"] = new_name
            person["admin"] = request.form.get("admin") == "on"
            person["locale"] = request.form.get("locale", person.get("locale", "en"))
            # Deactivating keeps the person, their id and everything they own.
            # Removing is the destructive one, and even that leaves their state
            # on disk -- an id is never reissued, so nothing can inherit it.
            person["active"] = request.form.get("active") == "on"
            # Their own WhatsApp bridge. One member's was written into the
            # compose file by name; the renderer reads this instead, so a
            # second linked phone is a checkbox rather than an edit.
            person["whatsapp"] = request.form.get("whatsapp") == "on"
            # Whether this person's Programmer runs on opencode. Per member
            # because one bridge holds one member's credentials: switching it
            # on gives them a container and a server of their own, and leaving
            # it off keeps their Programmer on the assistant, which is the same
            # profession answered by a different backend.
            person["programmer"] = request.form.get("programmer") == "on"
            # Also receives the shared `Parents` topic. Subscription, not
            # sending: it changes what that phone listens to, so it is pushed
            # to the chat proxies rather than only written to the config.
            person["parents"] = request.form.get("parents") == "on"

            # What the assistant is told.
            #
            # Stored ISO, typed in whatever order this locale writes dates.
            # A date that is not understood leaves the stored one alone and
            # says so: silently blanking somebody's birthday, or guessing
            # between the two readings of 03/04, are both worse than asking
            # again.
            _iso, _ok = parse_date(request.form.get("birthdate", ""),
                                   date_pattern(cfg))
            if _ok:
                person["birthdate"] = _iso
            else:
                flash(translator("admin.profile.birthdate_bad",
                                 locale=current_locale(cfg),
                                 format=date_pattern(cfg)), "error")
            person["hobbies"] = request.form.get("hobbies", "").strip()
            person["notes"] = request.form.get("notes", "").strip()
            relationships = {}
            for other in people:
                if other["id"] == member_id:
                    continue
                rel = request.form.get(f"rel:{other['id']}", "").strip()
                if rel:
                    relationships[other["id"]] = rel
            person["relationships"] = relationships

            cfg["members"] = people
            _sync_nanobot_members(cfg)
            save_config(cfg)
            note_pending(services_affected(before, cfg) | {"nanobot"})
            # Push the subscription out, because saving it here changes
            # nothing on its own: the phone learns its topics from the chat
            # proxy, and the proxies are what were out of step.
            # The login, not the member id: subscription is reached from a
            # request, and `_current_user` on the proxy returns what the session
            # carries. Keying this on `user1` would register a person who never
            # signs in as that.
            _acct = login_of(member_id) or {}
            _login = str(_acct.get("username") or "").strip()
            if _login:
                _res = _proxy_ntfy_write(cfg, _login,
                                         member_ntfy_topics(cfg, person))
                if _res["failed"]:
                    flash(translator("admin.profile.ntfy_partial",
                                     where=", ".join(_res["failed"])), "error")
            flash("saved", "ok")
            return redirect(url_for("member_profile", member_id=member_id))

    account = login_of(member_id)
    return render_template(
        "member_profile.html",
        person=person,
        # Present for exactly one response, after the button was pressed.
        finance_token=shown_token,
        date_format=date_pattern(cfg),
        # The other side of what somebody else already said. Offered, never
        # written: a box with something in it is not touched, and a household
        # is not all one shape.
        suggested=suggest_relationships(cfg, member_id, current_locale(cfg)),
        # Where the two halves already disagree. Each page reads fine on its
        # own; only holding them side by side shows it.
        conflicts=relationship_conflicts(cfg, member_id, current_locale(cfg)),
        birthdate_shown=format_date(person.get("birthdate", ""),
                                    date_pattern(cfg)),
        others=[m for m in people if m["id"] != member_id],
        locales=[{"code": c, "name": translator.name_of(c)} for c in translator.available],
        phone_code=phone_code,
        account=account,
        # The WhatsApp bridge, as it stands right now. Read on every render
        # rather than cached: a QR is valid for about a minute, and a cached one
        # is a code that scans and then fails.
        wa={
            "enabled": bool(person.get("whatsapp")),
            "state": wa_container_state(member_id),
            "linked": wa_linked(member_id),
            # No QR here. It is fetched when somebody asks for it, because a
            # code is dead about twenty seconds after it is drawn -- rendering
            # one into the page guarantees it is stale by the time anybody
            # points a phone at it, which is what "couldn't link the device"
            # was. It also keeps `docker logs` off every page load.
        },
        min_password=MIN_PORTAL_PASSWORD,
        # Whether renaming this login would strand anything. Nothing to warn
        # about on a house that has never had a conversation, and there is no
        # point spending somebody's attention on a risk they do not have.
        has_history=bool(account) and os.path.isdir(
            os.path.join(HISTORY_DIR, account.get("username", ""))),
    )


@app.route("/secrets", methods=["GET", "POST"])
def secrets_page():
    revealed = {}
    if request.method == "POST":
        key = request.form.get("key", "")
        action = request.form.get("action", "set")

        if action == "generate":
            # One key, minted the moment it is needed. Never in bulk from this
            # page: a button that rotates thirty credentials at once is a
            # button that takes the whole house down one misclick at a time.
            try:
                set_secret(key, generate_secret(key))
                note_pending(secret_impact(key))
                flash("saved", "ok")
            except ValueError as exc:
                flash(str(exc), "error")
            return redirect(url_for("secrets_page"))

        if action == "reveal":
            # Shown for this one response and never persisted anywhere the
            # browser keeps: a POST, so the value is absent from the URL, the
            # history and the server log.
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", key or ""):
                revealed[key] = get_secret(key)
            # falls through to render with the value visible

        elif request.form.get("value", ""):
            try:
                set_secret(key, request.form.get("value", ""))
                note_pending(secret_impact(key))
                flash("saved", "ok")
            except ValueError as exc:
                flash(str(exc), "error")
            return redirect(url_for("secrets_page"))
        else:
            return redirect(url_for("secrets_page"))

    present = secret_keys_present()
    cfg = load_config()
    # `secret_keys`, not `keys`: Jinja resolves attributes before items, so
    # `m.keys` in a template is the dict *method* -- it rendered /secrets as a
    # 500 trying to iterate a builtin.
    members_rows = [
        {"id": p["id"], "name": p.get("display_name", p["id"]),
         "secret_keys": member_keys(p["id"])}
        for p in (cfg.get("members") or [])
    ]
    groups = {
        # Everywhere the assistant can think, in one place, because the choice
        # is one choice: which service answers, and for which kind of turn.
        # A household can run notifications on the GPU in the cupboard, the
        # everyday assistant on a flat plan, and a profession on whatever is
        # best at it -- and that is only obvious if the keys sit together.
        #
        # OpenCode Zen first because it is the one this stack recommends,
        # billed per token. Not the OpenCode **Go** plan on the same account:
        # Go is the flat $10/month subscription, its traffic is "monitored for
        # abusive traffic that degrades the experience for other users", and
        # what this stack sends is five containers on one key running unattended
        # around the clock. That is the shape a flat plan flags, and the account
        # can be blocked for it. None of the rest is required, and a local
        # Ollama needs no key at all.
        "models": ["OPENCODE_API_KEY", "OPENROUTER_API_KEY",
                   "OLLAMA_API_KEY", "TOGETHER_API_KEY"],
        "required": [],
        "generated": [
            "HOMECORE_SECRET_KEY", "HOMECORE_DEBUG_API_KEY", "ADMIN_SECRET_KEY",
            "PROXY_SHARED_SECRET", "VOICE_GATEWAY_TOKEN", "NANOBOT_DEBUG_SECRET",
            "NANOBOT_API_SECRET_HOUSE", "SHARE_SMB_PASSWORD", "PAPERLESS_SECRET_KEY",
            "PROXY_SESSION_SIGNING_KEY", "BACKUP_ENCRYPTION_KEY",
        ],
        "optional": [
            "HOMEASSISTANT_TOKEN", "NTFY_CREDENTIALS", "ACME_DNS_API_TOKEN",
            "TAVILY_API_KEY", "BRAVE_API_KEY",
            "KAGI_API_KEY", "JINA_API_KEY", "NANOBOT_N8N_API_KEY",
            "PAPERLESS_MAIL_PASSWORD", "BRIGHTDATA_API_TOKEN",
        ],
    }
    return render_template("secrets.html", groups=groups, present=present,
                           plugin_groups=plugin_secret_groups(),
                           members_rows=members_rows, revealed=revealed)


# ---------------------------------------------------------------------------
# The deploy job
# ---------------------------------------------------------------------------
# One at a time, in a background thread, streaming into a log file the page
# polls. The synchronous version held the browser on a spinning tab for however
# long a build took, showed nothing until the end, and a closed tab orphaned
# the run with no way to see how it ended.
DEPLOY_LOG = CONFIG.parent / "deploy-job.log"
_deploy_lock = threading.Lock()
_deploy_job = {"running": False, "failed": None, "targets": [],
               "dry_run": False, "started": None, "finished": None}


def _run_deploy_job(argv, targets, dry_run, clear_all):
    with DEPLOY_LOG.open("w") as log:
        proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                text=True, cwd=ROOT)
        code = proc.wait()
    with _deploy_lock:
        _deploy_job.update(running=False, failed=code != 0,
                           finished=time.time())
    if code == 0 and not dry_run:
        # Only what actually deployed stops being pending.
        if clear_all:
            save_pending([])
        else:
            save_pending(set(load_pending()) - set(targets))


def _start_deploy(targets, dry_run):
    argv = [sys.executable, str(DEPLOYER), *targets, "--no-color"]
    if dry_run:
        argv.append("--dry-run")
    with _deploy_lock:
        if _deploy_job["running"]:
            return False
        _deploy_job.update(running=True, failed=None, targets=targets,
                           dry_run=dry_run, started=time.time(), finished=None)
    clear_all = targets == ["all"] or set(targets) >= set(load_pending() or ["-"])
    threading.Thread(target=_run_deploy_job,
                     args=(argv, targets, dry_run, clear_all),
                     daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# The Alfred APK
# ---------------------------------------------------------------------------
# Two Jenkins jobs built this, against a separate repository, on the one machine
# with an Android SDK installed by hand -- which made "release the app" something
# only that machine could do, and left the SDK there as the only reason it needed
# one. Everything the build needs is in this repository except the signing key,
# which is the household's and lives in the config directory (APK_SIGNING):
# whoever holds it can sign an update every installed app accepts, so it is never
# committed. The builder creates one there on a house's first build. Publishing
# needs the share credential this stack already owns.
#
# Same shape as the deploy job below it: one at a time, in a thread, streaming
# into a log the page polls. A build is minutes, which is far too long to hold a
# browser tab on.
# `/cache`, not `CONFIG.parent`. This was written when CONFIG was a file
# bind-mounted into `/state`, whose rest is the container's own filesystem, so
# anything beside it died the moment a deploy recreated the container -- which
# is exactly when somebody looks for the result of the build they just ran.
# CONFIG is in the host's config directory now, but that is the household's
# settings, not a place for build artefacts. This is the volume for those.
APK_DIR = Path(os.environ.get("HOME_STACK_CACHE", "/cache"))
APK_LOG = APK_DIR / "apk-build.log"
APK_IMAGE = "alfred-app-builder:latest"
APK_SRC = ROOT / "services" / "proxy" / "android"
APK_SIGNING = CONFIG.parent / "alfred-app"
APK_STATE = APK_DIR / "apk-build.json"
_apk_lock = threading.Lock()
_apk_job = {"running": False, "failed": None, "channel": None,
            "started": None, "finished": None}


def _apk_dir() -> Path:
    """The directory the log and the verdict live in, created on first use.

    Not at import. `mkdir` at module level made importing this file require a
    writable /cache, so every suite that imports it died with
    "PermissionError: [Errno 13] ... '/cache'" outside the container -- three of
    them, on a change that had nothing to do with them.
    """
    try:
        APK_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001 - reported by the caller that needs it
        pass
    return APK_DIR


def _apk_save():
    """Keep the verdict beside the log, because this process does not keep it.

    The container restarts on every deploy of this page -- including the deploy
    somebody runs right after a build -- and an in-memory result dies with it
    while `apk-build.log` survives. The page then had a full log and no way to
    say whether it ended well, which is the same as not reporting at all.
    """
    try:
        with _apk_lock:
            state = dict(_apk_job)
        _apk_dir()
        APK_STATE.write_text(json.dumps(state))
    except Exception:  # noqa: BLE001 - a lost verdict must not fail a good build
        pass


def _apk_load():
    """The verdict from a previous process, if this one has not run a build."""
    with _apk_lock:
        if _apk_job["started"] or _apk_job["running"]:
            return dict(_apk_job)
    try:
        state = json.loads(APK_STATE.read_text())
        # Never resume "running" from disk: the process that was running it is
        # gone, so a crashed build would otherwise show as running for ever.
        state["running"] = False
        return state
    except Exception:  # noqa: BLE001
        with _apk_lock:
            return dict(_apk_job)


def _apk_env() -> dict:
    """Where to publish, and what to publish with.

    The two halves come from two places, which is the thing to get right here:
    the password is a *secret* named `SHARE_SMB_PASSWORD`, and the host is
    *config* (`share.host`). The manifest already spells this out for the portal
    -- `SMB_PASSWORD: $SHARE_SMB_PASSWORD` beside `SMB_HOST: {derived.share_host}`
    -- and a first version of this looked for `SMB_PASSWORD` in the secrets file,
    found nothing, and built an APK that went nowhere while reporting success.

    Read from the files rather than from this process's environment: the admin
    container is not given the share password, and putting it in this image's
    environment to hand to a child would leave it in `docker inspect` for
    anything that can read the socket.
    """
    env = {}
    try:
        for line in (CONFIG.parent / "smart-home-bot.env").read_text().splitlines():
            if line.startswith("SHARE_SMB_PASSWORD=") and "=" in line:
                env["SMB_PASSWORD"] = line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001 - no credential is a build that does not publish
        pass
    try:
        cfg = load_config()
        env["SMB_HOST"] = str((cfg.get("share") or {}).get("host", ""))
        env["SMB_SHARE"] = str((cfg.get("share") or {}).get("name", "share"))
        env["SMB_USERNAME"] = str((cfg.get("share") or {}).get("user", "share"))
        domain = str(((cfg.get("cloud") or {}).get("vps") or {}).get("domain", ""))
        if domain:
            env["CHAT_BASE_URL"] = domain if "://" in domain else f"https://{domain}"
        # The second host the app's WebView is allowed to keep: opencode's
        # interface, from `dns.code`. Without it the build is valid and the
        # Programmer link opens the phone's browser instead of staying in the
        # app, because MainActivity hands every host it was not told about to an
        # ACTION_VIEW intent. Empty when the household has no such name, which
        # is the shipped default and behaves exactly as before.
        code = str((cfg.get("dns") or {}).get("code", "") or "")
        if code:
            env["CODE_BASE_URL"] = code if "://" in code else f"https://{code}"
    except Exception:  # noqa: BLE001
        pass
    return {k: v for k, v in env.items() if v}


def _run_apk_job(channel: str):
    creds = _apk_env()

    # Built in a copy, never in the repository. Gradle writes `app/build/` and
    # `.gradle/` beside the sources -- hundreds of megabytes -- and the deployer
    # rsyncs the working tree, so a build started from this page would ship its
    # own output to every service on the next deploy and leave `git status`
    # permanently dirty. The copy lives in state, where build output belongs.
    work = Path(os.environ.get("HOME_STACK_STATE_DIR",
                               "/var/lib/home-stack/state")) / "alfred-app" / "src"
    def _clear(path: Path):
        """Remove a previous staging tree, including what root left behind.

        The builder runs as root, so its `app/build/` output is root-owned and
        `rmtree` from this process gets EACCES on the second run -- the first
        build works and every one after it fails to even start. Falling back to
        a throwaway container is the same privilege that made the files.
        """
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            pass
        subprocess.run(
            ["docker", "run", "--rm", "-v", f"{path.parent}:/w",
             "alpine:latest", "rm", "-rf", f"/w/{path.name}"],
            capture_output=True, timeout=120)

    try:
        APK_SIGNING.mkdir(parents=True, exist_ok=True)
        work.parent.mkdir(parents=True, exist_ok=True)
        if work.exists():
            _clear(work)
        # `.gradle` and any previous output are left behind deliberately: the
        # tree is replaced, and the *cache* that makes a second build fast is
        # the named volume, not this directory.
        shutil.copytree(APK_SRC, work,
                        ignore=shutil.ignore_patterns(".gradle", "build"))
    except Exception as exc:  # noqa: BLE001
        _apk_dir()
        with APK_LOG.open("w") as log:
            log.write(f"ERROR: could not stage the sources in {work}: {exc}\n")
        with _apk_lock:
            _apk_job.update(running=False, failed=True, finished=time.time())
        return

    argv = [
        "docker", "run", "--rm", "--network", "host",
        "-v", f"{work}:/src",
        # The household's signing key (build-apk.sh makes one if there is none).
        "-v", f"{APK_SIGNING}:/signing",
        # Gradle's caches, so the second build is minutes rather than the first
        # one again.
        "-v", "alfred-app-gradle:/gradle",
        "-e", f"SMB_HOST={creds.get('SMB_HOST', '')}",
        "-e", f"SMB_SHARE={creds.get('SMB_SHARE', 'share')}",
        "-e", f"SMB_USERNAME={creds.get('SMB_USERNAME', 'share')}",
        "-e", f"SMB_PASSWORD={creds.get('SMB_PASSWORD', '')}",
        # The address this house's phones use, from `cloud.vps.domain`. Without
        # it the APK keeps the sanitised `chat.home` compiled in and fails
        # on its first request.
        "-e", f"CHAT_BASE_URL={creds.get('CHAT_BASE_URL', '')}",
        "-e", f"CODE_BASE_URL={creds.get('CODE_BASE_URL', '')}",
        APK_IMAGE, channel,
    ]
    _apk_dir()
    with APK_LOG.open("w") as log:
        # The credential is in argv, which `ps` on this host can see for the
        # length of the run. Not written to the log, which is what the page
        # shows and what somebody pastes into a message.
        log.write(f"$ docker run --rm ... {APK_IMAGE} {channel}\n\n")
        log.flush()
        proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                text=True, cwd=ROOT)
        code = proc.wait()
    with _apk_lock:
        _apk_job.update(running=False, failed=code != 0, finished=time.time())
    _apk_save()


def _start_apk_build(channel: str) -> bool:
    if channel not in ("release", "beta"):
        return False
    with _apk_lock:
        if _apk_job["running"]:
            return False
        _apk_job.update(running=True, failed=None, channel=channel,
                        started=time.time(), finished=None)
    _apk_save()
    threading.Thread(target=_run_apk_job, args=(channel,), daemon=True).start()
    return True


def apk_image_present() -> bool:
    """Whether the builder image exists. It is ~2.5 GB and built on demand."""
    try:
        out = subprocess.run(["docker", "image", "inspect", APK_IMAGE],
                             capture_output=True, timeout=15)
        return out.returncode == 0
    except Exception:  # noqa: BLE001
        return False


@app.get("/apk/log")
def apk_log():
    """Same contract as /deploy/log, so the page polls both the same way."""
    offset = max(0, request.args.get("offset", 0, type=int))
    text = ""
    if APK_LOG.exists():
        with APK_LOG.open() as fh:
            fh.seek(offset)
            text = fh.read()
            offset = fh.tell()
    job = _apk_load()
    return jsonify(
        offset=offset, lines=text.splitlines(),
        running=job["running"], failed=job["failed"], channel=job["channel"],
        elapsed=round((job["finished"] or time.time()) - job["started"], 1)
        if job["started"] else 0,
    )


@app.post("/apk/build")
def apk_build():
    """Start a build. Answers JSON when asked to, redirects otherwise.

    The page starts it over fetch and stays where it is: a build is minutes of
    log arriving into a card that is already on screen, and reloading to deliver
    "it started" threw away the page to say something the page could show. The
    redirect stays for a form post without JavaScript, which is what the buttons
    are still marked up as.
    """
    channel = request.form.get("channel", "")
    started = _start_apk_build(channel)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=started, channel=channel,
                       reason="" if started else "busy"), (200 if started else 409)
    if not started:
        flash(translator("admin.apk.busy", locale=current_locale(load_config())),
              "error")
    return redirect(url_for("deploy"))


# --------------------------------------------------------------------------
# Models > Benchmark
# --------------------------------------------------------------------------
# One model, through a real assistant's agent loop, on the house's own work:
# everyday chat, tool calls, notification triage, events and the heartbeat.
# The benchmark is services/nanobot/bench/model_bench.py, baked into the nanobot
# image; this page resolves the model (a Hugging Face GGUF link becomes an
# Ollama name), pulls it if it is local and missing, runs it with one
# `docker exec`, and keeps the result for the comparison table.
#
# Same shape as the deploy and APK jobs: one at a time, a log file the page
# polls by offset, state in a dict behind a lock.
BENCH_DIR = APK_DIR / "bench"
BENCH_RESULTS = BENCH_DIR / "results"
BENCH_LOGS = BENCH_DIR / "logs"
BENCH_STATE = BENCH_DIR / "job.json"
BENCH_LOCK = BENCH_DIR / "job.lock"
BENCH_QUEUE = BENCH_DIR / "queue.json"      # runs waiting for the one in progress
_BENCH_CONTAINER_RE = re.compile(r"^nanobot-(?:user\d+|house)$")
# What the running job holds, so Stop can cut it short from a request thread:
# the pull's open response and the `docker exec` of the benchmark.
_bench_live = {"stop": threading.Event(), "pull": None, "proc": None,
               "container": "", "run_id": ""}


def _bench_stopping() -> bool:
    """Stop was pressed -- in this process, or in whichever one served the page."""
    return _bench_live["stop"].is_set() or bool(_bench_state().get("stop_requested"))


def _bench_state() -> dict:
    """The job as the page sees it, from disk -- so every admin process agrees.

    It used to be a dict in this process, which is how two runs happened at
    once on 2026-09-10: a second process could not see the first one's lock,
    and both wrote into the same log, so a pull at 0% sat above another
    model's results and read as "testing before it was downloaded".
    """
    try:
        state = json.loads(BENCH_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"running": False}
    # "running" on disk is only true while somebody holds the lock. An admin
    # restart mid-run kills the job thread and leaves the file saying running
    # forever -- which would refuse every run after it as "busy".
    if state.get("running") and not _bench_lock_held():
        state.update(running=False, stopped=True)
    return state


def _bench_lock_held() -> bool:
    try:
        fd = os.open(BENCH_LOCK, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _bench_save_state(**changes) -> dict:
    state = {**_bench_state(), **changes}
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = BENCH_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(BENCH_STATE)
    return state


def _bench_queue() -> list[dict]:
    try:
        return json.loads(BENCH_QUEUE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _bench_queue_write(entries: list[dict]) -> None:
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = BENCH_QUEUE.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries), encoding="utf-8")
    tmp.replace(BENCH_QUEUE)


def _bench_queue_pop() -> dict | None:
    """The next waiting run, taken under the queue's own lock (any process may add)."""
    fd = os.open(BENCH_DIR / "queue.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        entries = _bench_queue()
        if not entries:
            return None
        head, rest = entries[0], entries[1:]
        _bench_queue_write(rest)
        return head
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def bench_containers() -> list[str]:
    """The assistants a benchmark can run inside: members first, then the house."""
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    names = [n for n in out.split() if _BENCH_CONTAINER_RE.match(n)]
    return sorted(names, key=lambda n: (n == "nanobot-house", n))


# Each assistant's setup digest as deployed now -- `setup_digest` in
# model_bench.py, which a run stamps on its result -- so the results card can
# say which runs were made with skills, prompts or code that have since
# changed. Asking costs ~5 s of nanobot imports inside the container, so it is
# asked once per container start (keyed on the container's id, which a redeploy
# changes), in the background, and never while a page waits.
_setup_digests: dict[str, tuple[str, str]] = {}      # name -> (container id, digest)
_setup_pending: set[str] = set()
_setup_lock = threading.Lock()


def _container_ids(names) -> dict[str, str]:
    """{name: full container id} for the running ones, in one docker call."""
    names = sorted(set(n for n in names if n and _BENCH_CONTAINER_RE.match(n)))
    if not names:
        return {}
    try:
        out = subprocess.run(["docker", "inspect", "-f", "{{.Name}} {{.Id}}", *names],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    ids = {}
    for line in out.splitlines():
        name, _, cid = line.strip().lstrip("/").partition(" ")
        if name and cid:
            ids[name] = cid
    return ids


def _remember_setup_digest(name: str, digest: str) -> None:
    cid = _container_ids([name]).get(name)
    if cid:
        with _setup_lock:
            _setup_digests[name] = (cid, digest)


def _work_out_setup_digest(name: str, cid: str) -> None:
    """Run the benchmark's own `setup_digest` in *name*, the script piped in.

    The page's copy of the script, the same one each run is given, so both
    sides hash with the same function -- the image's baked copy may be older.
    """
    digest = ""
    try:
        script = (ROOT / "services" / "nanobot" / "bench" / "model_bench.py").read_bytes()
        got = subprocess.run(["docker", "exec", "-i", name, "python3", "-", "--setup-digest"],
                             input=script, capture_output=True, timeout=90)
        last = (got.stdout.decode(errors="replace").strip().splitlines() or [""])[-1]
        if got.returncode == 0 and re.fullmatch(r"[0-9a-f]{12}", last):
            digest = last
    except (OSError, subprocess.SubprocessError):
        pass
    with _setup_lock:
        _setup_pending.discard(name)
        if digest:
            _setup_digests[name] = (cid, digest)


def _current_setup_digests(names) -> dict[str, str]:
    """{name: digest} for the containers whose setup is known right now.

    Anything not known yet is started in the background and left out, which
    reads as "can't tell" -- the next time the card is drawn it is there.
    """
    ids = _container_ids(names)
    known: dict[str, str] = {}
    start: list[tuple[str, str]] = []
    with _setup_lock:
        for name, cid in ids.items():
            cached = _setup_digests.get(name)
            if cached and cached[0] == cid:
                known[name] = cached[1]
            elif name not in _setup_pending:
                _setup_pending.add(name)
                start.append((name, cid))
    for name, cid in start:
        threading.Thread(target=_work_out_setup_digest, args=(name, cid), daemon=True).start()
    return known


# A separate Ollama for benchmarks: GPU0, port 11436 (see bench/host/README).
# Used when it answers, so a candidate model never evicts the family's model on
# GPU1 -- on the shared card every benchmark case and every family turn swapped
# one model out for the other, and the numbers measured the swapping.
BENCH_OLLAMA_DEFAULT = "http://127.0.0.1:11436"


def _bench_instance(cfg: dict) -> tuple[str, str]:
    """(URL this page uses, URL a container uses) for the benchmark Ollama, or ("", "")."""
    try:
        inst = OI.bench_instance(cfg)
    except OI.InstanceError:
        inst = None
    if inst and inst["url"]:
        url = inst["url"]
    elif inst:
        address = ((cfg.get("hosts") or {}).get(inst["host"]) or {}).get("address") or "127.0.0.1"
        url = f"http://{address}:{inst['port']}"
    else:
        url = str((((cfg.get("cloud") or {}).get("ollama") or {}).get("bench") or {}).get("url")
                  or BENCH_OLLAMA_DEFAULT)
    url = url.rstrip("/")
    try:
        with _bench_ollama(url, "/api/tags", method="GET", timeout=4):
            pass
    except (OSError, ValueError):
        return "", ""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    inside = url if host not in ("127.0.0.1", "localhost", "::1") else urllib.parse.urlunsplit(
        parsed._replace(netloc="host.docker.internal" + (f":{parsed.port}" if parsed.port else "")))
    return url, inside


def _bench_ollama_url(cfg: dict, prefix: str) -> str:
    source = OI.provider_of_prefix(prefix) or "ollama"
    return (model_endpoints(cfg).get(source) or {}).get("url", "").rstrip("/")


def _bench_ollama(url: str, path: str, body: dict | None = None,
                  method: str = "POST", timeout: int = 30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{url}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def _bench_pull(url: str, name: str, log) -> bool:
    """Pull *name* into the Ollama at *url* unless it is there. Progress to *log*."""
    try:
        with _bench_ollama(url, "/api/tags", method="GET", timeout=10) as r:
            have = {m.get("name") for m in json.load(r).get("models") or []}
    except (OSError, ValueError) as exc:
        log.write(f"cannot reach Ollama at {url}: {exc}\n")
        return False
    if name in have or (":" not in name and f"{name}:latest" in have):
        log.write(f"{name} is already on {url}\n")
        return True
    log.write(f"pulling {name} into {url} ...\n")
    log.flush()
    last, last_status = -10, ""
    try:
        r = _bench_ollama(url, "/api/pull", {"model": name, "stream": True}, timeout=3600)
        _bench_live["pull"] = r
        with r:
            for i, raw in enumerate(r):
                if _bench_live["stop"].is_set() or (i % 20 == 0 and _bench_stopping()):
                    return False
                ev = json.loads(raw or b"{}")
                if ev.get("error"):
                    log.write(f"pull failed: {ev['error']}\n")
                    return False
                status, total, done = ev.get("status", ""), ev.get("total"), ev.get("completed")
                if total and done:
                    pct = int(done * 100 / total)
                    if pct >= last + 10:
                        last = pct
                        log.write(f"  downloading: {pct}% of {total / 1e9:.1f} GB\n")
                        log.flush()
                elif status and status != last_status and not status.startswith("pulling "):
                    last_status = status
                    log.write(f"  {status}\n")
                    log.flush()
    except (OSError, ValueError) as exc:
        if not _bench_live["stop"].is_set():
            log.write(f"pull failed: {exc}\n")
        return False
    finally:
        _bench_live["pull"] = None
    return not _bench_live["stop"].is_set()


# The local engines a benchmark brings up itself: `llamacpp:` is the llama.cpp
# router (llamacpp-router.service on the host, reached through the stack's one
# `cloud.openai_compatible` slot), `freetoken:` is FreeToken. Neither runs a
# model all the time -- the card they use is the benchmark Ollama's -- so a run
# makes sure the engine answers and lists the model, clears the card, lets the
# router load the model on the first request, and unloads it when done.
_ENGINES = {"llamacpp": ("openai_compatible", "OPENAI_COMPATIBLE_API_KEY"),
            "freetoken": ("freetoken", "FREETOKEN_API_KEY")}


def _bench_engine(cfg: dict, prefix: str) -> tuple[str, str]:
    """(base url without /v1, api key) for a local engine, or ("", "") when it is off."""
    slot, key_name = _ENGINES[prefix]
    conf = (cfg.get("cloud") or {}).get(slot) or {}
    url = str(conf.get("url") or "").strip().rstrip("/")
    if not conf.get("enabled") or not url:
        return "", ""
    if url.endswith("/v1"):
        url = url[:-3]
    key = ""
    for line in _read_secrets().splitlines():
        if line.startswith(key_name + "="):
            key = line.split("=", 1)[1].strip()
    return url, key


def _engine_call(url: str, path: str, key: str, body: dict | None = None,
                 method: str = "GET", timeout: int = 10):
    headers = {"Content-Type": "application/json"}
    if key and key != "disabled":
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{url}{path}", data=data, method=method, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def _bench_engine_up(cfg: dict, prefix: str, name: str, bench_url: str, log) -> bool:
    """Is the engine for *name* answering, and is its card free? Says why not."""
    url, key = _bench_engine(cfg, prefix)
    if not url:
        slot = _ENGINES[prefix][0]
        log.write(f"not benchmarked: cloud.{slot} is switched off, so there is no {prefix} "
                  "engine to run it on.\n")
        return False
    if prefix == "freetoken":
        try:
            _engine_call(url, "/v1/models", key).close()
        except Exception as exc:                           # noqa: BLE001
            log.write(f"not benchmarked: FreeToken is not answering at {url} ({exc}). It is "
                      "not started from here -- start it on the host, then run again.\n")
            return False
        return True
    try:
        with _engine_call(url, "/models", key) as r:
            listed = [m.get("id") for m in (json.load(r).get("data") or [])]
    except Exception as exc:                               # noqa: BLE001
        log.write(f"not benchmarked: the llama.cpp router is not answering at {url} ({exc}). "
                  "On the host: systemctl --user start llamacpp-router\n")
        return False
    if name not in listed:
        log.write(f"not benchmarked: the llama.cpp router has no model called {name}. It serves "
                  f"{', '.join(listed) or 'nothing'} -- add it to models.ini.\n")
        return False
    # The router's card is the benchmark Ollama's: whatever that holds stays
    # out of the way, or the model does not fit.
    if bench_url:
        try:
            with _bench_ollama(bench_url, "/api/ps", method="GET", timeout=10) as r:
                held = [m.get("name") for m in (json.load(r).get("models") or [])]
        except Exception:                                  # noqa: BLE001
            held = []
        for other in held:
            _bench_unload(bench_url, other, log)
    log.write(f"on the llama.cpp router ({url}): {name} loads on the first request\n")
    log.flush()
    return True


def _bench_engine_down(cfg: dict, prefix: str, name: str, log) -> None:
    """Give the card back: unload *name* from the router. FreeToken is left as found."""
    if prefix != "llamacpp":
        return
    url, key = _bench_engine(cfg, prefix)
    if not url:
        return
    try:
        _engine_call(url, "/models/unload", key, {"model": name}, method="POST", timeout=60).close()
        log.write(f"unloaded {name} from the router\n")
    except urllib.error.HTTPError as exc:
        # 400 "model is not running" is the answer when it is already gone.
        log.write(f"unloaded {name} from the router\n" if exc.code == 400
                  else f"could not unload {name} from the router: {exc}\n")
    except Exception as exc:                               # noqa: BLE001
        log.write(f"could not unload {name} from the router: {exc}\n")
    log.flush()


# ---------------------------------------------------------------------------
# Lending a card to a benchmark
# ---------------------------------------------------------------------------
# A model the house does not already run needs room on the benchmark's card,
# and that card is shared with the house's own servers and services. Before
# the run the page measures the card live, frees just enough -- least
# disruptive first -- and gives everything back afterwards:
#
#   1. unload the house's text setups on it; their roles go to the everyday
#      cloud model meanwhile (a detour the assistants read, with a deadline)
#   2. stop audio-cpp (no spoken replies meanwhile)
#   3. stop faster-whisper (no voice input meanwhile)
#   4. unload the vision setup (camera descriptions pause)
#
# Each step is measured before the next: it stops the moment the model fits.
def detours_file() -> Path:
    """The assistants' shared state (`{paths.state}/nanobot-shared`, their
    /shared-state), where they read detours. From the config rather than a
    constant: a default naming one household's disk sent every other
    household's detours to a directory nothing reads."""
    env = os.environ.get("HOME_STACK_SHARED_STATE")
    if env:
        return Path(env) / "model-detours.json"
    state = (load_config().get("paths") or {}).get("state") or "/var/lib/home-stack/state"
    return Path(state) / "nanobot-shared" / "model-detours.json"
CUDA_PROBE_IMAGE = "nvidia/cuda:12.2.0-runtime-ubuntu22.04"
LEND_MARGIN_MIB = 700
LEND_DEADLINE_S = 4 * 3600


def _gpu_live() -> dict[int, dict]:
    """Each card's memory now, read through a throwaway CUDA container -- the
    page's own container has no nvidia-smi. {} when it cannot be read."""
    try:
        out = subprocess.run(["docker", "run", "--rm", "--gpus", "all", CUDA_PROBE_IMAGE,
                              "nvidia-smi", "--query-gpu=index,memory.used,memory.total",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    cards = {}
    for line in out.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 3 and all(x.isdigit() for x in parts):
            cards[int(parts[0])] = {"used": int(parts[1]), "total": int(parts[2])}
    return cards


def _ollama_loaded_mib(port: int) -> dict[str, int]:
    """Model -> MiB on the card, for the Ollama server on *port*."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ps", timeout=3) as r:
            return {m.get("name") or m.get("model"): int(m.get("size_vram") or 0) // 2**20
                    for m in json.load(r).get("models") or []}
    except (OSError, ValueError):
        return {}


def _ollama_load(port: int, model: str, keep_alive) -> None:
    body = {"model": model, "keep_alive": keep_alive}
    if keep_alive != 0:
        body["prompt"] = ""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/generate", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600):
        pass


def _detours_write(gateways: list[str], target: tuple[str, str], reason: str) -> None:
    doc = {"reason": reason, "gateways": {g: {"until": time.time() + LEND_DEADLINE_S,
                                              "model": target[0], "provider": target[1]}
                                          for g in gateways}}
    path = detours_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1))
    tmp.replace(path)


def _detours_clear() -> None:
    with contextlib.suppress(OSError):
        detours_file().unlink()


def _lend_card(cfg: dict, name: str, url: str, log) -> list:
    """Make room for *name* on the benchmark's card. Returns how to undo it.

    Measured, not assumed: the need is the model's estimate at the benchmark
    server's settings, the room is what the card has free right now, and each
    step is measured again before deciding on the next."""
    import ollama_vram
    undo: list = []
    try:
        bench = OI.bench_instance(cfg)
    except OI.InstanceError:
        bench = None
    if not bench or not bench["gpus"]:
        log.write("no benchmark server pinned to a card: nothing is freed\n")
        return undo
    card = bench["gpus"][0]
    facts = _ollama_facts(url, [name]).get(name) or {}
    need = ollama_vram.instance_need(bench, [name], {name: facts}, {})
    need_mib = int(need["bytes"] / 2**20) + LEND_MARGIN_MIB
    live = _gpu_live()
    if card not in live:
        log.write(f"GPU{card} could not be read: nothing is freed\n")
        return undo
    free = live[card]["total"] - live[card]["used"]
    log.write(f"GPU{card}: {free} MiB free, {name} needs ~{need_mib} MiB ({need['how']})\n")
    if free >= need_mib:
        log.write("  it fits: nothing is freed\n")
        return undo

    def measure(step: str) -> int:
        time.sleep(3)
        now = _gpu_live().get(card)
        got = (now["total"] - now["used"]) if now else free
        log.write(f"  {step}: {got} MiB free now\n")
        log.flush()
        return got

    try:
        insts = [i for i in OI.instances(cfg) if i.get("setup") and card in (i["gpus"] or [])]
    except OI.InstanceError:
        insts = []
    everyday = str(((cfg.get("assistant") or {}).get("models") or {}).get("everyday") or "")
    target = model_bench.split_model(everyday) if everyday else ("", "")
    ollama_setups = [i for i in insts if i.get("engine", "ollama") == "ollama"]
    text = [i for i in ollama_setups if i["id"] not in _vision_setups(cfg)]
    vision = [i for i in ollama_setups if i["id"] in _vision_setups(cfg)]

    # 1. The text setups, their roles to the everyday cloud model meanwhile.
    if text and target[0]:
        gateways = [f"http://host.docker.internal:{i['port']}/v1" for i in text]
        _detours_write(gateways, target, f"benchmark of {name}")
        undo.append(("detour", None))
        log.write(f"  roles on {', '.join(i['id'] for i in text)} go to {everyday} meanwhile\n")
        for i in text:
            for model in _ollama_loaded_mib(i["port"]):
                _ollama_load(i["port"], model, 0)
                undo.append(("reload", (i["port"], model)))
        free = measure(f"unloaded {', '.join(i['id'] for i in text)}")
        if free >= need_mib:
            return undo
    # 2-3. The services that hold this card, cheapest loss first.
    on_card = {str(t.get("owner", "")) for t in next(
        (g.get("tenants") or [] for g in _config_dir_json("gpus.json").get("gpus") or []
         if g.get("index") == card), [])}
    for container, what in (("audio-cpp", "spoken replies"), ("faster-whisper", "voice input")):
        if f"container {container}" not in on_card:
            continue
        if subprocess.run(["docker", "stop", container], capture_output=True, timeout=120).returncode == 0:
            undo.append(("start", container))
            free = measure(f"stopped {container} (no {what} meanwhile)")
            if free >= need_mib:
                return undo
    # 4. Vision, last: camera descriptions pause while it is out.
    for i in vision:
        for model in _ollama_loaded_mib(i["port"]):
            _ollama_load(i["port"], model, 0)
            undo.append(("reload", (i["port"], model)))
        free = measure(f"unloaded {i['id']} (camera descriptions pause meanwhile)")
        if free >= need_mib:
            return undo
    log.write(f"  still {need_mib - free} MiB short: the model runs partly on the CPU, "
              f"and its timings say so\n")
    return undo


def _give_card_back(undo: list, log) -> None:
    """Undo _lend_card, last step first. Every step is tried, whatever failed before."""
    for kind, arg in reversed(undo):
        try:
            if kind == "start":
                subprocess.run(["docker", "start", arg], capture_output=True, timeout=120)
                log.write(f"  started {arg} again\n")
            elif kind == "reload":
                _ollama_load(arg[0], arg[1], -1)
                log.write(f"  loaded {arg[1]} again\n")
            elif kind == "detour":
                _detours_clear()
                log.write("  roles back on their own setups\n")
        except Exception as exc:                          # noqa: BLE001
            log.write(f"  could not undo {kind} {arg}: {exc}\n")
        log.flush()


def _vision_setups(cfg: dict) -> set[str]:
    """Setup ids the vision role runs on."""
    value = str(((cfg.get("assistant") or {}).get("models") or {}).get("vision") or "")
    return {local_model(value)[1]} - {"", None} if value else set()


def _house_setup_serving(cfg: dict, prefix: str, name: str) -> str:
    """The id of the house setup that serves *name* now, or "".

    Either the model names the setup outright (`ollama-text:gemma4:e4b`), or
    it is a plain local model (`ollama:gemma4:e4b`) some Ollama setup runs."""
    try:
        insts = [i for i in OI.instances(cfg) if i.get("setup") and i["enabled"]]
    except OI.InstanceError:
        return ""
    for inst in insts:
        if prefix == OI.prefix_of(inst["id"]) and inst["model"] == name:
            return inst["id"]
    if prefix == "ollama":
        for inst in insts:
            if inst["model"] == name and inst.get("engine", "ollama") == "ollama":
                return inst["id"]
    return ""


def _bench_one(model: str, roles: str, repeat: int, effort: str, container: str,
               cfg: dict, log) -> str:
    """One model: pull if local and missing, run, keep the result. Its file name, or ""."""
    BENCH_RESULTS.mkdir(parents=True, exist_ok=True)
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"[^\w.-]+", "_", model)[:80]
    name, prefix = model_bench.split_model(model)
    local = bool(OI.provider_of_prefix(prefix))
    engine = prefix in _ENGINES
    # A model a house setup already serves is tested there, as loaded: a copy
    # on the benchmark's server would be a second load of the same weights,
    # and a llama.cpp setup's model has nowhere else to run at all.
    house = _house_setup_serving(cfg, prefix, name) if local else ""
    if house:
        model = f"{OI.prefix_of(house)}:{name}"
        prefix = OI.prefix_of(house)
    bench_url, bench_inside = _bench_instance(cfg) if local and not house else ("", "")
    url = bench_url or (_bench_ollama_url(cfg, prefix) if local and not house else "")
    where = ("house" if house else "engine" if engine else
             "bench" if bench_url else ("shared" if local else "hosted"))
    saved = ""
    # The run's cases on its header line: two roles on one model (plan steps
    # and sub-agents, both on Bonsai) are two runs, and a Test button follows
    # the one that is its own.
    log.write(f"\n━━ {model}  [{roles}]\n")
    if where == "house":
        log.write(f"on the house's own {house} server, where it is already loaded: nothing else "
                  f"is loaded, and it stays loaded afterwards\n")
    elif where == "bench":
        log.write(f"on the benchmark Ollama ({bench_url}): the family's model is not touched\n")
    elif where == "shared":
        log.write("on the family's Ollama -- no benchmark instance answers; timings will include "
                  "swapping with the house's model\n")
    log.flush()
    lent: list = []
    try:
        if engine and not _bench_engine_up(cfg, prefix, name, _bench_instance(cfg)[0], log):
            return ""
        if url and not _bench_pull(url, name, log):
            if not _bench_live["stop"].is_set():
                log.write("not benchmarked: the pull did not finish.\n")
            return ""
        # A model the house does not run needs room on the benchmark's card.
        if where == "bench":
            lent = _lend_card(cfg, name, url, log)
            log.flush()
        log.write(f"benchmarking inside {container} ...\n")
        log.flush()
        tmp = f"/tmp/{run_id}.json"
        # The benchmark and its cases come from this checkout, copied in per
        # run, so editing a case needs an admin deploy and not a nanobot one --
        # which would cut off whoever is talking to Alfred. The copy baked into
        # the image is the fallback.
        script = "/app/bench/model_bench.py"
        bench_src = ROOT / "services" / "nanobot" / "bench"
        work = f"/tmp/{run_id}-bench"
        if (bench_src / "model_bench.py").is_file():
            copied = subprocess.run(["docker", "cp", str(bench_src) + "/.", f"{container}:{work}"],
                                    capture_output=True, text=True, timeout=60)
            if copied.returncode == 0:
                script = f"{work}/model_bench.py"
        argv = ["docker", "exec", container, "python3", script,
                "--model", model, "--roles", roles, "--repeat", str(repeat), "--out", tmp]
        if effort:
            argv += ["--effort", effort]
        if bench_inside:
            argv += ["--ollama-url", bench_inside]
        _bench_live.update(container=container, run_id=run_id)
        _bench_save_state(run_id=run_id)
        proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, text=True)
        _bench_live["proc"] = proc
        # Polled rather than waited on, so a Stop pressed in another admin
        # process -- which cannot reach this Popen -- still ends it.
        while proc.poll() is None:
            if _bench_stopping():
                _bench_kill(container, run_id, proc)
            time.sleep(1)
        code = proc.returncode
        _bench_live["proc"] = None
        if code == 0:
            got = subprocess.run(["docker", "exec", container, "cat", tmp],
                                 capture_output=True, text=True, timeout=30)
            if got.returncode == 0 and got.stdout.strip():
                try:
                    doc = json.loads(got.stdout)
                    doc["where"] = where
                    doc["container_name"] = container
                    # The run just hashed this container's live setup, so it
                    # is also the container's current digest: no 5 s exec to
                    # find out what the page already knows.
                    if doc.get("setup_digest"):
                        _remember_setup_digest(container, str(doc["setup_digest"]))
                    text = json.dumps(doc, ensure_ascii=False, indent=1)
                except ValueError:
                    text = got.stdout
                (BENCH_RESULTS / f"{run_id}.json").write_text(text, encoding="utf-8")
                saved = f"{run_id}.json"
                # The last run of a model is its result: earlier ones go.
                dropped = model_bench.supersede(BENCH_RESULTS, model, saved)
                if dropped:
                    log.write(f"replaces {len(dropped)} earlier result(s) for {model}\n")
        elif not _bench_live["stop"].is_set():
            log.write(f"\nthe benchmark exited with {code}\n")
        subprocess.run(["docker", "exec", container, "rm", "-rf", tmp, work],
                       capture_output=True, timeout=30)
    finally:
        # Give the card back: a tested model otherwise stays resident for
        # Ollama's keep_alive, beside the family's everyday model.
        if url:
            _bench_unload(url, name, log)
        if lent:
            _give_card_back(lent, log)
        if engine:
            _bench_engine_down(cfg, prefix, name, log)
    return saved


def _bench_unload(url: str, name: str, log) -> bool:
    """Unload *name* from the Ollama at *url*, and check that it went.

    `keep_alive: 0` with no prompt is Ollama's unload. The call returning is
    not the model being gone -- it answers before the release, and a model
    another request holds is kept -- so `/api/ps` is read until it is, or the
    log says it still sits there. Done as each model finishes, not when the
    queue is: a model that came before is not wanted on the card during the
    next one's timings.
    """
    try:
        _bench_ollama(url, "/api/generate", {"model": name, "keep_alive": 0}, timeout=30).close()
    except Exception as exc:                                  # noqa: BLE001
        log.write(f"could not unload {name}: {type(exc).__name__}: {exc}\n")
        log.flush()
        return False
    for _ in range(10):
        try:
            with _bench_ollama(url, "/api/ps", method="GET", timeout=10) as r:
                loaded = {m.get("name") for m in (json.load(r).get("models") or [])}
        except Exception:                                     # noqa: BLE001
            break
        if name not in loaded:
            log.write(f"unloaded {name}\n")
            log.flush()
            return True
        time.sleep(1)
    log.write(f"{name} is still loaded on {url} after the unload was asked for\n")
    log.flush()
    return False


def _run_bench_job(models: list[str], roles: str, repeat: int, effort: str,
                   container: str, cfg: dict, lock_fd, log_path: Path) -> None:
    saved: list[str] = []
    try:
        with log_path.open("w") as log:
            batch = {"models": list(models), "roles": roles, "repeat": repeat,
                     "effort": effort, "container": container}
            while batch and not _bench_stopping():
                ms = batch["models"]
                _bench_save_state(models=ms, roles=batch["roles"].split(","))
                for i, model in enumerate(ms, 1):
                    if _bench_stopping():
                        break
                    _bench_save_state(model=model, index=i)
                    result = _bench_one(model, batch["roles"], batch["repeat"], batch["effort"],
                                        batch["container"], cfg, log)
                    if result:
                        saved.append(result)
                        state = _bench_state()
                        _bench_save_state(results=(state.get("results") or []) + [result])
                # Whatever was added while this ran, in order. Stop empties it.
                batch = None if _bench_stopping() else _bench_queue_pop()
                if batch:
                    log.write("\n── next in the queue\n")
            if _bench_stopping():
                _bench_queue_write([])
                log.write("\nstopped.\n")
    finally:
        stopped = _bench_stopping()
        _bench_save_state(running=False, finished=time.time(), stopped=stopped,
                          failed=not saved and not stopped, results=saved)
        with contextlib.suppress(Exception):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def _bench_reply(ok: bool, status: int = 200, **payload):
    """JSON for the page's script; for a plain form post, a flash and the page.

    A JSON body shown as the page is what somebody got when the script had
    died: `{"ok":false,"reason":"busy"}` where a sentence and the card should be.
    """
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=ok, **payload), status
    reason = payload.get("reason", "")
    if payload.get("queued"):
        reason = _t_or("admin.models.bench_queued", "Queued behind the running benchmark.")
    flash(reason or _t_or("admin.models.bench_running", "Running"), "ok" if ok else "warn")
    return redirect(url_for("models_catalog") + "#bench")


@app.post("/models/bench/start")
def bench_start():
    cfg = load_config()
    lines = [x.strip() for x in re.split(r"[\n,]+", request.form.get("model", "")) if x.strip()]
    if not lines:
        return _bench_reply(False, 400, reason="name a model first.")
    models, notes = [], []
    for line in lines[:6]:
        try:
            model, note = model_bench.resolve_model(line)
        except model_bench.ResolveError as exc:
            return _bench_reply(False, 400, reason=f"{line}: {exc}")
        models.append(model)
        notes.append(f"{model}  ({note})" if note else model)
    roles = [r for r in request.form.getlist("roles") if r in model_bench.ROLES]
    if not roles:
        return _bench_reply(False, 400, reason="choose at least one role.")
    repeat = max(1, min(3, request.form.get("repeat", 1, type=int) or 1))
    effort = request.form.get("effort", "")
    if effort not in model_bench.EFFORTS:
        effort = ""
    launched = _bench_launch(models, roles, repeat, effort, request.form.get("container", ""), cfg)
    if not launched.get("ok"):
        return _bench_reply(False, 409, reason=launched.get("reason", ""))
    return _bench_reply(True, models=models, notes=notes, container=launched["container"],
                        queued=launched.get("queued", False), position=launched.get("position"))


def _bench_launch(models: list[str], roles: list[str], repeat: int, effort: str,
                  container: str, cfg: dict) -> dict:
    """Start a benchmark run, or queue it behind the one running. The one way
    a run begins -- the Benchmark card and each role's Test button use it."""
    running = bench_containers()
    if container not in running:
        container = running[0] if running else ""
    if not container:
        return {"ok": False, "reason": "no assistant container is running."}
    for d in (BENCH_DIR, BENCH_RESULTS, BENCH_LOGS):
        d.mkdir(parents=True, exist_ok=True)
    # One run at a time *across processes*: the lock is a file, held by the
    # job until it ends, so a second admin process -- or a restart racing an
    # old thread -- is told "busy" instead of starting a second run beside it.
    lock_fd = os.open(BENCH_LOCK, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Somebody's run is going: this one waits its turn rather than being
        # refused. The running job drains the queue before it lets the lock go.
        os.close(lock_fd)
        entries = _bench_queue() + [{"models": models, "roles": ",".join(roles), "repeat": repeat,
                                     "effort": effort, "container": container}]
        _bench_queue_write(entries)
        return {"ok": True, "container": container, "queued": True, "position": len(entries)}
    for old in sorted(BENCH_LOGS.glob("*.log"))[:-20]:
        with contextlib.suppress(OSError):
            old.unlink()
    log_path = BENCH_LOGS / (time.strftime("%Y%m%d-%H%M%S") + ".log")
    _bench_live["stop"].clear()
    _bench_save_state(running=True, stopped=False, stop_requested=False, failed=None,
                      models=models, model=models[0],
                      index=1, started=time.time(), finished=None, results=[],
                      log=log_path.name, container=container, roles=roles)
    threading.Thread(target=_run_bench_job,
                     args=(models, ",".join(roles), repeat, effort, container, cfg,
                           lock_fd, log_path),
                     daemon=True).start()
    return {"ok": True, "container": container, "queued": False}


def _bench_kill(container: str, run_id: str, proc=None) -> None:
    """End a run's benchmark: the `docker exec` client, and the process inside."""
    with contextlib.suppress(Exception):
        if proc is not None:
            proc.terminate()
    # Killing the client leaves the benchmark running inside the container,
    # still on the GPU; its --out path carries the run id, so that is the handle.
    if container and run_id:
        with contextlib.suppress(Exception):
            subprocess.run(["docker", "exec", container, "pkill", "-f", run_id],
                           capture_output=True, timeout=15)


@app.post("/models/bench/stop")
def bench_stop():
    """Stop now: the pull in flight, the benchmark in the container, the queue.

    Works from any admin process: the request is written to the job's state
    file, which the job checks while it pulls, between models and every second
    while a benchmark runs; and the benchmark inside the container is killed
    here as well, by the run id the state file names.
    """
    state = _bench_state()
    if not state.get("running"):
        return _bench_reply(True, running=False)
    _bench_save_state(stop_requested=True)
    _bench_live["stop"].set()
    with contextlib.suppress(Exception):
        if _bench_live["pull"] is not None:
            _bench_live["pull"].close()          # Ollama drops a pull with no client
    _bench_kill(state.get("container", ""), state.get("run_id", ""), _bench_live["proc"])
    return _bench_reply(True, running=True, stopping=True)


@app.get("/models/bench/log")
def bench_log():
    """Same contract as /deploy/log, so the page polls it the same way."""
    state = _bench_state()
    offset = max(0, request.args.get("offset", 0, type=int))
    text = ""
    log_path = BENCH_LOGS / Path(str(state.get("log") or "none")).name
    if log_path.exists():
        with log_path.open() as fh:
            fh.seek(offset)
            text = fh.read()
            offset = fh.tell()
    started = state.get("started")
    return jsonify(
        offset=offset, lines=text.splitlines(), running=bool(state.get("running")),
        log=log_path.name,
        failed=state.get("failed"), stopped=state.get("stopped"),
        model=state.get("model", ""), index=state.get("index", 0),
        count=len(state.get("models") or []), results=state.get("results") or [],
        queue=[m for e in _bench_queue() for m in e.get("models") or []],
        elapsed=round((state.get("finished") or time.time()) - started, 1) if started else 0,
    )


def _cases_digest() -> str:
    """The digest of the cases file a run started now would be scored by."""
    try:
        return hashlib.sha256(
            (ROOT / "services" / "nanobot" / "bench" / "cases.json").read_bytes()
        ).hexdigest()[:12]
    except OSError:
        return ""


# Ollama's own parameter counts, so a row whose name carries no size still
# shows one. Cached for a few minutes: this runs on every render of the Models
# page, and two servers answering /api/tags is not something to do per paint.
_PARAMS_CACHE: dict = {"at": 0.0, "map": {}}
_PARAMS_TTL = 300


def _ollama_params(cfg: dict) -> dict:
    """`{model name: "12.2B"}` from every Ollama this house can reach.

    Asked of the servers rather than parsed from the name, because the name is
    where the information was missing in the first place -- `ollama:gemma4` is
    a real row and says nothing about its size. The benchmark instance is asked
    first: it is where candidates are pulled, so it knows models the family's
    instance has never seen.
    """
    now = time.time()
    if _PARAMS_CACHE["map"] and now - _PARAMS_CACHE["at"] < _PARAMS_TTL:
        return _PARAMS_CACHE["map"]
    out: dict = {}
    urls = []
    bench_url, _inside = _bench_instance(cfg)
    if bench_url:
        urls.append(bench_url)
    for source, ep in model_endpoints(cfg).items():
        u = (ep.get("url") or "").rstrip("/") if model_catalogue.is_local_ollama(source) else ""
        if u and u not in urls:
            urls.append(u)
    for url in urls:
        try:
            with _bench_ollama(url, "/api/tags", method="GET", timeout=4) as r:
                doc = json.loads(r.read().decode() or "{}")
        except Exception:                                  # noqa: BLE001
            continue                                       # a server that is down costs nothing
        for m in doc.get("models") or []:
            name = m.get("name") or m.get("model") or ""
            size = ((m.get("details") or {}).get("parameter_size") or "").strip()
            if name and size:
                out.setdefault(name, size)
                out.setdefault(name.lower(), size)
    if out:
        _PARAMS_CACHE.update(at=now, map=out)
    return out or _PARAMS_CACHE["map"]


# ---------------------------------------------------------------------------
# The household's Ollama servers (cloud.ollama.instances)
# ---------------------------------------------------------------------------
# The page edits the list and shows what it costs on each card. Applying it is
# the host's job: this container cannot reach systemd, so Apply drops a request
# next to the config and a root .path unit there runs
# `deploy/ollama_host.py --from-trigger`, which applies the *saved* list and
# writes back the plan, the cards and a log (see that file).
OLLAMA_FACTS = Path(os.environ.get("HOME_STACK_CACHE", "/cache")) / "ollama-facts.json"
OLLAMA_MEASURED = Path(os.environ.get("HOME_STACK_CACHE", "/cache")) / "ollama-vram.json"
OLLAMA_CONTEXTS = (2048, 4096, 8192, 16384, 32768, 40960, 65536, 98304, 131072, 262144)


def _safe_serving(cfg: dict) -> list[dict]:
    try:
        return OI.serving(cfg)
    except OI.InstanceError:
        return []


# The other things on the cards, chosen beside the Ollama servers because they
# compete for the same memory: (service, what nvidia-smi calls it on the host,
# whether it can run on the CPU). audio.cpp has a CPU build (`gpu: false`); the
# camera wall's detectors do not, so its choice is a card.
GPU_TENANTS = (
    ("audio-cpp", "container audio-cpp", True),          # text to speech
    ("faster-whisper", "container faster-whisper", True),  # speech to text
    ("home-cameras", "container home-cameras-web", False),
)
# What each takes on a card when the host has never seen it on one -- moved
# off the CPU, say. Measured here where there is a figure: audio.cpp with
# Supertonic 0.57 GB (2.2 with the ASR packages), the camera wall 0.84-1.28
# GB (docs/local-ollama.md); whisper large-v3-turbo at float16 about 1.6 GB.
# Counted as an estimate until the host's next look replaces it.
TENANT_TYPICAL_MIB = {"audio-cpp": 600, "faster-whisper": 1700, "home-cameras": 1300}


def _tenant_choice(cfg: dict, svc: str) -> str:
    """"0", "1", ... or "cpu"; "all" when the service may use any card."""
    conf = (cfg.get("services") or {}).get(svc) or {}
    if svc == "audio-cpp" and conf.get("gpu") is False:
        return "cpu"
    if svc == "faster-whisper" and str(conf.get("device") or "cuda") == "cpu":
        return "cpu"
    dev = conf.get("gpu_device")
    return str(dev) if dev not in (None, "") else "all"


def _set_tenant(conf: dict, svc: str, pick: str) -> None:
    """Write a card choice into the service's own block."""
    if svc == "audio-cpp":
        conf["gpu"] = pick != "cpu"
    elif svc == "faster-whisper":
        # float32 on the CPU, not int8: int8 mis-heard the voice round trip
        # outright (config/home-stack.example.yml says how).
        conf["device"], conf["compute_type"] = (("cpu", "float32") if pick == "cpu"
                                                else ("cuda", "float16"))
    if pick != "cpu":
        conf["gpu_device"] = pick


def _gpus_with_tenants(cfg: dict) -> list[dict]:
    """The host's last look at the cards, with audio.cpp and the cameras moved
    to the card this config gives them -- so the bars and the placement count
    them where they will be after their next deploy, not where they were."""
    import copy
    gpus = copy.deepcopy(_config_dir_json("gpus.json").get("gpus") or [])
    by_index = {g["index"]: g for g in gpus}
    for svc, owner, _cpu in GPU_TENANTS:
        choice = _tenant_choice(cfg, svc)
        if choice == "all":
            continue                    # any card: counted where it was seen
        moved = []
        for g in gpus:
            keep = []
            for t in g.get("tenants") or []:
                (moved if str(t.get("owner", "")).startswith(owner) else keep).append(t)
            g["tenants"] = keep
        if choice.isdigit() and int(choice) in by_index:
            by_index[int(choice)]["tenants"] += moved or [
                {"owner": owner, "mib": TENANT_TYPICAL_MIB.get(svc, 1000), "estimated": True}]
    return gpus


def _config_dir_json(name: str) -> dict:
    try:
        return json.loads((CONFIG.parent / name).read_text())
    except (OSError, ValueError):
        return {}


def _ollama_users(cfg: dict) -> dict[str, list[tuple[str, str]]]:
    """instance id -> [(what uses it, model)], from every model setting."""
    out: dict[str, list[tuple[str, str]]] = {}
    models = (cfg.get("assistant") or {}).get("models") or {}
    for role, value in models.items():
        for v in (value if isinstance(value, list) else [value]):
            canon, iid = local_model(str(v or ""))
            if canon.startswith("ollama:"):
                out.setdefault(iid or "main", []).append((role, canon.split(":", 1)[1]))
    emb = str(((cfg.get("services") or {}).get("home-paperless") or {}).get("embeddings") or "")
    canon, iid = local_model(emb)
    if canon.startswith("ollama:"):
        out.setdefault(iid or "main", []).append(("paperless embeddings", canon.split(":", 1)[1]))
    return out


def _ollama_facts(url: str, names: list[str]) -> dict[str, dict]:
    """Each model's metadata, cached: a model's shape does not change, and
    asking every instance on every page load is what made this page slow once."""
    import ollama_vram
    cache = ollama_vram.load_measured(OLLAMA_FACTS)
    missing = [n for n in names if n not in cache]
    found = False
    # A llama.cpp setup's hf: file: its metadata straight from Hugging Face,
    # by range request -- sized before anybody downloads 7 GB. Cached like the
    # rest: that is megabytes from the internet, and every page load asks.
    for n in [n for n in missing if OI.HF_RE.fullmatch(n)]:
        m = OI.HF_RE.fullmatch(n)
        f = ollama_vram.gguf_facts(
            f"https://huggingface.co/{m.group(1)}/{m.group(2)}/resolve/main/{m.group(3)}")
        if f and not f.get("error"):
            cache[n], found = f, True
    missing = [n for n in missing if not OI.HF_RE.fullmatch(n)]
    # A model Ollama pulled from Hugging Face (hf.co/owner/repo:TAG): when
    # Ollama cannot describe it -- a packing it does not run, like Bonsai's
    # PTQ1_0 -- the same file is read from Hugging Face by its tag.
    for n in [n for n in missing if n.startswith("hf.co/")]:
        f = _hf_tag_facts(n)
        if f:
            cache[n] = f
    missing = [n for n in missing if n not in cache]
    if missing and url:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=4) as r:
                tags = {m.get("name"): m.get("size") for m in (json.load(r).get("models") or [])}
        except (OSError, ValueError):
            tags = {}
        for n in missing:
            f = ollama_vram.model_facts(url, n, tags)
            if f:
                cache[n], found = f, True
    if found:
        with contextlib.suppress(OSError):
            ollama_vram.save_measured(OLLAMA_FACTS, cache)
    return {n: cache[n] for n in names if n in cache}


def _cpu_spill(inst: dict) -> int:
    """MiB of an Ollama setup's loaded model that run on the CPU, 0 when all of
    it is on the card (or it is not loaded, or not Ollama's).

    Ollama decides the split when it loads, from what is free at that moment:
    loaded while a service on the same card was restarting, qwen3.5:4b ran
    1.5 of its 4 GiB on the CPU for good, and every notification turn with it
    (2026-09-24). A reload, once the card has room, puts it back whole."""
    if not inst or inst.get("engine", "ollama") != "ollama" or not inst.get("port"):
        return 0
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{inst['port']}/api/ps", timeout=2) as r:
            models = json.load(r).get("models") or []
    except (OSError, ValueError):
        return 0
    for m in models:
        if (m.get("name") or m.get("model")) in (inst.get("model"), f"{inst.get('model')}:latest"):
            return max(0, int(m.get("size") or 0) - int(m.get("size_vram") or 0)) // 2**20
    return 0


@app.post("/models/ollama/reload")
def ollama_reload():
    """Unload a setup's model and load it again, whole on its card if it fits now."""
    sid = (request.form.get("id") or "").strip()
    inst = next((i for i in OI.instances(load_config()) if i["id"] == sid and i.get("setup")), None)
    if not inst or inst.get("engine", "ollama") != "ollama":
        return jsonify(ok=False, message="no such Ollama setup"), 400
    base = f"http://127.0.0.1:{inst['port']}"
    try:
        for body, wait in (({"model": inst["model"], "keep_alive": 0}, 60),
                           ({"model": inst["model"], "prompt": "", "keep_alive": -1}, 600)):
            req = urllib.request.Request(base + "/api/generate", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=wait):
                pass
    except (OSError, ValueError) as exc:
        return jsonify(ok=False, message=str(exc)[:200])
    left = _cpu_spill(inst)
    return jsonify(ok=True, message=(
        _t_or("admin.models.ollama_reloaded_whole", "Reloaded: all of it on the card.") if not left else
        _t_or("admin.models.ollama_reloaded_partly", "Reloaded, still {mib} MiB on the CPU: the card "
              "has no more room -- lower its window or slots, or move something off it.", mib=left)))


def _hf_tag_facts(name: str) -> dict:
    """GGUF facts for `hf.co/owner/repo:TAG`: the repo's .gguf file whose name
    carries the tag (not a vision projector), read by range request."""
    import ollama_vram
    m = re.fullmatch(r"hf\.co/([\w.\-]+)/([\w.\-]+)(?::([\w.\-]+))?", name)
    if not m:
        return {}
    owner, repo, tag = m.group(1), m.group(2), (m.group(3) or "").lower()
    try:
        with urllib.request.urlopen(f"https://huggingface.co/api/models/{owner}/{repo}", timeout=15) as r:
            files = [x.get("rfilename", "") for x in (json.load(r).get("siblings") or [])]
    except (OSError, ValueError):
        return {}
    ggufs = [f for f in files if f.endswith(".gguf") and "mmproj" not in f.lower()]
    pick = [f for f in ggufs if tag and tag in f.lower()] or (ggufs if len(ggufs) == 1 else [])
    if not pick:
        return {}
    f = ollama_vram.gguf_facts(f"https://huggingface.co/{owner}/{repo}/resolve/main/{pick[0]}")
    return f if f and not f.get("error") else {}


def _ollama_card(cfg: dict) -> dict:
    """Everything the instances card draws."""
    import ollama_vram
    card = {"error": "", "instances": [], "gpus": [], "gpu_view": [], "contexts": OLLAMA_CONTEXTS,
            "removed": [],
            "kv_types": OI.KV_TYPES, "listed": False, "trigger": "missing",
            "plan_at": "", "gpus_at": "", "log": "", "requested": False, "unpinned": []}
    conf = (cfg.get("cloud") or {}).get("ollama") or {}
    card["listed"] = conf.get("instances") is not None
    try:
        insts = OI.instances(cfg, enabled_only=False)
    except OI.InstanceError as exc:
        card["error"] = str(exc)
        return card
    inv = _config_dir_json("gpus.json")
    plan = {r["id"]: r for r in (_config_dir_json("ollama-plan.json").get("instances") or [])}
    marker = _config_dir_json("ollama-trigger.json")
    card["trigger"] = ("ok" if marker.get("version") == OI.HELPER_VERSION
                       else "stale" if marker else "missing")
    card["requested"] = (CONFIG.parent / "ollama-apply.request").exists()
    with contextlib.suppress(OSError):
        card["log"] = (CONFIG.parent / "ollama-apply.log").read_text()[-4000:]
    at = _config_dir_json("ollama-plan.json").get("at")
    card["plan_at"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(at)) if at else ""
    card["gpus_at"] = (time.strftime("%Y-%m-%d %H:%M", time.localtime(inv["at"]))
                       if inv.get("at") else "")
    card["gpus"] = _gpus_with_tenants(cfg)
    card["tenants"] = [{"svc": svc, "choice": _tenant_choice(cfg, svc), "cpu": cpu,
                        "label": _t_or(f"admin.models.tenant_{svc.replace('-', '_')}", svc)}
                       for svc, _owner, cpu in GPU_TENANTS
                       if svc in ((cfg.get("services") or {}))]
    users = _ollama_users(cfg)
    measured = ollama_vram.load_measured(OLLAMA_MEASURED)
    needs = _ollama_needs(cfg, insts, users, measured)
    for inst in insts:
        used = users.get(inst["id"], [])
        need = needs[inst["id"]]
        row = plan.get(inst["id"]) or {}
        card["instances"].append({
            **inst, "display": OI.display(inst), "prefix": OI.prefix_of(inst["id"]),
            "used_by": used, "need": need, "need_gib": round(need["bytes"] / ollama_vram.GIB, 1),
            # Saved since the host last looked: to apply, whatever it said then.
            "state": ("pending" if inst["managed"] and row.get("signature")
                      and row["signature"] != OI.signature(inst)
                      else row.get("state") or ("unmanaged" if not inst["managed"] else "unknown")),
            "changes": row.get("changes") or [],
            "measured_at": (time.strftime("%Y-%m-%d %H:%M", time.localtime(
                (measured.get(inst["id"]) or {}).get("at")))
                if (measured.get(inst["id"]) or {}).get("at") else ""),
            "removable": inst["id"] != "main" and not used and inst["purpose"] != "bench",
        })
    enabled = [i for i in insts if i["enabled"]]
    card["gpu_view"] = _gpu_view(card["gpus"], enabled, needs)
    # An "Auto" server with no card is one the save could not place (the
    # cards were not known then): as unpinned as one set that way.
    card["unpinned"] = [i["id"] for i in enabled if not i["gpus"] and not i.get("cpu")]
    catalogue = model_catalogue.load_cache(MODELS_CACHE)
    # What Ollama has *now*, asked live: the catalogue is refreshed with the
    # price check (weekly, here), so a model pulled since was missing from the
    # list for days -- qwen3.5:4b, 2026-09-24. /api/tags is local and takes
    # milliseconds; the cached roster is the fallback when it does not answer.
    cached = {m["id"].split(":", 1)[1] for m in catalogue.get("ollama") or []
              if str(m.get("id", "")).startswith("ollama:")}
    live: set[str] = set()
    main_url = (model_endpoints(cfg).get("ollama") or {}).get("url", "")
    if main_url:
        with contextlib.suppress(OSError, ValueError):
            with urllib.request.urlopen(main_url.rstrip("/") + "/api/tags", timeout=2) as r:
                live = {m.get("name") for m in (json.load(r).get("models") or []) if m.get("name")}
    library_ids = {m.get("id") for m in (_config_dir_json(LIBRARY_FILE).get("models") or [])
                   if m.get("id")}
    card["local_models"] = sorted((live or cached) | library_ids)
    card["next_port"] = _next_ollama_port(insts)
    card["library"] = _library_view(cfg)
    # The engines a setup may run on; llama.cpp only once it is built on the
    # host (./home-stack llamacpp build writes llamacpp-builds.json).
    card["engines"] = list(OI.ENGINE_LABELS.items())
    builds = _config_dir_json("llamacpp-builds.json")
    card["engines_built"] = [e for e, flavor in (("llamacpp", "vanilla"), ("prism", "prism"))
                             if builds.get(flavor)]
    card["blank"] = {**OI.DEFAULTS, "id": "", "context": 8192, "gpu_mode": "auto",
                     "pinned": True, "managed": True, "port": 0}
    card["tasks"] = _ollama_tasks(cfg, insts, catalogue)
    # The setups, used or not. A used one is also a server above; an unused
    # one runs nowhere, and its size is what it would take if a task used it.
    by_inst = {i["id"]: i for i in card["instances"]}
    card["setups"] = []
    try:
        all_setups = OI.setups(cfg)
    except OI.InstanceError as exc:
        card["error"] = str(exc)
        all_setups = []
    main_url = (model_endpoints(cfg).get("ollama") or {}).get("url", "")
    for st in all_setups:
        inst = by_inst.get(st["id"])
        if inst is None:
            # Its engine too: a llama.cpp setup's model (a .gguf path) is no
            # Ollama name, and its server carries llama.cpp's own overhead.
            probe = OI.normalize({"id": st["id"], "port": 1, "model": st["model"],
                                  "context": st["context"], "parallel": st["parallel"],
                                  "kv_cache": st["kv_cache"], "max_models": 1,
                                  "engine": st["engine"], "thinking": st["thinking"]})
            need = ollama_vram.instance_need(probe, [st["model"]],
                                             _ollama_facts(main_url, [st["model"]]),
                                             measured.get(st["id"]) or {})
        else:
            need = inst["need"]
        card["setups"].append({
            **st, "label": setup_label(st), "used": inst is not None,
            "used_by": [r for r, _m in users.get(st["id"], [])],
            "state": inst["state"] if inst else "unused", "changes": inst["changes"] if inst else [],
            "placed": (inst or {}).get("gpus") or [],
            "need": need, "need_gib": round(need["bytes"] / ollama_vram.GIB, 1),
            "measured_at": (inst or {}).get("measured_at", ""),
            "cpu_mib": _cpu_spill(inst) if inst else 0})
    card["setup_blank"] = {"id": "", "name": "", "model": "", "context": 8192, "kv_cache": "q4_0",
                           "parallel": 1, "gpu": "auto"}
    card["instances"] = [i for i in card["instances"] if not i.get("setup")]
    # Servers the host still runs for this stack and the list no longer has:
    # the next Apply stops them (ollama_host.py plan). From the host's last
    # look, plus anything managed there that a save since has taken off.
    listed = {i["unit"] for i in insts}
    card["removed"] = sorted({r["unit"] for r in plan.values()
                              if r.get("managed") and r.get("unit") not in listed
                              and r.get("unit") != "ollama"})
    return card


def _gpu_view(gpus: list[dict], enabled: list[dict], needs: dict, measured: bool = True) -> list[dict]:
    """The cards as the page draws them: each one's tenants and servers, in GiB
    and as a share of the card. The card and the preview both use this."""
    import ollama_vram
    names = {i["id"]: OI.display(i) for i in enabled}
    # What a person reads in the card's rows: the setup's own name, and what
    # it runs, short -- the full display() string stays as the hover text.
    # A general server (not a setup) is said to be one: the main one was
    # labelled "Text", like the Text setup, and the card showed two "Text"s.
    labels = {i["id"]: ((i["label"] or i["id"]) if i.get("setup") else
                        _t_or("admin.models.ollama_general_server", "{name} (general server)",
                              name=i["label"] or i["id"]))
              for i in enabled}
    models = {i["id"]: " · ".join(x for x in (
        OI.short_model(i["model"]) if i.get("model") else "",
        OI.ENGINE_LABELS.get(i.get("engine", "ollama"), "") if i.get("engine", "ollama") != "ollama" else "")
        if x) for i in enabled}
    return [
        {**g, "total_gib": round(g["total"] / ollama_vram.GIB, 1),
         "free_gib": round(g["free"] / ollama_vram.GIB, 1),
         "used_gib": round((g["total"] - g["free"]) / ollama_vram.GIB, 1),
         "over_gib": round(max(0, -g["free"]) / ollama_vram.GIB, 1),
         "others": [{**o, "gib": round(o["bytes"] / ollama_vram.GIB, 2),
                     "pct": round(o["bytes"] * 100 / g["total"], 1)} for o in g["others"]],
         "instances": [{**r, "gib": round(r["bytes"] / ollama_vram.GIB, 1), "name": names.get(r["id"], r["id"]),
                        "label": labels.get(r["id"], r["id"]), "model": models.get(r["id"], ""),
                        "pct": round(r["bytes"] * 100 / g["total"], 1)} for r in g["instances"]]}
        for g in ollama_vram.gpu_view(gpus, enabled, needs, measured=measured)]


def _ollama_needs(cfg: dict, insts: list[dict], users: dict | None = None,
                  measured: dict | None = None) -> dict[str, dict]:
    """Each server's estimated share (ollama_vram.instance_need), by id.

    One copy for the card's bars and the save's placement, so a server is
    placed by the same number the page then draws it with.
    """
    import ollama_vram
    endpoints = model_endpoints(cfg)
    users = _ollama_users(cfg) if users is None else users
    measured = ollama_vram.load_measured(OLLAMA_MEASURED) if measured is None else measured
    main_url = (endpoints.get("ollama") or {}).get("url", "")
    needs = {}
    for inst in insts:
        names = ([inst["model"]] if inst.get("model")
                 else sorted({m for _r, m in users.get(inst["id"], [])}))
        # A llama.cpp server has no /api/show: its model's facts come from
        # Ollama's (the same GGUF) or from the file itself (_ollama_facts).
        url = main_url if inst.get("engine", "ollama") != "ollama" else \
            (endpoints.get(OI.provider_of(inst["id"])) or {}).get("url") or main_url
        needs[inst["id"]] = ollama_vram.instance_need(
            inst, names, _ollama_facts(url, names), measured.get(inst["id"]) or {})
    return needs


def _next_ollama_port(insts: list[dict], start: int = 11437) -> int:
    used = {i["port"] for i in insts}
    port = start
    while port in used:
        port += 1
    return port


def setup_label(st: dict) -> str:
    """"Chat — gemma4:e4b · 48k · q4_0 × 2": what a task's picker shows."""
    ctx = st["context"]
    ctx_s = f"{ctx // 1024}k" if ctx % 1024 == 0 else str(ctx)
    body = f"{OI.short_model(st['model'])} · {ctx_s} · {st['kv_cache']} × {st['parallel']}"
    if st.get("engine", "ollama") != "ollama":
        body += f" · {OI.ENGINE_LABELS[st['engine']]}"
    return f"{st['name']} — {body}" if st.get("name") else body


def _ollama_tasks(cfg: dict, insts: list[dict], catalogue: dict) -> list[dict]:
    """The task table: each role on "Local Ollama", and the setups it may use.

    Every setup whose model can do the role is offered, used or not: picking
    one is what starts its server on the next apply. The benchmark's score for
    the role's kind is beside each. A role still on a general server keeps
    that as its current option until a setup is picked.
    """
    every = {m["id"]: m for m in model_catalogue.all_models(catalogue)}
    best = _bench_local_best(_bench_views(cfg))
    models = (cfg.get("assistant") or {}).get("models") or {}
    try:
        all_setups = OI.setups(cfg)
    except OI.InstanceError:
        all_setups = []
    out = []
    todo = []
    for persona in model_catalogue.PERSONA_NEEDS:
        value = models.get(persona)
        if isinstance(value, str):
            todo.append((persona, persona, value, ""))
        elif isinstance(value, list):
            # An ordered chain: one row per local place, "Rescue · 1".
            for i, v in enumerate(value):
                todo.append((f"{persona}-{i}", persona, str(v), f" · {i + 1}"))
    for task_id, persona, value, suffix in todo:
        canon, iid = local_model(value)
        if not canon.startswith("ollama:"):
            continue
        iid = iid or "main"
        kind = ROLE_BENCH_KIND.get(persona, "")
        scores = {b["id"]: f"{b['passed']}/{b['total']}" for b in best.get(kind) or []}
        options = []
        for st in all_setups:
            m = every.get(f"ollama:{st['model']}")
            # A llama.cpp setup's hf: file is in no catalogue: what it can do
            # is the household's call, so it is offered rather than hidden.
            if m is None and st.get("engine", "ollama") != "ollama":
                m = {"id": st["model"], "tools": True}
            if not _meets_role(persona, m or {"id": st["model"]}):
                continue
            score = scores.get(f"ollama:{st['model']}")
            options.append({"value": st["id"], "selected": st["id"] == iid,
                            "text": setup_label(st) + (f" — {score}" if score else "")})
        if not any(o["selected"] for o in options):
            inst = next((i for i in insts if i["id"] == iid), None)
            options.insert(0, {"value": "", "selected": True,
                               "text": f"{canon.split(':', 1)[1]} · "
                                       + (OI.display(inst) if inst else iid)})
        out.append({"id": task_id, "options": options,
                    "label": _t_or(f"admin.models.role_{persona}",
                                   (model_catalogue.PERSONA_NEEDS[persona] or {}).get("label", persona))
                             + suffix})
    return out


def _form_setups(form, cfg: dict) -> tuple[list[dict], list[str]]:
    """The setups rows: name, model, window, cache, slots, and a card or Auto.

    A new row needs only a model; its id comes from its name, or the model's.
    An existing one keeps its port and where it was placed, so saving does not
    renumber or move a server that is fine.
    """
    errors, out = [], []
    try:
        old = {st["id"]: st for st in OI.setups(cfg)}
    except OI.InstanceError:
        old = {}
    raw = ((cfg.get("cloud") or {}).get("ollama") or {}).get("instances") or []
    taken_ids = set(old) | {str(e.get("id")) for e in raw if isinstance(e, dict)}
    used = OI.used_setup_ids(cfg, set(old))
    count = min(int(form.get("su-count") or 0), 64)
    for n in range(count + 1):                  # the last row is "add one"
        f = lambda k, d="": (form.get(f"su-{n}-{k}") or d).strip()      # noqa: E731
        sid, model, name = f("id"), f("model"), f("name")
        if model == "__other__":                # the picker's "Other": typed by hand
            model = f("model-other")
        if not sid:
            if not model:
                continue
            base = re.sub(r"[^a-z0-9]", "", (name or model.split("/")[-1]).lower()) or "setup"
            base = base if base[0].isalpha() else "m" + base
            sid, k = base[:14], 2
            while sid in taken_ids or any(e["id"] == sid for e in out):
                sid, k = f"{base[:13]}{k}", k + 1
        if form.get(f"su-{n}-remove") == "on":
            if sid in used:
                errors.append(_t_or("admin.models.setup_in_use",
                                    "{name}: not removed, a task still uses it -- pick another "
                                    "setup for it first.", name=(old.get(sid) or {}).get("name") or sid))
                out.append({k: v for k, v in (old.get(sid) or {}).items()})
            continue
        prev = old.get(sid) or {}
        gpu = f("gpu", "auto")
        entry = {"id": sid, "name": name, "model": model, "context": f("context") or None,
                 "kv_cache": f("kv_cache") or "q4_0", "parallel": f("parallel") or None,
                 "engine": f("engine") or prev.get("engine") or "ollama",
                 "thinking": (f("thinking") or ("on" if prev.get("thinking", True) else "off")) != "off",
                 "gpu": gpu if gpu in ("auto", "cpu") else [int(g) for g in gpu.split(",") if g.isdigit()],
                 "gpus": prev.get("gpus") or [], "port": prev.get("port") or 0}
        try:
            clean = OI.normalize_setup(entry)
        except OI.InstanceError as exc:
            errors.append(str(exc))
            continue
        kept = {k: clean[k] for k in ("id", "name", "model", "context", "kv_cache",
                                      "parallel", "gpu", "gpus", "port")}
        # Written only when not the default, so a list saved before engines
        # existed reads the same afterwards.
        if clean["engine"] != "ollama":
            kept["engine"] = clean["engine"]
            kept["thinking"] = clean["thinking"]
        out.append(kept)
    return out, errors


def _form_instances(form, cfg: dict) -> tuple[list[dict], list[str]]:
    """The servers card's rows, as the entries to write, and what is wrong.

    A new row needs only a model: its id comes from the model's name and its
    port is the next free one. "Auto" keeps the cards the server is on now;
    ollama_save places it afterwards.
    """
    errors, out = [], []
    users = _ollama_users(cfg)
    try:
        current = {i["id"]: i for i in OI.instances(cfg, enabled_only=False)}
    except OI.InstanceError:
        current = {}
    all_cards = [g["index"] for g in (_config_dir_json("gpus.json").get("gpus") or [])]
    taken_ports = {i["port"] for i in current.values()}
    count = min(int(form.get("oi-count") or 0), 32)
    for n in range(count + 1):                   # the last row is "add one"
        f = lambda k, d="": (form.get(f"oi-{n}-{k}") or d).strip()      # noqa: E731
        iid, model = f("id").lower(), f("model")
        if not iid and model and n == count:
            base = re.sub(r"[^a-z0-9]", "", model.split("/")[-1].lower()) or "model"
            base = base if base[0].isalpha() else "m" + base
            iid, k = base[:14], 2
            while iid in current or any(e["id"] == iid for e in out):
                iid, k = f"{base[:13]}{k}", k + 1
        if not iid:
            continue
        if form.get(f"oi-{n}-remove") == "on":
            if users.get(iid):
                errors.append(f"{iid}: not removed, "
                              + ", ".join(r for r, _m in users[iid]) + " still run on it")
            elif iid == "main":
                errors.append("main: the server `ollama:` models run on cannot be removed")
            continue
        old = current.get(iid) or {}
        gpu = f("gpu", "auto" if not old else ("auto" if old.get("gpu_mode") == "auto" else ""))
        if gpu == "auto":
            mode, cards = "auto", list(old.get("gpus") or [])
        elif gpu == "all":
            mode, cards = "fixed", all_cards or list(old.get("gpus") or [])
        elif gpu == "none":
            mode, cards = "fixed", []
        else:
            mode, cards = "fixed", [int(g) for g in gpu.split(",") if g.strip().isdigit()]
        port = f("port")
        if not port and not old:
            port = str(_next_ollama_port([{"port": p} for p in taken_ports]))
            taken_ports.add(int(port))
        entry = {
            "id": iid, "label": f("label"), "host": old.get("host") or (
                ((cfg.get("cloud") or {}).get("ollama") or {}).get("local", {}).get("host") or "hub"),
            "port": port or old.get("port") or None, "gpus": cards, "gpu_mode": mode,
            "context": f("context") or None, "parallel": f("parallel") or None,
            "max_models": f("max_models") or None, "kv_cache": f("kv_cache") or "q4_0",
            "flash_attention": True, "keep_alive": f("keep_alive"),
            "unit": old.get("unit") or OI.default_unit(iid),
            "managed": form.get(f"oi-{n}-managed") == "on",
            "purpose": old.get("purpose", ""), "enabled": True,
            "model": model, "pinned": form.get(f"oi-{n}-pinned") == "on",
        }
        if old.get("url"):
            entry["url"] = old["url"]
        try:
            clean = OI.normalize(entry)
        except OI.InstanceError as exc:
            errors.append(str(exc))
            continue
        out.append({k: clean[k] for k in ("id", "label", "host", "port", "gpus", "gpu_mode",
                                          "context", "parallel", "max_models", "kv_cache",
                                          "flash_attention", "keep_alive", "unit", "managed",
                                          "purpose", "model", "pinned")
                    if clean[k] not in ("", None) or k in ("keep_alive", "gpus")}
                   | ({"url": clean["url"]} if clean["url"] else {}))
    if not errors:
        try:
            OI.instances({"cloud": {"ollama": {"instances": out}}})
        except OI.InstanceError as exc:
            errors.append(str(exc))
    return out, errors


def _preview_warnings() -> list[str]:
    """What this request flashed, taken back for a preview to show: a preview
    saves nothing, and its warnings must not greet the next page load."""
    return [m for c, m in get_flashed_messages(with_categories=True) if c in ("warn", "error")]


@app.post("/models/ollama")
def ollama_save():
    """The setups, which one each task uses, and where the used ones run.

    In order: the setups as typed; the tasks' picks (a task can use any setup,
    and using one is what starts its server); a role on a setup follows it
    when its model changes, if the new model can do the job; the general
    servers, when their rows were on screen. Then only the setups some task
    uses become servers (ollama_instances.setup_instances), and those on Auto
    are placed on the cards (ollama_vram.place). A setup nothing uses runs
    nowhere, and one no longer used is stopped by the next apply.
    """
    import ollama_vram
    cfg = load_config()
    before = load_config()
    models = (cfg.setdefault("assistant", {})).setdefault("models", {})
    oll = cfg.setdefault("cloud", {}).setdefault("ollama", {})
    every = {m["id"]: m for m in model_catalogue.all_models(model_catalogue.load_cache(MODELS_CACHE))}

    def can(persona: str, model: str, *, unknown_ok: bool = False) -> bool:
        m = every.get(f"ollama:{model}")
        if m is None and unknown_ok:
            return True
        return _meets_role(persona, m or {"id": model})

    new_setups, errors = _form_setups(request.form, cfg)
    by_sid = {st["id"]: st for st in new_setups}
    # The library's tests decide what a setup may run: a model that failed on
    # the setup's engine is refused here, before an apply takes roles down
    # with it (gemma4:e4b on llama.cpp, 2026-09-24). Untested is only said.
    untested = []
    for st in new_setups:
        engine = st.get("engine", "ollama")
        verdict, why = library_verdict(st["model"], engine)
        if verdict == "fail":
            errors.append(_t_or("admin.models.library_setup_fails",
                                "{name}: {model} does not load on {engine} ({why}). Pick a model "
                                "the library shows working there.", name=st["name"] or st["id"],
                                model=OI.short_model(st["model"]),
                                engine=OI.ENGINE_LABELS.get(engine, engine), why=why))
        elif verdict == "untested":
            untested.append(st["name"] or st["id"])
    if untested and not request.form.get("preview"):
        flash(_t_or("admin.models.library_setup_untested",
                    "{names}: its model is not tested on that engine yet -- test it in the "
                    "library before applying.", names=", ".join(untested)), "warn")
    # llama.cpp reads the files Ollama pulled from Hugging Face, not Ollama's
    # own library conversions: qwen3.5:9b and gemma4:e4b both failed to load
    # (2026-09-24). Said at save, not only after an apply.
    library = [st["name"] or st["id"] for st in new_setups
               if st.get("engine", "ollama") != "ollama"
               and not str(st["model"]).startswith(("hf:", "hf.co/", "/"))]
    if library and not request.form.get("preview"):
        flash(_t_or("admin.models.setup_library_on_llamacpp",
                    "{names}: an Ollama library model on llama.cpp usually does not load. "
                    "Use hf:owner/repo/file.gguf, or set the engine to Ollama.",
                    names=", ".join(library)), "warn")
    # The model is typed now, not picked: a name Ollama does not have here
    # (a typo, or not pulled yet) saves, runs nowhere, and no task offers it --
    # so say so, once, when the model is new to the setup.
    before_models = {str(s.get("id")): s.get("model") for s in (oll.get("setups") or [])
                     if isinstance(s, dict)}
    if any(k.startswith("ollama:") for k in every):
        for st in new_setups:
            if (st.get("engine", "ollama") == "ollama" and st["model"] != before_models.get(st["id"])
                    and f"ollama:{st['model']}" not in every):
                flash(_t_or("admin.models.setup_model_unknown",
                            "{name}: Ollama has no model called {model} here, so no task can "
                            "use it yet. Check the spelling, or pull it first.",
                            name=st["name"] or st["id"], model=st["model"]), "warn")
    for persona in model_catalogue.PERSONA_NEEDS:
        if isinstance(models.get(persona), list):
            chain = list(models[persona])
            for i in range(len(chain)):
                st = by_sid.get((request.form.get(f"task-{persona}-{i}") or "").strip())
                if st and can(persona, st["model"], unknown_ok=st.get("engine", "ollama") != "ollama"):
                    chain[i] = f"{OI.prefix_of(st['id'])}:{st['model']}"
            chain = [v for i, v in enumerate(chain) if v not in chain[:i]]
            if chain != list(models[persona]):
                models[persona] = chain
            continue
        pick = (request.form.get(f"task-{persona}") or "").strip()
        st = by_sid.get(pick)
        if st and can(persona, st["model"], unknown_ok=st.get("engine", "ollama") != "ollama"):
            models[persona] = f"{OI.prefix_of(pick)}:{st['model']}"
    kept, kept_chain = [], []

    def follow(persona: str, value):
        """*value*, moved onto its setup's model if it is on a setup and the
        new model can do the job; otherwise as it was, noted in `kept`."""
        st = by_sid.get(local_model(value)[1]) if isinstance(value, str) else None
        if not st:
            return value
        if persona in model_catalogue.PERSONA_NEEDS and not can(
                persona, st["model"], unknown_ok=True):
            # A chain is not in the task picker, so its note says where it
            # is edited instead of pointing at a picker that does not show it.
            (kept_chain if isinstance(models.get(persona), list) else kept).append(
                f"{persona} ({st['model']})")
            return value
        return f"{OI.prefix_of(st['id'])}:{st['model']}"

    for persona, value in list(models.items()):
        if isinstance(value, list):
            # An ordered chain (the fallback) follows too, entry by entry: left
            # behind, a fallback naming the old model makes the setup's server
            # unload its own model to load that one. Two entries that land on
            # the same name collapse to one -- the deploy refuses a chain that
            # names a model twice.
            chain = [follow(persona, v) for v in value]
            chain = [v for i, v in enumerate(chain) if v not in chain[:i]]
            if chain != list(value):
                models[persona] = chain
        elif isinstance(value, str):
            models[persona] = follow(persona, value)
    # The fallback rescues turns on the everyday model's route; a fallback
    # entry that has become that same model rescues a failing turn with the
    # model that just failed. Allowed -- the chain is the household's -- and
    # said.
    everyday = models.get("everyday")
    same_as_everyday = (isinstance(models.get("fallback"), list) and isinstance(everyday, str)
                        and everyday in models["fallback"])
    if request.form.get("oi-count") is not None:
        general, more = _form_instances(request.form, cfg)
        errors += more
    else:
        general = list(oll.get("instances") or [])
    if errors:
        if request.form.get("preview"):
            return jsonify(ok=False, errors=errors, warnings=_preview_warnings())
        for e in errors:
            flash(e, "error")
        return redirect(url_for("models_page") + "#ollama")
    if kept:
        flash(_t_or("admin.models.ollama_setup_not_followed",
                    "Not moved with its setup, which now runs a model it cannot use: "
                    "{roles}. Pick another setup for it below.",
                    roles=", ".join(kept)), "warn")
    if kept_chain:
        flash(_t_or("admin.models.ollama_chain_not_followed",
                    "Left in the fallback chain as it was, because its setup now runs a model "
                    "that cannot stand in: {roles}. The chain is edited in the config file.",
                    roles=", ".join(kept_chain)), "warn")
    if same_as_everyday:
        flash(_t_or("admin.models.ollama_fallback_is_everyday",
                    "The fallback chain now names the everyday model itself ({model}): a "
                    "failing everyday turn would fall back to the model that just failed.",
                    model=everyday), "warn")
    oll["setups"] = new_setups
    oll["instances"] = general
    # audio.cpp and the cameras: which card. A change here is a change to
    # that service's own block, so the page offers to deploy it.
    for svc, _owner, allow_cpu in GPU_TENANTS:
        pick = (request.form.get(f"gpu-{svc}") or "").strip()
        conf = (cfg.setdefault("services", {})).get(svc)
        if not pick or conf is None:
            continue
        if (pick == "cpu" and allow_cpu) or pick.isdigit() or pick == "all":
            if pick != _tenant_choice(cfg, svc):
                _set_tenant(conf, svc, pick)
    try:
        insts = OI.instances(cfg, enabled_only=False)
    except OI.InstanceError as exc:
        if request.form.get("preview"):
            return jsonify(ok=False, errors=[str(exc)], warnings=_preview_warnings())
        flash(str(exc), "error")
        return redirect(url_for("models_page") + "#ollama")
    # A used setup keeps the port it was given, from now on.
    for inst in insts:
        if inst.get("setup") and inst["id"] in by_sid and not by_sid[inst["id"]]["port"]:
            by_sid[inst["id"]]["port"] = inst["port"]
    unplaced: list[str] = []
    # Placement's own notes speak of a save ("Saved on the card with most
    # room"); a preview says the same things its own way.
    placement_notes: list[str] = []
    if any(i["gpu_mode"] == "auto" for i in insts):
        needs = {k: v["bytes"] for k, v in _ollama_needs(cfg, insts).items()}
        gpus = _gpus_with_tenants(cfg)
        if gpus:
            placed = ollama_vram.place(gpus, insts, needs)
            for sid, cards in placed["gpus"].items():
                if sid in by_sid:
                    by_sid[sid]["gpus"] = cards
                for e in general:
                    if e.get("id") == sid:
                        e["gpus"] = cards
            unplaced = list(placed["unplaced"])
            if placed["unplaced"]:
                placement_notes.append(_t_or(
                    "admin.models.ollama_unplaced",
                    "Does not fit on any card: {ids}. Saved on the card with most "
                    "room; lower a window or the slots, or remove a setup.",
                    ids=", ".join(placed["unplaced"])))
        else:
            placement_notes.append(_t_or("admin.models.ollama_no_gpus_to_place",
                                         "The cards are not known, so nothing was placed. Run "
                                         "./home-stack ollama on the host once."))
    if request.form.get("preview"):
        # What saving would do to the cards, without saving: the same setups,
        # tasks, tenants and placement as above, drawn and discarded -- with
        # the warnings the save would have given, which are half of the answer.
        warnings = _preview_warnings()
        try:
            enabled = [i for i in OI.instances(cfg) if i["enabled"]]
        except OI.InstanceError as exc:
            return jsonify(ok=False, errors=[str(exc)], warnings=warnings)
        needs = _ollama_needs(cfg, enabled)
        view = _gpu_view(_gpus_with_tenants(cfg), enabled, needs, measured=False)
        if not view:
            warnings.append(_t_or("admin.models.ollama_no_gpus",
                                  "The cards are not known yet. Run ./home-stack ollama on the "
                                  "host once and they appear here."))
        cpu = [{"id": i["id"], "name": OI.display(i),
                "gib": round(needs[i["id"]]["bytes"] / ollama_vram.GIB, 1)}
               for i in enabled if i.get("cpu")]
        # The same partial the page draws its cards with, so the two read alike.
        html = render_template("_gpu_cards.html", gpu_view=view) if view else ""
        return jsonify(ok=True, errors=[], warnings=warnings, unplaced=unplaced, cpu=cpu, html=html)
    for note in placement_notes:
        flash(note, "warn")
    save_config(cfg)
    affected = sorted(services_affected(before, cfg))
    note_pending(affected)
    if request.form.get("apply"):
        if _config_dir_json("ollama-trigger.json").get("version") != OI.HELPER_VERSION:
            flash(_t_or("admin.models.ollama_trigger_missing",
                        "Saved, not applied: run sudo ./home-stack ollama --install-trigger on the "
                        "host once."), "warn")
        else:
            (CONFIG.parent / "ollama-apply.request").write_text(
                json.dumps({"at": int(time.time()), "by": session.get("user", "")}))
            flash(_t_or("admin.models.ollama_apply_sent",
                        "Saved and sent to the host. Servers restart one by one."), "ok")
        # "Apply" means the house uses it: the services that read a model this
        # save changed are deployed too. Otherwise the chat titler and
        # Paperless kept asking the Text server for the model it had before,
        # and it reloaded between the two on every request (2026-09-24).
        if affected:
            if _start_deploy(affected, False):
                flash(_t_or("admin.models.ollama_deploying",
                            "Deploying what uses these models: {names}. The Deploy page shows it.",
                            names=", ".join(affected)), "ok")
            else:
                flash(_t_or("admin.models.ollama_deploy_busy",
                            "A deploy is already running; deploy {names} from the Deploy page when "
                            "it ends.", names=", ".join(affected)), "warn")
        return redirect(url_for("models_page") + "#ollama")
    flash(_t_or("admin.models.ollama_saved",
                "Saved. Apply it to the host to start, stop or restart the servers that changed."), "ok")
    return redirect(url_for("models_page") + "#ollama")


@app.post("/models/ollama/apply")
def ollama_apply():
    """Ask the host to apply the saved list (ollama_host.py --from-trigger)."""
    if _config_dir_json("ollama-trigger.json").get("version") != OI.HELPER_VERSION:
        return jsonify(ok=False, message=_t_or(
            "admin.models.ollama_trigger_missing",
            "Not installed on the host yet: run sudo ./home-stack ollama --install-trigger once."))
    (CONFIG.parent / "ollama-apply.request").write_text(
        json.dumps({"at": int(time.time()), "by": session.get("user", "")}))
    return jsonify(ok=True, message=_t_or("admin.models.ollama_apply_sent",
                                          "Sent to the host. Instances restart one by one."))


# ---------------------------------------------------------------------------
# The local model library (deploy/model_library.py runs it on the host)
# ---------------------------------------------------------------------------
LIBRARY_FILE, LIBRARY_QUEUE = "model-library.json", "model-library-queue.json"


def _library_view(cfg: dict) -> dict:
    """The library as the card draws it: each model, its size, and for each
    engine that could run it whether it was tested and how that went."""
    doc = _config_dir_json(LIBRARY_FILE)
    tests = doc.get("tests") or {}
    try:
        setups = OI.setups(cfg)
    except OI.InstanceError:
        setups = []
    rows = []
    for m in doc.get("models") or []:
        used = [st["name"] or st["id"] for st in setups if st["model"] == m["id"]]
        results = tests.get(m["id"]) or {}
        rows.append({
            **m, "gib": round(int(m.get("bytes") or 0) / 2**30, 1), "short": OI.short_model(m["id"]),
            "used_by": used,
            "engines": [{"id": e, "label": OI.ENGINE_LABELS.get(e, e),
                         "state": ("ok" if (results.get(e) or {}).get("ok") else
                                   "fail" if e in results else "untested"),
                         "error": (results.get(e) or {}).get("error", ""),
                         "seconds": (results.get(e) or {}).get("seconds")}
                        for e in m.get("engines") or []]})
    rows.sort(key=lambda r: (r["store"] != "ollama", r["id"]))
    queued = []
    with contextlib.suppress(OSError, ValueError):
        queued = json.loads((CONFIG.parent / LIBRARY_QUEUE).read_text()) or []
    return {"models": rows, "queued": queued,
            "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(doc["at"])) if doc.get("at") else "",
            "disk_free_gib": round(int(doc.get("disk_free") or 0) / 2**30) if doc.get("disk_free") else None}


def library_verdict(model: str, engine: str) -> tuple[str, str]:
    """("ok"|"fail"|"untested"|"unknown", error) for *model* on *engine*."""
    doc = _config_dir_json(LIBRARY_FILE)
    if not any(m.get("id") == model for m in doc.get("models") or []):
        return "unknown", ""
    res = (doc.get("tests") or {}).get(model, {}).get(engine)
    if not res:
        return "untested", ""
    return ("ok" if res.get("ok") else "fail"), res.get("error", "")


@app.post("/models/library")
def library_queue():
    """Queue library work for the host: pull, download, delete or test one model."""
    import model_library as ML
    try:
        job = ML.check_job({"op": request.form.get("op"), "model": request.form.get("model"),
                            "engines": request.form.getlist("engine")})
    except ML.LibraryError as exc:
        flash(str(exc), "error")
        return redirect(url_for("models_page") + "#library")
    if job["op"] == "delete" and job["model"] in ML.used_models(load_config()):
        flash(_t_or("admin.models.library_in_use", "{model} is used by a setup; pick another model "
                    "for it first.", model=job["model"]), "error")
        return redirect(url_for("models_page") + "#library")
    path = CONFIG.parent / LIBRARY_QUEUE
    jobs = []
    with contextlib.suppress(OSError, ValueError):
        jobs = json.loads(path.read_text()) or []
    if job not in jobs:
        jobs.append(job)
    path.write_text(json.dumps(jobs))
    if _config_dir_json("ollama-trigger.json").get("version") != OI.HELPER_VERSION:
        flash(_t_or("admin.models.library_trigger_missing",
                    "Queued. The host cannot run it from here yet: run ./home-stack ollama "
                    "--library-run on the host, or install the trigger once with sudo ./home-stack "
                    "ollama --install-trigger."), "warn")
    else:
        (CONFIG.parent / "ollama-apply.request").write_text(
            json.dumps({"at": int(time.time()), "by": session.get("user", ""), "what": "library"}))
        flash(_t_or("admin.models.library_sent",
                    "Sent to the host. Every new model is tested on each engine that could run "
                    "it; the log below follows it."), "ok")
    return redirect(url_for("models_page") + "#library")


@app.get("/models/library/hf-files")
def library_hf_files():
    """A Hugging Face repo's GGUF files and their sizes, for the download form."""
    repo = (request.args.get("repo") or "").strip().strip("/")
    repo = re.sub(r"^https?://huggingface\.co/", "", repo)
    if not re.fullmatch(r"[A-Za-z0-9][\w.\-]{0,95}/[A-Za-z0-9][\w.\-]{0,95}", repo):
        return jsonify(ok=False, error=_t_or("admin.models.library_bad_repo",
                                             "A repository is owner/name, as on huggingface.co."))
    try:
        with urllib.request.urlopen(
                f"https://huggingface.co/api/models/{repo}/tree/main?recursive=true", timeout=20) as r:
            tree = json.load(r)
    except (OSError, ValueError) as exc:
        return jsonify(ok=False, error=str(exc)[:200])
    files = [{"path": f["path"], "gib": round(int(f.get("size") or 0) / 2**30, 2),
              "model": f"hf:{repo}/{f['path']}"}
             for f in tree if isinstance(f, dict) and str(f.get("path", "")).endswith(".gguf")
             and "mmproj" not in f["path"].lower() and OI.HF_RE.fullmatch(f"hf:{repo}/{f['path']}")]
    return jsonify(ok=True, files=sorted(files, key=lambda f: f["path"]))


@app.get("/models/ollama/status")
def ollama_status():
    card = _ollama_card(load_config())
    return jsonify(requested=card["requested"], log=card["log"], plan_at=card["plan_at"],
                   states={i["id"]: i["state"] for i in card["instances"]})


@app.post("/models/ollama/measure")
def ollama_measure():
    """Load an instance's models and record what each takes on the card."""
    import ollama_vram
    cfg = load_config()
    iid = (request.form.get("id") or "").strip()
    card = _ollama_card(cfg)
    inst = next((i for i in card["instances"] if i["id"] == iid), None)
    if inst is None:
        return jsonify(ok=False, message=f"no instance {iid!r}")
    if inst["state"] == "pending":
        return jsonify(ok=False, message=_t_or(
            "admin.models.ollama_measure_pending",
            "Apply first: the instance is not running these settings yet."))
    names = sorted({m for _r, m in inst["used_by"]})
    if not names:
        return jsonify(ok=False, message=_t_or("admin.models.ollama_measure_idle",
                                               "Nothing runs on this instance to measure."))
    endpoints = model_endpoints(cfg)
    url = (endpoints.get(OI.provider_of(iid)) or {}).get("url") or (
        _bench_instance(cfg)[0] if inst["purpose"] == "bench" else "")
    rows, err = ollama_vram.measure(url, names, inst)
    if err:
        return jsonify(ok=False, message=err)
    doc = ollama_vram.load_measured(OLLAMA_MEASURED)
    entry = doc.setdefault(iid, {"models": {}})
    for r in rows:
        entry["models"][r["model"]] = r
    entry["at"] = int(time.time())
    # The unit's real total, from the host's last look at the card, minus
    # what Ollama reports: the context and buffers /api/ps leaves out.
    unit_mib = sum(t["mib"] for g in card["gpus"] for t in g.get("tenants") or []
                   if t.get("owner") == f"unit {inst['unit']}")
    if unit_mib:
        entry["overhead"] = max(0, unit_mib * 1024 * 1024 - sum(r["vram"] for r in rows))
    ollama_vram.save_measured(OLLAMA_MEASURED, doc)
    total = sum(r["vram"] for r in rows) / ollama_vram.GIB
    return jsonify(ok=True, message=_t_or("admin.models.ollama_measured",
                                          "Measured: {gib} GiB for {n} model(s).",
                                          gib=f"{total:.1f}", n=len(rows)))


# Which benchmark kind (bench.KINDS) answers for a Models-page role. The
# personas are everyday turns with another voice; the titler is a short,
# unattended job like a notification. vision and documents have no benchmark
# yet and keep the catalogue's own pick.
# Roles that are an ordered chain rather than one model, and how many places
# their picker offers.
CHAIN_ROLES = ("fallback",)
CHAIN_SLOTS = 3

# Which of the benchmark's case groups test each role (services/nanobot/bench/
# cases.json). A role's Test button runs exactly these, through the same
# assistant loop and scoring as the Benchmark card -- one suite, one verdict.
# Roles with no group here (the classifier, titles, vision, documents) keep
# the quick check until the benchmark has cases for them.
ROLE_BENCH_GROUPS = {
    "everyday": ["everyday", "tools"], "powerful": ["everyday", "tools"],
    "programmer": ["everyday", "tools"], "teacher": ["everyday", "tools"],
    "designer": ["everyday", "tools"], "doctor": ["everyday", "tools"],
    "legal": ["everyday", "tools"], "fallback": ["everyday", "tools"],
    "planner": ["planner"], "subagent": ["longtask"], "plan_steps": ["steps"],
    "notifications": ["notifications"], "events": ["events"], "heartbeat": ["heartbeat"],
}

ROLE_BENCH_KIND = {
    "everyday": "everyday", "powerful": "everyday", "programmer": "everyday",
    "teacher": "everyday", "designer": "everyday", "doctor": "everyday",
    "legal": "everyday", "fallback": "everyday",
    "notifications": "background", "events": "background", "heartbeat": "background",
    "classifier": "background", "titles": "background",
    "subagent": "longtask", "planner": "planner", "plan_steps": "everyday",
}
LOCAL_PICKS = 3


def _bench_local_best(views: list[dict]) -> dict[str, list[dict]]:
    """kind -> local Ollama models, best first, as the benchmark ranked them."""
    out: dict[str, list[dict]] = {}
    for kind, _roles in model_bench.KINDS:
        ranked = sorted((v for v in views if kind in (v.get("rank") or {})),
                        key=lambda v: v["rank"][kind])
        seen, picks = set(), []
        for v in ranked:
            canon, _iid = local_model(str(v.get("model") or ""))
            score = v["kinds"][kind]
            if not canon.startswith("ollama:") or canon in seen or not score["passed"]:
                continue
            seen.add(canon)
            picks.append({"id": canon, "passed": score["passed"], "total": score["total"]})
        out[kind] = picks
    return out


def _picker_setups(cfg: dict) -> list[dict]:
    """Every setup, as the model pickers offer it: by name, with its engine,
    window and card. Picking one is what starts it -- the Ollama card says
    how it is built; the picker says which one a role runs on."""
    try:
        setups = OI.setups(cfg)
    except OI.InstanceError:
        return []
    every = {m["id"]: m for m in model_catalogue.all_models(model_catalogue.load_cache(MODELS_CACHE))}
    out = []
    for st in setups:
        where = ("CPU" if st["gpu"] == "cpu" else
                 "GPU " + "+".join(map(str, st["gpu"] if st["gpu"] != "auto" else st["gpus"]))
                 if (st["gpu"] != "auto" or st["gpus"]) else _t_or("admin.models.ollama_gpu_auto", "Auto"))
        m = every.get(f"ollama:{st['model']}") or {}
        out.append({"id": st["id"], "label": f"{setup_label(st)} · {where}",
                    # A llama.cpp server here has no vision projector loaded.
                    "vision": bool(m.get("vision")) and st.get("engine", "ollama") == "ollama"})
    return out


def _setup_value(cfg: dict, sid: str, persona: str) -> str:
    """What a role is set to when a setup is picked for it: the setup's
    address. "" when there is no such setup or its model cannot do the role."""
    try:
        st = next((x for x in OI.setups(cfg) if x["id"] == sid), None)
    except OI.InstanceError:
        st = None
    if not st:
        return ""
    if persona in model_catalogue.PERSONA_NEEDS:
        every = {m["id"]: m for m in model_catalogue.all_models(model_catalogue.load_cache(MODELS_CACHE))}
        m = every.get(f"ollama:{st['model']}")
        if m is None and st.get("engine", "ollama") != "ollama":
            m = {"id": st["model"]}                  # a llama.cpp file: no catalogue entry
        if not _meets_role(persona, m or {"id": st["model"]}):
            return ""
    return f"{OI.prefix_of(sid)}:{st['model']}"


def _place_new_setups(cfg: dict) -> list[str]:
    """Put setups a save just started using on a card, when they are on Auto
    and have none yet -- the placement the Ollama card's save does. Returns
    the ids that fit nowhere."""
    import ollama_vram
    try:
        insts = OI.instances(cfg, enabled_only=False)
    except OI.InstanceError:
        return []
    todo = [i for i in insts if i.get("setup") and i["gpu_mode"] == "auto" and not i["gpus"]]
    gpus = _gpus_with_tenants(cfg)
    if not todo or not gpus:
        return []
    needs = {k: v["bytes"] for k, v in _ollama_needs(cfg, insts).items()}
    placed = ollama_vram.place(gpus, insts, needs)
    for st in ((cfg.get("cloud") or {}).get("ollama") or {}).get("setups") or []:
        if st.get("id") in placed["gpus"] and st.get("id") in {i["id"] for i in todo}:
            st["gpus"] = placed["gpus"][st["id"]]
    return [u for u in placed["unplaced"] if u in {i["id"] for i in todo}]


def _default_local(cfg: dict, persona: str, every: dict | None = None,
                   best_by_kind: dict | None = None) -> str:
    """The local model a role gets when it is switched to "Local Ollama".

    A setup whose model can do the role -- the one the benchmark ranks best
    for the role's kind first, else the first listed. With no setup that can:
    the general server, with the benchmark's best pulled model for the kind,
    or failing a ranking (vision, documents) the first pulled model that can
    do the job. "" when nothing local can. `every` and `best_by_kind` may be
    passed in by a caller asking for several roles.
    """
    if every is None:
        every = {m["id"]: m for m in model_catalogue.all_models(
            model_catalogue.load_cache(MODELS_CACHE))}
    if best_by_kind is None:
        best_by_kind = _bench_local_best(_bench_views(cfg))
    best = best_by_kind.get(ROLE_BENCH_KIND.get(persona, "")) or []
    rank = {b["id"]: i for i, b in enumerate(best)}
    try:
        all_setups = OI.setups(cfg)
    except OI.InstanceError:
        all_setups = []
    fit = [st for st in all_setups
           if _meets_role(persona, every.get(f"ollama:{st['model']}") or {"id": st["model"]})]
    if fit:
        top = min(fit, key=lambda st: rank.get(f"ollama:{st['model']}", len(rank)))
        return f"{OI.prefix_of(top['id'])}:{top['model']}"
    if not any(i["id"] == "main" for i in _safe_serving(cfg)):
        return ""
    for b in best:
        m = every.get(b["id"])
        if m and _meets_role(persona, m):
            return b["id"]
    for mid, m in sorted(every.items()):
        if mid.startswith("ollama:") and _meets_role(persona, m):
            return mid
    return ""


def _meets_role(persona: str, model: dict) -> bool:
    spec = model_catalogue.PERSONA_NEEDS.get(persona) or {}
    return model_catalogue.meets(model, spec.get("requires", {}))


def _bench_kind_args() -> dict:
    """What the results card needs to sort by kind (bench.KINDS)."""
    return {"bench_kinds": [k for k, _ in model_bench.KINDS],
            "bench_kind_of": model_bench.KIND_OF_ROLE}


def _bench_views(cfg: dict) -> list[dict]:
    """The results card's rows: one per model, best first, the best one marked."""
    current = _cases_digest()
    runs = model_bench.load_results(BENCH_RESULTS)
    setups = _current_setup_digests(str(r.get("container_name") or "") for r in runs)
    params = _ollama_params(cfg)
    views = []
    for run in runs:
        run["removable"] = model_bench.removable(cfg, str(run.get("model") or ""))
        view = model_bench.run_view(run, params_lookup=params)
        # A run the current cases would not have produced is not comparable with
        # one they would. Unstamped runs predate the stamp, so they are stale too.
        view["stale_cases"] = bool(current) and view["cases_digest"] != current
        # Nor is one made with skills, prompts or code that have changed since.
        view["stale_setup"] = model_bench.setup_is_stale(view, setups.get(view["container_name"]))
        views.append(view)
    # The best total among the runs on screen gets marked, so the answer to
    # "which of these should the house use" is not a column of fractions.
    views = model_bench.sort_runs(views)
    model_bench.rank_kinds(views)
    return views


@app.get("/models/bench/results")
def bench_results():
    """The results card alone, for the page to swap in as each model finishes.

    A job with three models queued used to show nothing until the third was
    done; the page now asks for this whenever the log says another result
    was saved, and leaves the live progress above it alone.
    """
    return render_template("_bench_results.html", bench_runs=_bench_views(load_config()),
                           bench_roles=model_bench.ROLES, **_bench_kind_args())


def _bench_answer(message: str = "", level: str = "ok"):
    """What forget and remove answer: JSON to the page's own fetch, which then
    swaps the results card in place; a flash and a redirect to a plain form
    post, so the buttons still work with scripts off."""
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=level == "ok", message=message, level=level)
    if message:
        flash(message, level)
    return redirect(url_for("models_catalog") + "#bench")


@app.post("/models/bench/delete")
def bench_delete():
    """Forget a model's result. The page sends the file it shows; any earlier
    file the same model still has goes with it, or forgetting would reveal it."""
    name = Path(request.form.get("file", "")).name
    path = BENCH_RESULTS / name
    if name.endswith(".json") and path.is_file():
        try:
            model = str(json.loads(path.read_text(encoding="utf-8")).get("model") or "")
        except (OSError, ValueError):
            model = ""
        path.unlink()
        for other in (model_bench.results_for(BENCH_RESULTS, model) if model else []):
            with contextlib.suppress(OSError):
                other.unlink()
    return _bench_answer()


@app.post("/models/bench/remove-model")
def bench_remove_model():
    """Delete a pulled model from Ollama -- only one no role is configured to use."""
    cfg = load_config()
    model = request.form.get("model", "")
    if not model_bench.removable(cfg, model):
        return _bench_answer(_t_or("admin.models.bench_remove_refused",
                                   "Not removed: {model} is in use, or is not a local model.",
                                   model=model), "warn")
    name, prefix = model_bench.split_model(model)
    try:
        _bench_ollama(_bench_ollama_url(cfg, prefix), "/api/delete", {"model": name},
                      method="DELETE", timeout=60).close()
    except (OSError, ValueError) as exc:
        return _bench_answer(f"{model}: {exc}", "error")
    return _bench_answer(_t_or("admin.models.bench_removed", "Removed {model} from Ollama.",
                               model=model), "ok")


@app.get("/deploy/log")
def deploy_log():
    """The page's polling endpoint: new log bytes from `offset`, plus state."""
    offset = max(0, request.args.get("offset", 0, type=int))
    text = ""
    if DEPLOY_LOG.exists():
        with DEPLOY_LOG.open() as fh:
            fh.seek(offset)
            text = fh.read()
            offset = fh.tell()
    with _deploy_lock:
        job = dict(_deploy_job)
    return jsonify(
        offset=offset, lines=text.splitlines(),
        running=job["running"], failed=job["failed"],
        targets=job["targets"], dry_run=job["dry_run"],
        elapsed=round((job["finished"] or time.time()) - job["started"], 1)
        if job["started"] else 0,
    )


def backup_catalogue(cfg: dict) -> list[dict]:
    """The archives that exist, newest first, or nothing if backup.py is out.

    Read from the filenames and the two sidecars beside each archive, so this
    costs no decryption: an encrypted archive would otherwise need the key to
    say its own size, on every page load.
    """
    mod = _backup_module()
    if mod is None:
        return []
    try:
        return list(reversed(mod.catalogue(cfg)))
    except Exception:  # noqa: BLE001 - the page still has to render
        return []


def _start_backup(argv: list[str], targets: list[str]) -> bool:
    """Run a backup or a restore through the same one-job-at-a-time slot.

    The same lock as a deploy, deliberately. A restore replaces state on disk
    while a deploy replaces the trees that read it, and the two racing is a
    house in a state neither of them describes. One job, one log, one page
    that can be watched.
    """
    with _deploy_lock:
        if _deploy_job["running"]:
            return False
        _deploy_job.update(running=True, failed=None, targets=targets,
                           dry_run=False, started=time.time(), finished=None)
    threading.Thread(target=_run_deploy_job,
                     args=(argv, targets, True, False), daemon=True).start()
    return True


@app.route("/access", methods=["GET", "POST"])
def access():
    """How this page reaches the other machines.

    Only interesting on a split install, and invisible until now on any of
    them: `{config}/ssh` was mounted read-only and started empty, so a deploy
    from this page could not reach a second host and the reason arrived as an
    ssh error inside a deploy log.

    Two things live here. The key this page deploys with -- minted here,
    private half never read or shown, public half offered because getting it
    onto the other machine is the whole job. And what each host's key is,
    against what this stack trusts.
    """
    cfg = load_config()
    locale = current_locale(cfg)
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "generate_key":
            ok, why = make_deploy_key()
            flash(translator(f"admin.access.{why}", locale=locale)
                  if why in ("key_made", "key_exists")
                  else translator("admin.access.key_failed",
                                  locale=locale, detail=why),
                  "ok" if ok else "error")
        elif action == "import_key":
            # Never echoed back, never logged, and out of the form the moment
            # it lands: a private key in a flash message would survive in a
            # session cookie.
            ok, why = import_deploy_key(request.form.get("private_key", ""))
            flash(translator(f"admin.access.{why}", locale=locale)
                  if why in ("key_imported", "key_empty", "key_invalid")
                  else translator("admin.access.key_failed",
                                  locale=locale, detail=why),
                  "ok" if ok else "error")

        elif action == "set_user":
            if set_remote_user(cfg, request.form.get("role", ""),
                               request.form.get("user", "")):
                save_config(cfg)
                # Which account the deployer logs in as is read at deploy time,
                # so nothing has to be redeployed for it to take effect -- but
                # the services on that machine are now reachable in a way they
                # were not, which is worth a deploy.
                flash("saved", "ok")
            else:
                flash(translator("admin.access.user_bad", locale=locale), "error")

        elif action == "trust":
            address = request.form.get("address", "").strip()
            # The fingerprint typed back, not a button. Trusting a *changed*
            # key is the one action on this page that can hand a session to
            # somebody in the middle, and the difference between clicking and
            # copying a fingerprint off another screen is the difference
            # between a slip and a decision.
            state = host_key_state(address) if address else {}
            want = (state.get("fingerprint") or "").split()
            want = want[1] if len(want) > 1 else ""
            if not want:
                flash(translator("admin.access.unreachable", locale=locale,
                                 address=address or "-"), "error")
            elif request.form.get("confirm", "").strip() != want:
                flash(translator("admin.access.confirm_mismatch",
                                 locale=locale, fingerprint=want), "error")
            else:
                ok, why = trust_host(address)
                flash(translator(f"admin.access.{why}", locale=locale,
                                 address=address), "ok" if ok else "error")
        return redirect(url_for("access"))

    roles = []
    for entry in remote_roles(cfg):
        roles.append({**entry, **host_key_state(entry["address"])})
    return render_template("access.html", roles=roles, key=deploy_key(),
                           ssh_dir=SSH_DIR)


@app.route("/backups", methods=["GET", "POST"])
def backups():
    """What to point a backup at, what exists, and putting one back.

    The inventory is derived: `deploy/backup.py` computes it from the
    manifest's `state:` entries, so this page and `./home-stack backup` cannot
    disagree about what matters. Somebody backing the machine up with their own
    tool needs the list, and reading it out of docs/backups.md means reading a
    file that is true of the package rather than of *this* install -- the paths
    here are interpolated against this household's `paths:`.

    Restoring from here is narrowed to one service and asks for the service's
    name to be typed. Both on purpose. A whole-archive restore from a web page
    is a single click that reverts the user store and the credentials file to
    whatever hour that archive was made -- signing the house out and undoing
    every setting changed since -- and the CLI is the right place for the thing
    that does that. What a person actually wants here is "the documents came
    back wrong, put those back".
    """
    cfg = load_config()
    if request.method == "POST":
        action = request.form.get("action", "")
        locale = current_locale(cfg)
        script = str(ROOT / "deploy" / "backup.py")

        if action == "encrypt":
            # The key first, then the switch. Turning encryption on with no
            # key makes every run fail at the last step, after it has copied
            # everything -- and the message a person sees is about openssl.
            want = request.form.get("encrypt") == "on"
            if want and not get_secret("BACKUP_ENCRYPTION_KEY"):
                flash(translator("admin.backups.need_key", locale=locale), "error")
                return redirect(url_for("backups"))
            cfg.setdefault("backups", {})["encrypt"] = want
            save_config(cfg)
            flash("saved", "ok")
            return redirect(url_for("backups"))

        if action == "generate_key":
            # Refused rather than silently replaced. Every archive already
            # written is decryptable only with the key it was made under, so
            # rotating this makes all of them unreadable -- and the page that
            # did it looked like it had done something helpful.
            if get_secret("BACKUP_ENCRYPTION_KEY"):
                flash(translator("admin.backups.key_exists", locale=locale), "error")
                return redirect(url_for("backups"))
            set_secret("BACKUP_ENCRYPTION_KEY", secrets_mod.token_urlsafe(32))
            flash(translator("admin.backups.key_made", locale=locale), "ok")
            return redirect(url_for("backups"))

        if action == "run":
            argv = [sys.executable, script]
            if not _start_backup(argv, ["backup"]):
                flash(translator("admin.deploy.busy", locale=locale), "error")
            return redirect(url_for("backups"))

        if action == "verify":
            which = request.form.get("archive", "")
            # Optional, and the per-archive button in the table above sends
            # none -- that one still asks the whole-archive question and is
            # still the only thing that writes the `verified` marker.
            #
            # Ticked rather than typed, unlike the restore form below it: this
            # unpacks into a scratch directory and changes nothing, so there is
            # no slip for a typed confirmation to protect against. The names
            # come from the archive's own sidecar, so a service that predates
            # or postdates it is not offered.
            services = [s.strip() for s in request.form.getlist("service")
                        if s.strip()]
            argv = [sys.executable, script, "--verify", which]
            label = f"verify {which}"
            if services:
                argv += ["--only", ",".join(services)]
                label = f"verify {which} ({', '.join(services)})"
            if not _start_backup(argv, [label]):
                flash(translator("admin.deploy.busy", locale=locale), "error")
            return redirect(url_for("backups"))

        if action == "restore":
            which = request.form.get("archive", "")
            service = request.form.get("service", "").strip()
            # Typed back, not ticked. This overwrites live state, and the
            # difference between choosing the wrong row in a select and typing
            # a name is the difference between a slip and a decision.
            if request.form.get("confirm", "").strip() != service or not service:
                flash(translator("admin.backups.confirm_mismatch",
                                 locale=locale, service=service or "-"), "error")
                return redirect(url_for("backups"))
            argv = [sys.executable, script, "restore", which,
                    "--only", service, "--confirm"]
            if not _start_backup(argv, [f"restore {service}"]):
                flash(translator("admin.deploy.busy", locale=locale), "error")
            else:
                # It lands on disk; the container goes on reading what it had
                # open. Saying so here rather than only in the job log, which
                # is the thing somebody scrolls past.
                note_pending([service])
            return redirect(url_for("backups"))

        return redirect(url_for("backups"))

    with _deploy_lock:
        job = dict(_deploy_job)
    return render_template("backups.html", inv=backup_inventory(cfg),
                           paths=(cfg.get("paths") or {}),
                           archives=backup_catalogue(cfg),
                           encrypt=bool((cfg.get("backups") or {}).get("encrypt")),
                           has_key=bool(get_secret("BACKUP_ENCRYPTION_KEY")),
                           job=job, has_log=DEPLOY_LOG.exists())


@app.route("/deploy", methods=["GET", "POST"])
def deploy():
    cfg = load_config()
    manifest = load_manifest()

    # Computed before the POST branch, not after it. Both paths need it -- the
    # POST expands «All» into the enabled services minus this page -- and having
    # it below meant a 500 on every Preview and Deploy of «All». Nothing caught
    # that because the only test posting «All» was the one asserting the
    # *blocked* case, which returns before it gets here.
    names = [
        n for n in manifest.get("services", {})
        if ((cfg.get("services") or {}).get(n) or {}).get("enabled", True)
    ]

    if request.method == "POST":
        blocked = cannot_deploy_from_here(cfg)
        if request.form.get("target") == SELF_SERVICE and running_in_container():
            flash(translator("admin.deploy.no_self", locale=current_locale(cfg)),
                  "error")
            return redirect(url_for("deploy"))
        if blocked:
            # Not only hidden in the template: a POST can arrive from a stale
            # tab, and starting a job that can only fail costs a minute and
            # leaves a failed run on the page.
            flash(translator("admin.deploy.blocked_container",
                             locale=current_locale(cfg), roles=blocked), "error")
            return redirect(url_for("deploy"))
        target = request.form.get("target", "all")
        pending = expand_pending(load_pending(), manifest)
        if target == "__pending__":
            if not pending:
                flash("nothing is waiting to be deployed", "ok")
                return redirect(url_for("deploy"))
            targets = pending
        elif target == "all":
            # Everything except this page, when this page is the thing running
            # the deploy. Named explicitly rather than passing "all": the
            # deployer would take the admin unit down -- `compose down` before
            # `up` -- and the process doing the deploying is inside it, so the
            # run dies halfway with the container gone and nothing to report.
            targets = ([n for n in names if n != SELF_SERVICE]
                       if running_in_container() else ["all"])
        else:
            targets = [target]
        if not _start_deploy(targets, bool(request.form.get("dry_run"))):
            flash(translator("admin.deploy.busy", locale=current_locale(cfg)), "error")
        return redirect(url_for("deploy"))

    # The plan, so the page can say what each choice actually means.
    plan = {
        name: [u["name"] for u in spec.get("units", [])]
        for name, spec in manifest.get("services", {}).items()
        if name in names
    }
    with _deploy_lock:
        job = dict(_deploy_job)
    return render_template(
        "deploy.html", names=names, plan=plan, job=job,
        has_log=DEPLOY_LOG.exists(),
        # The app build, which shares this page because it is the other thing
        # here that takes minutes and streams a log.
        apk={"image": apk_image_present(), "has_log": APK_LOG.exists(),
             **{k: v for k, v in _apk_load().items() if k != "started"}},
        blocked_roles=cannot_deploy_from_here(cfg),
        self_service=SELF_SERVICE if running_in_container() else "",
        pending=expand_pending(load_pending(), manifest),
    )


def start_price_watcher() -> None:
    """Start the background model-price refresh thread.

    Call this once after importing `app` when running under a WSGI server;
    the dev/container entry point below calls it automatically.
    """
    threading.Thread(target=_price_watcher, daemon=True).start()



# --------------------------------------------------------------------------
# Service status: what is running, and what is running *behind* the config
# --------------------------------------------------------------------------
# "Outdated" is three different questions and answering them as one number is
# how a dashboard lies. A service can be:
#
#   pending   a setting changed and nothing has deployed it -- the admin page
#             already tracks this, and it is the only one of the three that
#             says somebody has to act.
#   stale     the image tag has moved since this container was created, so the
#             container is running a build nobody replaced it with. That is the
#             `alfred-nanobot:latest` race: two units build the same tag from
#             different contexts, and whoever built last owns the name.
#   behind    a file under the service's own directory is newer than the
#             container. Weakest of the three and deliberately last: an edit
#             nobody deployed is not the same as a broken deploy, and on a
#             checkout that is being worked in it is the normal state.
#
# None of them is health. A container can be up, healthy, and three commits
# behind; a container can be current and crash-looping. They are reported side
# by side rather than folded together.

def _exited_clean(status: str) -> bool:
    """Whether `Exited (0)` -- a container that finished rather than failed.

    Read off the status line because `docker ps` already has it and asking
    again is another subprocess per container. The shape is stable and the
    parse fails safe: anything unrecognised counts as *not* clean, so a
    container in a state this does not understand is reported rather than
    hidden.
    """
    match = re.search(r"exited \((\d+)\)", (status or "").lower())
    return bool(match) and match.group(1) == "0"


def _docker_ps() -> list[dict]:
    """Every container docker knows about, with the labels compose stamps on it."""
    fmt = ("{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}\t"
           "{{.Label \"com.docker.compose.project\"}}")
    try:
        out = subprocess.run(["docker", "ps", "-a", "--format", fmt],
                             capture_output=True, text=True, timeout=20)
    except Exception:  # noqa: BLE001 - no docker is a legitimate answer here
        return []
    if out.returncode != 0:
        return []
    rows = []
    for line in (out.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 5:
            rows.append({"name": parts[0], "state": parts[1], "status": parts[2],
                         "image": parts[3], "project": parts[4]})
    return rows


def _image_id(ref: str) -> str:
    try:
        out = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", ref],
                             capture_output=True, text=True, timeout=15)
        return (out.stdout or "").strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _container_facts(name: str) -> dict:
    """The image a container actually runs, and when it was created.

    `docker ps` reports the image *reference* a container was started with,
    which is a tag -- and a tag is a name that moves. The id underneath it is
    what says whether this container is the build that name points at now.
    """
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.Image}}\t{{.Created}}\t{{.Config.Image}}", name],
            capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return {}
        image, created, ref = ((out.stdout or "").strip().split("\t") + ["", "", ""])[:3]
        return {"image_id": image, "created": created, "ref": ref}
    except Exception:  # noqa: BLE001
        return {}


def _newest_source(directory: Path) -> float:
    """The newest mtime under a service's own directory, or 0.

    Bounded rather than exhaustive: this runs on a page load, and a service
    directory can hold a node_modules. The directories that matter for "did
    somebody edit this" are the ones under version control anyway.
    """
    skip = {".git", "__pycache__", "node_modules", ".venv", "dist", "build"}
    newest = 0.0
    try:
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if d not in skip]
            for f in files:
                try:
                    newest = max(newest, os.stat(os.path.join(root, f)).st_mtime)
                except OSError:
                    continue
    except Exception:  # noqa: BLE001
        return 0.0
    return newest


def service_status() -> list[dict]:
    """One row per enabled service: what is up, and what is behind.

    Containers are matched to services through the compose *project*, which the
    deployer sets to `<service>-<unit>` -- matched against the manifest's own
    names rather than split on a dash, because half the services have one in
    their name and `home-core-local` would resolve to `home` otherwise.
    """
    cfg = load_config()
    manifest = load_manifest()
    services = manifest.get("services", {}) or {}
    pending = set(load_pending())
    containers = _docker_ps()
    now = time.time()

    rows = []
    for name, spec in sorted(services.items()):
        if not ((cfg.get("services") or {}).get(name) or {}).get("enabled", True):
            continue
        units = spec.get("units") or []
        # `compose_project` wins where a unit sets one, exactly as the deployer
        # does it (`unit.get("compose_project") or f"{name}-{unit['name']}"`).
        # Two units override it -- home-paperless deploys as `paperless` and
        # home-search as `home-search` -- and this built only the derived name,
        # so it matched none of their containers and reported a running document
        # archive as having none at all. Idle is the one state that needs no
        # action, so the row said "nothing to see" about the service that was
        # most obviously there.
        projects = {u.get("compose_project") or f"{name}-{u['name']}"
                    for u in units if u.get("name")}
        mine = [c for c in containers if c["project"] in projects]

        # A container that exited 0 is *finished*, not broken. Every compose
        # project here has one-shot `init-dirs` containers that create a
        # directory and stop, and `depends_on: service_completed_successfully`
        # is the whole point of them -- counting those as trouble had this page
        # reporting `nanobot 7/8, 1 unhealthy` about a stack with nothing wrong
        # with it. A dashboard that cries wolf on a healthy house is one nobody
        # reads on the day it is right.
        running = [c for c in mine if c["state"] == "running"]
        done = [c for c in mine
                if c["state"] == "exited" and _exited_clean(c["status"])]
        broken = [c for c in mine
                  if "unhealthy" in (c["status"] or "").lower()
                  or c["state"] in ("dead", "restarting")
                  or (c["state"] == "exited" and not _exited_clean(c["status"]))]

        # One `docker inspect` per container, and one walk per directory.
        #
        # Both loops below want the same two answers, and asking twice is a
        # subprocess twice: the assistants are three units over six containers,
        # which spawned twenty-four inspects to fill one row -- each with a 15s
        # timeout, on a page load somebody is waiting on. The three units also
        # share `services/nanobot`, so the tree was walked three times for the
        # same mtime.
        facts = {c["name"]: _container_facts(c["name"]) for c in running}

        # stale: the tag moved out from under a running container.
        stale, image_ids = [], {}
        for c in running:
            f = facts[c["name"]]
            ref = f.get("ref") or c["image"]
            if not ref or "@" in ref:
                continue
            if ref not in image_ids:
                image_ids[ref] = _image_id(ref)
            current = image_ids[ref]
            if current and f.get("image_id") and current != f["image_id"]:
                stale.append(c["name"])

        # behind: somebody edited the service and did not deploy it.
        stamps = []
        for c in running:
            created = facts[c["name"]].get("created") or ""
            try:
                stamps.append(datetime.datetime.fromisoformat(
                    created.replace("Z", "+00:00")).timestamp())
            except ValueError:
                continue
        oldest_created = min(stamps) if stamps else 0.0
        # The newest edit anywhere the service is built from, against the
        # oldest container: the same question the nested loops asked, which was
        # true when *any* unit's source was newer than *any* container.
        newest_src = 0.0
        for d in {ROOT / str(u.get("dir") or "") for u in units}:
            if d.exists():
                newest_src = max(newest_src, _newest_source(d))
        behind = bool(stamps) and newest_src > min(stamps)

        # `expected` leaves out the ones that are supposed to have stopped, so
        # "3/3" means three that should be up are up rather than three out of a
        # count that includes a finished setup step.
        expected = [c for c in mine if c not in done]
        rows.append({
            "name": name,
            "description": (spec.get("description") or "").strip().split("\n")[0],
            "containers": len(expected),
            "completed": len(done),
            "running": len(running),
            "unhealthy": [c["name"] for c in broken],
            "pending": name in pending,
            "stale": stale,
            "behind": behind,
            "age": _since(now - oldest_created) if oldest_created else "",
        })

    # Whatever needs somebody first. Alphabetical order is the right default for
    # a list you are looking something up in and the wrong one for a list you
    # are checking -- the one row that matters was sitting between `n8n` and
    # `nodered`, reading exactly like them.
    def _urgency(row):
        if row["unhealthy"] or (row["containers"] and not row["running"]):
            return 0
        if row["pending"]:
            return 1
        if row["stale"]:
            return 2
        if not row["containers"]:
            return 3
        return 4
    rows.sort(key=lambda r: (_urgency(r), r["name"]))
    return rows


def _since(seconds: float) -> str:
    """How long ago, in the words somebody would use.

    `122.1` was the number this printed, and hours stop being a unit somebody
    reads at about two days. Nothing here needs to be exact -- the question it
    answers is "has this been up since before the change I am asking about",
    and that is answered by "5 days" as well as by 122.1.
    """
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h"
    return f"{hours // 24} d"


@app.route("/status")
def status_page():
    rows = service_status()
    # One sentence at the top, because the question somebody opens this page
    # with is "is anything wrong" and the answer should not require reading
    # fourteen rows to assemble.
    trouble = [r for r in rows
               if r["unhealthy"] or (r["containers"] and not r["running"])]
    return render_template(
        "status.html",
        rows=rows,
        trouble=trouble,
        needs_deploy=[r["name"] for r in rows if r["pending"]],
        stale=[r for r in rows if r["stale"] and r not in trouble],
        idle=[r for r in rows if not r["containers"]],
        healthy=len([r for r in rows if r["containers"] and r["running"]
                     and not r["unhealthy"]]),
    )

if __name__ == "__main__":
    # This is how the container starts -- admin/Dockerfile's CMD is
    # `python /app/admin/app.py` -- not a dev-only branch.
    start_price_watcher()
    #
    # The container is on the host's network, so there is no port publish
    # between a container side and a host side any more: what this binds is
    # what somebody on the network can reach, and it is the only thing that
    # decides it. That is why the default here is loopback and not 0.0.0.0.
    #
    # It used to be the other way round, and correctly so: with
    # `${ADMIN_BIND:-127.0.0.1}:${ADMIN_PORT}:8099` in the compose file the
    # publish decided reach, and binding the container's own loopback made the
    # page unreachable while the healthcheck -- curling 127.0.0.1 from inside
    # -- went on reporting healthy. Host networking inverts that: the two
    # addresses are now one address, and the safe default is the closed one.
    app.run(
        host=os.environ.get("ADMIN_BIND_INSIDE", "127.0.0.1"),
        port=int(os.environ.get("ADMIN_PORT", "21002")),
    )
