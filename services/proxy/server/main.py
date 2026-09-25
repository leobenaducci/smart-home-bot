"""home-chat cloud proxy.

Public entrypoint for the family to chat with Alfred without the VPN. Runs on a
VPS, authenticates against a synced copy of HomeCore's users.json, and reverse-
proxies the chat routes to HomeCore *local* over the home->VPS tunnel, injecting a
trusted X-Proxy-Secret / X-Proxy-User pair. HomeCore then behaves exactly as it
does for a browser session (resolving the user's nanobot_id, streaming, etc).

The Android app is a thin WebView pointing at this server; on login it receives a
long-lived HttpOnly cookie and is dropped straight into /chat?embed=1.
"""
import ipaddress
import logging
import os
import html

import httpx

import auth
from fastapi import FastAPI, Request, Form
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
    JSONResponse,
    Response,
)
from starlette.background import BackgroundTask

from auth import (
    verify_password,
    create_token,
    decode_token,
    COOKIE_NAME,
    COOKIE_MAX_AGE,
)
from users import find_user
import devices
import ntfy_config

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HOMECORE_LOCAL_URL = os.environ.get("HOMECORE_LOCAL_URL", "https://portal.home:8443").rstrip("/")
PROXY_SHARED_SECRET = os.environ.get("PROXY_SHARED_SECRET", "")
VERIFY_UPSTREAM_TLS = os.environ.get("VERIFY_UPSTREAM_TLS", "0") not in ("0", "false", "")
SECURE_COOKIE = os.environ.get("SECURE_COOKIE", "1") not in ("0", "false", "")

# Whether this is the copy running inside the house.
#
# The same code serves both sides of the split horizon: on the LAN, Pi-hole
# points chat.home at hub and a local copy answers; off the VPN, the
# VPS does. Some pages should only ever be the first kind — the cameras and the
# finance dashboard are for the house, and a login on a public host is not the
# boundary anybody wants around a live camera feed.
#
# Defaults to 0, which is the safe direction: forgetting it on the LAN copy
# costs a page at home, and the failure is visible immediately. Setting it on
# the VPS is the one that exposes something, and that takes a deliberate act in
# a file the VPS deploy never copies.
IS_LAN_COPY = os.environ.get("IS_LAN_COPY", "0") not in ("0", "false", "")


# Which source addresses count as being inside the house. The LAN itself is
# already answered by `IS_LAN_COPY` -- the copy on hub is only reachable from
# there -- so this is about the VPN, and about the one client that cannot use
# the LAN copy: the Android app hard-codes a single URL, the public one, so it
# always arrives *here*.
#
# Tailscale's range by default, and deliberately nothing else. The tempting
# alternative was the household's own public address, which would have let the
# app show these on the home wifi with no VPN at all -- and it is the wrong
# signal: it treats "came from the same building" as "came in privately", it
# breaks silently the day the ISP changes the address, and on a shared or
# CGNAT-ed address it is not even the same household. LAN or VPN means LAN or
# VPN.
#
# 172.16.0.0/12 is *not* here on purpose: the VPS's own network lives in it, so
# a hop added in front of Caddy would turn every caller in the world into the
# house. A household that really uses it can say so in `cloud.vps.home_networks`
# and take that on knowingly.
#
# Matched against the *last* X-Forwarded-For hop (see `_client_ip`), the only
# element a caller cannot prepend -- so being inside can be had but not claimed.
# An unparseable entry is dropped rather than widened, because the mistake to
# avoid here is the one that publishes a camera feed.
DEFAULT_HOME_NETWORKS = "100.64.0.0/10"


def _parse_home_networks(raw: str) -> list:
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            logging.getLogger("home-chat").warning(
                "HOME_NETWORKS: ignoring unparseable entry %r", part)
    return nets


HOME_NETWORKS = _parse_home_networks(
    os.environ.get("HOME_NETWORKS", "").strip() or DEFAULT_HOME_NETWORKS)


def _from_house(request: Request) -> bool:
    """Whether this request came from inside the house.

    True for every request the copy on hub serves -- it is only reachable from
    the LAN, so the question is already answered -- and, on the public copy, for
    a caller whose address is on the VPN. Nothing else: a public address is
    away, including the household's own, and an empty `HOME_NETWORKS` makes this
    False for everybody.
    """
    if IS_LAN_COPY:
        return True
    # No early return for an empty list: `any()` over nothing is already False,
    # and a second path to the same answer is one a test cannot tell apart --
    # "empty means nobody" was covered by two lines and provable by neither.
    try:
        addr = ipaddress.ip_address(_client_ip(request))
    except ValueError:
        return False
    return any(addr in net for net in HOME_NETWORKS)

NTFY_BASE_URL = os.environ.get("NTFY_BASE_URL", "").rstrip("/")


# Which prefixes are house-only. The household's choice, set from
# `services.<name>.house_only` in config/home-stack.yml and passed to both
# copies of this proxy; the default is what this shipped with. HomeCore is told
# the same list and enforces it again on its own routes -- this is the outer of
# two gates, not the only one.
HOUSE_ONLY_APPS = {
    part.strip().lower()
    for part in os.environ.get("HOUSE_ONLY_APPS", "cameras").split(",")
    if part.strip()
}


def _lan_only(request: Request, app_key: str = "cameras") -> JSONResponse | None:
    """Refuse a house-only prefix to a caller who is not in the house.

    Was "not the house *copy*", which is the same thing for a browser and the
    wrong thing for the app: it always arrives at the public copy, so the
    cameras were refused to it on the home wifi too. `_from_house` keeps the
    refusal for the public internet and lifts it for the house's own address.

    404 rather than 403: off the VPN this prefix genuinely is not served here,
    and that is also what every unrouted prefix already answers. A 403 would
    say "this exists and you may not have it", which is a slightly worse thing
    to publish from a public host.
    """
    if app_key.lower() not in HOUSE_ONLY_APPS:
        return None
    if _from_house(request):
        return None
    return JSONResponse({"error": "No disponible fuera de la casa"}, status_code=404)

# Only these path prefixes are proxied to HomeCore; everything else is handled here.
PROXY_PREFIX = "/chat"

# Hop-by-hop / connection headers we never forward in either direction.
_DROP_REQUEST_HEADERS = {
    "host", "cookie", "content-length", "connection", "accept-encoding",
    "x-proxy-secret", "x-proxy-user", "x-proxy-lan",
}
_DROP_RESPONSE_HEADERS = {
    "content-length", "transfer-encoding", "connection", "content-encoding",
    "set-cookie", "server", "date", "keep-alive",
}

app = FastAPI(title="home-chat proxy")

# uvicorn owns the handlers; this only needs a name to log under, so the
# tunnel going down is greppable next to the access lines.
logger = logging.getLogger("home-chat")

# Long-lived client. read=None so SSE streams (/chat/send up to ~5min, /chat/events
# indefinite) are never cut off by a read timeout.
_client = httpx.AsyncClient(
    verify=VERIFY_UPSTREAM_TLS,
    timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=None),
    follow_redirects=False,
)


@app.on_event("startup")
async def _startup():
    devices.init_db()


@app.on_event("shutdown")
async def _shutdown():
    await _client.aclose()


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
def _current_user(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    username = payload.get("sub")
    if not username:
        return None
    # `AUTH_MODE=upstream` is the whole point of this machine: it holds no user
    # store, because the household's password hashes stay on the household's
    # own hardware. The login path already knows that and asks the home machine
    # -- and then this one insisted on a local record anyway.
    #
    # So every login succeeded, set a cookie, and every request after it was
    # 401: `find_user` reads /app/data/users.json, which by design is not there.
    # The phone showed a working sign-in and then nothing worked, which is the
    # worst arrangement of those two facts.
    #
    # The token is the proof. It is signed with this proxy's own key, it has
    # already been decoded above, and it carries an expiry. A local record adds
    # nothing to that here -- it is the *other* mode, where this machine does
    # hold the store, that has a record to check.
    if auth.AUTH_MODE != "upstream" and not find_user(username):
        return None
    return username


def _set_session_cookie(response: Response, username: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        create_token(username),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=SECURE_COOKIE,
        samesite="lax",
        path="/",
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/ping")
async def ping():
    return {"ok": True}


@app.get("/healthz")
async def healthz():
    """Report whether the upstream HomeCore is reachable over the tunnel."""
    try:
        r = await _client.get(f"{HOMECORE_LOCAL_URL}/ping", timeout=5.0)
        return {"ok": True, "upstream": r.is_success}
    except Exception as e:
        return JSONResponse({"ok": True, "upstream": False, "error": str(e)}, status_code=200)


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------
LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Ingresar — Alfred</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      min-height: 100vh; display: flex; align-items: center; justify-content: center;
      background: radial-gradient(ellipse at 60% 30%, #f5e6c8 0%, #ecdbc0 40%, #e0cba8 100%);
      font-family: system-ui, -apple-system, sans-serif; color: #3D2B1F;
    }}
    .card {{
      background: rgba(255,248,235,.93); border: 1.5px solid #d9b98a; border-radius: 20px;
      padding: 44px 48px 48px; width: 90vw; max-width: 380px;
      box-shadow: 0 8px 40px rgba(92,61,46,.18), 0 2px 8px rgba(92,61,46,.10);
    }}
    h1 {{ font-family: Georgia,serif; font-size: 1.6rem; color: #5C3D2E; margin-bottom: 6px; }}
    .sub {{ font-size: .88rem; color: #9E7A5A; margin-bottom: 32px; }}
    label {{ display: block; font-size: .82rem; font-weight: 600; color: #7A5030; text-transform: uppercase; letter-spacing: .06em; margin-bottom: 6px; }}
    input {{
      display: block; width: 100%; padding: 11px 14px;
      border: 1.5px solid #d9b98a; border-radius: 10px; background: #fffaf2;
      font-size: 1rem; color: #3D2B1F; outline: none; transition: border-color .2s; margin-bottom: 20px;
    }}
    input:focus {{ border-color: #D4845A; background: #fff; }}
    .btn {{
      width: 100%; padding: 13px; border: none; border-radius: 999px;
      background: linear-gradient(135deg,#D4845A,#B8622E); color: #fff;
      font-size: 1rem; font-weight: 600; cursor: pointer; letter-spacing: .03em;
      box-shadow: 0 4px 16px rgba(180,98,46,.30); transition: transform .15s, box-shadow .15s; margin-top: 6px;
    }}
    .btn:hover {{ transform: translateY(-2px); box-shadow: 0 8px 24px rgba(180,98,46,.38); }}
    .btn:active {{ transform: translateY(0); }}
    .error {{ background: #fce8e8; border: 1px solid #e8a0a0; color: #8B2020; border-radius: 8px; padding: 10px 14px; font-size: .88rem; margin-top: 16px; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>🤵 Alfred</h1>
    <p class="sub">Enter your credentials to continue</p>
    <form id="login-form" method="POST" action="/login" novalidate>
      <label for="username">Username</label>
      <input id="username" name="username" type="text" autocomplete="username"
             inputmode="text" placeholder="your username" value="{username}" required autofocus>
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password" placeholder="••••••••" required>
      <input id="challenge-field" type="hidden" name="challenge" value="">
      <input id="signature-field" type="hidden" name="signature" value="">
      <button class="btn" type="submit">Sign in</button>
      {error}
    </form>
    <form id="enroll-form" novalidate style="display:none">
      <label for="code">Activation code</label>
      <input id="code" name="code" type="text" inputmode="numeric" placeholder="6-digit code" required autofocus>
      <button class="btn" type="submit">Activate device</button>
      <div id="enroll-error" class="error" style="display:none"></div>
    </form>
  </div>
  {script}
</body>
</html>"""

# Runs only inside the Android app's WebView (window.AndroidDeviceKey only
# exists there - see MainActivity.kt's DeviceKeyBridge). A plain browser has no
# bridge, so this script no-ops and the login form submits with empty
# challenge/signature fields, which the server always rejects by design.
# The 6-digit code itself can only ever be *generated* over SSH
# (manage_devices.py) - this script just redeems one the admin already gave
# the family member out-of-band, handling the public-key exchange
# automatically so the human only ever has to type the code.
LOGIN_SCRIPT = """<script>
(function() {
  var bridge = window.AndroidDeviceKey;
  if (!bridge) return;

  var loginForm = document.getElementById('login-form');
  var enrollForm = document.getElementById('enroll-form');

  if (!bridge.isEnrolled()) {
    loginForm.style.display = 'none';
    enrollForm.style.display = 'block';
    enrollForm.addEventListener('submit', function(e) {
      e.preventDefault();
      var code = document.getElementById('code').value.trim();
      var publicKey = bridge.getPublicKey();
      var errEl = document.getElementById('enroll-error');
      fetch('/enroll/complete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({code: code, public_key: publicKey}),
      }).then(function(r) { return r.json(); }).then(function(data) {
        if (data.ok) { window.location.reload(); }
        else { errEl.textContent = 'Codigo invalido o vencido.'; errEl.style.display = 'block'; }
      }).catch(function() {
        errEl.textContent = 'Error de red.'; errEl.style.display = 'block';
      });
    });
    return;
  }

  loginForm.addEventListener('submit', function(e) {
    e.preventDefault();
    fetch('/login/challenge').then(function(r) { return r.json(); }).then(function(data) {
      document.getElementById('challenge-field').value = data.challenge;
      document.getElementById('signature-field').value = bridge.sign(data.challenge);
      loginForm.submit();
    });
  });
})();
</script>"""


def _render_login(username: str = "", error: str = "") -> HTMLResponse:
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    body = LOGIN_HTML.format(username=html.escape(username), error=error_html, script=LOGIN_SCRIPT)
    return HTMLResponse(body)


@app.get("/login")
async def login_page(request: Request):
    if _current_user(request):
        return RedirectResponse("/", status_code=302)
    return _render_login()


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    challenge: str = Form(""),
    signature: str = Form(""),
):
    username = username.strip()
    if auth.AUTH_MODE == "upstream":
        # The proxy holds no user store. Credentials go to the home machine
        # over the tunnel, and an unreachable upstream is a failed login.
        ok = await auth.verify_upstream(
            _client, HOMECORE_LOCAL_URL,
            os.environ.get("PROXY_SHARED_SECRET", ""),
            username, password,
        )
    else:
        user = find_user(username)
        ok = bool(user) and verify_password(password, user.get("hash", ""))
    # Which half failed, in the log. Both answers are a 200 carrying the login
    # page again, so from the outside -- and from an app that retries -- "wrong
    # password" and "unknown device" are the same event. They have completely
    # different fixes, and telling them apart meant guessing.
    #
    # The username is already in every access line; the password is not logged
    # and never should be.
    if not ok:
        logger.info("login refused for %s: credentials", username)
        return _render_login(username, "Wrong username or password.")
    # Restricts login to enrolled devices (the Android app) - a plain browser
    # never has a bridge to produce challenge/signature, so this always fails
    # there regardless of password correctness. See devices.py.
    if not devices.verify_login_device(username, challenge, signature):
        logger.info("login refused for %s: device not enrolled or challenge "
                    "expired (challenge=%s signature=%s)", username,
                    "yes" if challenge else "missing",
                    "yes" if signature else "missing")
        return _render_login(username, "Este dispositivo no esta autorizado.")
    logger.info("login accepted for %s", username)
    resp = RedirectResponse("/", status_code=302)
    _set_session_cookie(resp, username)
    return resp


@app.get("/healthz/upstream")
async def healthz_upstream():
    """Is the tunnel to the home machine up?

    The proxy serves nothing of its own, so this is the only health signal that
    means anything: a 200 from /ping proves this container started, and a
    container that started with no route home has nothing to answer with.
    """
    try:
        response = await _client.get(
            HOMECORE_LOCAL_URL.rstrip("/") + "/healthz", timeout=10.0)
        reachable = response.status_code < 500
    except Exception:
        reachable = False
    return JSONResponse(
        {"status": "ok" if reachable else "no-upstream",
         "upstream": HOMECORE_LOCAL_URL},
        status_code=200 if reachable else 503,
    )


@app.get("/login/challenge")
async def login_challenge():
    return {"challenge": devices.issue_challenge()}


def _client_ip(request: Request) -> str:
    """The address the rate limiter counts against. The **last** hop, not the first.

    This app listens on loopback and is reached only through Caddy on this
    machine, so `request.client.host` is always 127.0.0.1 and every caller in
    the world shares one bucket -- blunt, but safe.

    `X-Forwarded-For` fixes that and has to be read from the right end. Caddy
    **appends** to whatever the caller sent rather than replacing it, so a
    request arriving with `X-Forwarded-For: 9.9.9.9` reaches this function as
    `9.9.9.9, <the address Caddy actually saw>`. Taking `[0]` returns the value
    the caller typed, which is worth being precise about: this address is the
    only key on the enrol rate limit, and that limit is the only thing standing
    between a **six-digit** code and somebody guessing it. A caller who picks
    their own key has an unlimited number of guesses at a million-wide space,
    and every one of them enrols a device on the household portal.

    The last element is the one Caddy wrote, from the peer it was actually
    talking to. Forged entries can only be prepended, never appended, so with
    one proxy in front -- which is this deployment -- the last element is the
    real client.

    `X-Real-IP` is deliberately not consulted. Nothing sets it here: the
    generated Caddy block (see `vps_caddy_snippet` in deploy.py) carries no
    `header_up`, so trusting it would mean trusting a header only a caller ever
    sends. If a hop is ever added in front of Caddy, this needs revisiting --
    the count of trusted hops is the whole of the assumption.
    """
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else "unknown"


@app.post("/enroll/complete")
async def enroll_complete(request: Request):
    body = await request.json()
    code = str(body.get("code", "")).strip()
    public_key = str(body.get("public_key", ""))
    label = str(request.headers.get("user-agent", ""))[:200]
    ip = _client_ip(request)
    username = devices.consume_enroll_code(code, public_key, label, ip)
    if not username:
        return JSONResponse({"ok": False}, status_code=400)
    return {"ok": True}


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@app.get("/")
async def root(request: Request):
    if _current_user(request):
        return RedirectResponse("/chat?embed=1", status_code=302)
    return RedirectResponse("/login", status_code=302)


# ---------------------------------------------------------------------------
# ntfy notification config for the Android app
# ---------------------------------------------------------------------------
@app.get("/api/ntfy-config")
async def ntfy_config_route(request: Request):
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "No autenticado"}, status_code=401)
    cfg = ntfy_config.get_ntfy_config(user)
    if not cfg or not cfg.get("topics"):
        return JSONResponse({"error": "not configured"}, status_code=404)
    return {"server": NTFY_BASE_URL, "topics": cfg["topics"], "token": cfg.get("token", "")}


# ---------------------------------------------------------------------------
# Reverse proxy: /chat*, /tasks*, /geo*, /grocery*  ->  HomeCore local
# ---------------------------------------------------------------------------
# What the family sees when the house cannot be reached.
#
# It used to be `{"error": "upstream unreachable: [Errno 111] Connection
# refused"}` rendered as plain text in the WebView — an httpx exception on
# screen, in English, with no way forward except closing the app and guessing
# when to try again. The tunnel does go down (a hub reboot, a Caddy restart,
# HomeCore redeploying), so this is a page the family will meet.
#
# It says three things and no more: it is not their phone, nobody has lost
# anything, and it is already checking. The retry loop is why this is a page
# rather than a better sentence — /healthz on the VPS reports whether the tunnel
# is up, so the page can heal itself the moment the house answers, which is the
# difference between "try again later" and not having to.
UNREACHABLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>No connection to the house — Alfred</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      min-height: 100vh; display: flex; align-items: center; justify-content: center;
      background: radial-gradient(ellipse at 60% 30%, #f5e6c8 0%, #ecdbc0 40%, #e0cba8 100%);
      font-family: system-ui, -apple-system, sans-serif; color: #3D2B1F; padding: 24px;
    }
    .card {
      background: rgba(255,248,235,.93); border: 1.5px solid #d9b98a; border-radius: 20px;
      padding: 40px 34px; width: 100%; max-width: 380px; text-align: center;
      box-shadow: 0 8px 40px rgba(92,61,46,.18), 0 2px 8px rgba(92,61,46,.10);
    }
    .icon { font-size: 2.6rem; line-height: 1; margin-bottom: 14px; }
    h1 { font-family: Georgia, serif; font-size: 1.35rem; color: #5C3D2E; margin-bottom: 10px; }
    p { font-size: .92rem; color: #7A5030; line-height: 1.55; }
    p + p { margin-top: 10px; }
    .btn {
      margin-top: 24px; width: 100%; padding: 13px; border: none; border-radius: 999px;
      background: linear-gradient(135deg,#D4845A,#B8622E); color: #fff;
      font-size: 1rem; font-weight: 600; cursor: pointer; letter-spacing: .03em;
      box-shadow: 0 4px 16px rgba(180,98,46,.30);
    }
    .status { margin-top: 16px; font-size: .78rem; color: #9E7A5A; min-height: 1.2em; }
    .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%;
           background: #B8622E; margin-right: 6px; animation: pulse 1.4s ease-in-out infinite; }
    @keyframes pulse { 0%, 100% { opacity: .25 } 50% { opacity: 1 } }
    @media (prefers-reduced-motion: reduce) { .dot { animation: none; opacity: .7 } }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">🏠</div>
    <h1>I can't reach the house</h1>
    <p>Alfred is fine and nothing you wrote has been lost. What is not
       answering right now is the house server.</p>
    <p>It is usually a moment: it is restarting or updating.</p>
    <button class="btn" id="retry">Try again</button>
    <div class="status" id="status"></div>
  </div>
  <script>
    var status = document.getElementById('status');
    var tries = 0;
    // Backs off to every 15s and keeps going: the usual outage is a redeploy
    // and the phone is often sitting on this page while it finishes.
    function delay() { return Math.min(15000, 2000 + tries * 2000); }
    async function probe(manual) {
      tries++;
      status.innerHTML = '<span class="dot"></span>Checking…';
      try {
        var r = await fetch('/healthz', { cache: 'no-store' });
        var d = await r.json();
        if (d && d.upstream) { location.reload(); return; }
      } catch (e) {}
      status.textContent = manual ? 'Still not answering.' : '';
      setTimeout(probe, delay());
    }
    document.getElementById('retry').addEventListener('click', function () {
      tries = 0;
      probe(true);
    });
    probe(false);
  </script>
</body>
</html>"""


def _unreachable(request: Request, detail: str) -> Response:
    """The answer when HomeCore cannot be reached, in the shape the caller can use.

    A page navigation gets a page — this is what the WebView paints, so it is
    the only thing the family actually sees. Everything else gets JSON, because
    every fetch in chat.html reads `.error`, and handing those an HTML document
    turns a clear failure into `JSON.parse: unexpected character`.

    The exception text stays in the log and out of the response. "[Errno 111]
    Connection refused" tells the family nothing they can act on, and it is the
    kind of detail a public host should not narrate to whoever asks.
    """
    logger.warning("upstream unreachable: %s", detail)
    accept = request.headers.get("accept", "")
    wants_page = "text/html" in accept and "application/json" not in accept
    if wants_page:
        return HTMLResponse(UNREACHABLE_HTML, status_code=503)
    return JSONResponse(
        {"error": "No puedo hablar con la casa ahora mismo. Reintentando…"},
        status_code=503,
    )


async def _forward_to_homeweb(request: Request, upstream: str, user: str) -> Response:
    """Reverse-proxy `request` to `upstream` on HomeCore local, injecting the
    trusted X-Proxy-Secret / X-Proxy-User pair and streaming the response back."""
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _DROP_REQUEST_HEADERS
    }
    fwd_headers["X-Proxy-Secret"] = PROXY_SHARED_SECRET
    fwd_headers["X-Proxy-User"] = user
    # Tells HomeCore this caller is in the house, so the Apps menu can show the
    # house-only tiles instead of leaving out links that would 404. Stripped
    # from the client's headers above with the other two, and decided from the
    # last X-Forwarded-For hop, so a phone can be at home but cannot say it is.
    if _from_house(request):
        fwd_headers["X-Proxy-Lan"] = "1"

    body = await request.body()
    upstream_req = _client.build_request(
        request.method, upstream, headers=fwd_headers, content=body,
    )
    try:
        upstream_resp = await _client.send(upstream_req, stream=True)
    except httpx.HTTPError as e:
        return _unreachable(request, str(e))

    resp_headers = {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in _DROP_RESPONSE_HEADERS
    }
    return StreamingResponse(
        upstream_resp.aiter_raw(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type"),
        background=BackgroundTask(upstream_resp.aclose),
    )


def _upstream_url(prefix: str, rest: str, query: str) -> str:
    upstream = f"{HOMECORE_LOCAL_URL}{prefix}{rest}"
    if query:
        upstream += f"?{query}"
    return upstream


@app.api_route("/chat{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_chat(request: Request, rest: str):
    user = _current_user(request)
    if not user:
        # A bare page navigation -> send to login; API/SSE calls -> 401.
        if request.method == "GET" and rest == "":
            return RedirectResponse("/login", status_code=302)
        return JSONResponse({"error": "No autenticado"}, status_code=401)
    return await _forward_to_homeweb(
        request, _upstream_url(PROXY_PREFIX, rest, request.url.query), user
    )


@app.api_route("/_code{rest:path}", methods=["GET"])
async def proxy_code(request: Request, rest: str):
    """`/_code/enter` — mint the ticket that opens opencode's own interface.

    Only this one path, and only GET. opencode is served on `dns.code`, a
    hostname of its own, because its assets and its API are absolute from the
    origin root; the session cookie here is host-only and does not reach it, so
    the portal hands over a signed sixty-second ticket instead of widening the
    cookie to every `.home` appliance.

    It has to be forwarded from *here* because minting requires the session,
    and the session lives on this name. The redeeming half (`/_code/auth`) and
    the check Caddy makes (`/_code/whoami`) are served on the other name and
    never come through this proxy.

    This list is an allowlist on purpose -- a path not named here 404s -- which
    is how `/_code/enter` came back `{"detail":"Not Found"}` before this
    existed.
    """
    user = _current_user(request)
    if not user:
        if request.method == "GET":
            return RedirectResponse("/login", status_code=302)
        return JSONResponse({"error": "No autenticado"}, status_code=401)
    return await _forward_to_homeweb(
        request, _upstream_url("/_code", rest, request.url.query), user
    )


@app.api_route("/tasks{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_tasks(request: Request, rest: str):
    """Tasks page + /tasks/api/* — used by the in-chat tasks panel and by
    notification action buttons. Same trusted-proxy forwarding as /chat, so
    HomeCore's @api_login_required / @tasks_admin_required handlers work
    unchanged (proxy requests are CSRF-exempt via g.is_proxy)."""
    user = _current_user(request)
    if not user:
        # Bare page navigation -> login; API calls -> 401.
        if request.method == "GET" and rest in ("", "/"):
            return RedirectResponse("/login", status_code=302)
        return JSONResponse({"error": "No autenticado"}, status_code=401)
    return await _forward_to_homeweb(
        request, _upstream_url("/tasks", rest, request.url.query), user
    )


@app.api_route("/geo{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_geo(request: Request, rest: str):
    """/geo/api/* — the app's geofence sync (GET /geo/api/geofences) and arrival
    reports (POST /geo/api/event). Same trusted-proxy forwarding as /chat and
    /tasks; HomeCore's @api_login_required handlers work unchanged."""
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "No autenticado"}, status_code=401)
    return await _forward_to_homeweb(
        request, _upstream_url("/geo", rest, request.url.query), user
    )


@app.api_route("/grocery{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_grocery(request: Request, rest: str):
    """Grocery page + /grocery/api/* — the shared shopping list, used by the
    in-chat grocery panel, the standalone /grocery page and notification taps.
    Same trusted-proxy forwarding as /chat and /tasks."""
    user = _current_user(request)
    if not user:
        # Bare page navigation -> login; API calls -> 401.
        if request.method == "GET" and rest in ("", "/"):
            return RedirectResponse("/login", status_code=302)
        return JSONResponse({"error": "No autenticado"}, status_code=401)
    return await _forward_to_homeweb(
        request, _upstream_url("/grocery", rest, request.url.query), user
    )


@app.api_route("/menu{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_menu(request: Request, rest: str):
    """Minuta page + /menu/api/* — the weekly lunch/dinner plan, used by the
    in-chat Minuta panel, the standalone /menu page and notification taps.
    Same trusted-proxy forwarding as /chat and /grocery.

    Without this the prefix simply isn't routed here and FastAPI 404s before
    anything reaches HomeCore — the page works on the LAN and only breaks
    off-VPN, which is exactly how it went unnoticed."""
    user = _current_user(request)
    if not user:
        # Bare page navigation -> login; API calls -> 401.
        if request.method == "GET" and rest in ("", "/"):
            return RedirectResponse("/login", status_code=302)
        return JSONResponse({"error": "No autenticado"}, status_code=401)
    return await _forward_to_homeweb(
        request, _upstream_url("/menu", rest, request.url.query), user
    )


@app.api_route("/stats{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_stats(request: Request, rest: str):
    """Consumo de Alfred — the token-usage page and /stats/api/*.

    Routed here for the same reason /menu and /camaras are: without the prefix
    FastAPI 404s before anything reaches HomeCore, so the tile works on the LAN
    and is a dead link in the app, which always comes through this proxy even
    on the home wifi. HomeCore does its own admin check on top of this — the
    page is Alex and Sam only."""
    user = _current_user(request)
    if not user:
        if request.method == "GET" and rest in ("", "/"):
            return RedirectResponse("/login", status_code=302)
        return JSONResponse({"error": "No autenticado"}, status_code=401)
    return await _forward_to_homeweb(
        request, _upstream_url("/stats", rest, request.url.query), user
    )


def _settings_gate(request, rest):
    """The login check these five share. Returns a response to send, or None."""
    user = _current_user(request)
    if user:
        return None, user
    if request.method == "GET" and rest in ("", "/"):
        return RedirectResponse("/login", status_code=302), None
    return JSONResponse({"error": "No autenticado"}, status_code=401), None


# The four Ajustes pages — Proyectos, Credenciales, Directorio familiar and
# Correos de Alfred — plus the family API the directory writes through. Same
# reason as /stats and /menu, and this is the sixth time: without the prefix
# FastAPI 404s here before anything reaches HomeWeb, so the page works on the
# LAN and is a dead link in the app, which comes through this proxy even on the
# home wifi.
#
# Written out one decorator at a time on purpose. A loop or a factory would say
# the same thing in fewer lines and be invisible to the check that exists to
# catch this exact bug — HomeWeb's test_app_tiles_reachable greps this file for
# `@app.api_route("/x{rest`, so a route it cannot see is a route nobody is
# guarding.
#
# HomeWeb does its own admin check on top of all five: the pages redirect a
# non-admin away and the APIs answer 403.
@app.api_route("/projects{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_projects(request: Request, rest: str):
    """Los proyectos de Alfred Programador."""
    early, user = _settings_gate(request, rest)
    if early:
        return early
    return await _forward_to_homeweb(
        request, _upstream_url("/projects", rest, request.url.query), user)


@app.api_route("/credentials{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_credentials(request: Request, rest: str):
    """Las credenciales que usan esos proyectos."""
    early, user = _settings_gate(request, rest)
    if early:
        return early
    return await _forward_to_homeweb(
        request, _upstream_url("/credentials", rest, request.url.query), user)


@app.api_route("/profiles{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_profiles(request: Request, rest: str):
    """El directorio familiar, y el /profiles/api que edita los buzones."""
    early, user = _settings_gate(request, rest)
    if early:
        return early
    return await _forward_to_homeweb(
        request, _upstream_url("/profiles", rest, request.url.query), user)


@app.api_route("/mailboxes{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_mailboxes(request: Request, rest: str):
    """Los correos de Alfred."""
    early, user = _settings_gate(request, rest)
    if early:
        return early
    return await _forward_to_homeweb(
        request, _upstream_url("/mailboxes", rest, request.url.query), user)


@app.api_route("/family{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_family(request: Request, rest: str):
    """The family API the directory page writes through. Alfred already reaches
    it from nanobot over the proxy secret; a browser does not, and the page is
    inert without it."""
    early, user = _settings_gate(request, rest)
    if early:
        return early
    return await _forward_to_homeweb(
        request, _upstream_url("/family", rest, request.url.query), user)


@app.api_route("/alfred{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_alfred(request: Request, rest: str):
    """Rendimiento de Alfred — the profiler panel and the data call behind it.

    Older than the four above and dead in the app for just as long: it has been
    in the menu since the panel was built and was never routed here, so tapping
    it has always 404'd off the LAN. Found by the menu-link check in HomeWeb's
    test_app_tiles_reachable, which until now only looked at the Casa tiles.

    HomeWeb serves the page itself and adds the profiler secret to the data
    call, so nothing here needs to know it."""
    early, user = _settings_gate(request, rest)
    if early:
        return early
    return await _forward_to_homeweb(
        request, _upstream_url("/alfred", rest, request.url.query), user)


@app.api_route("/camaras{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_camaras(request: Request, rest: str):
    """The cameras, which HomeCore now serves itself at /camaras/ so the page
    knows which member is looking. Routed here for the same reason /menu is:
    without it FastAPI 404s before anything reaches HomeCore, and the app —
    which always comes through this proxy, even on the home wifi — sees a
    dead link while a browser on the LAN sees a working page.

    **House only.** A camera feed behind a login on a public host is not the
    boundary anybody wants around it, so off the VPN this prefix simply is not
    served. Checked before the auth branch: whether it exists here does not
    depend on who is asking."""
    if (blocked := _lan_only(request, "cameras")) is not None:
        return blocked
    user = _current_user(request)
    if not user:
        if request.method == "GET":
            return RedirectResponse("/login", status_code=302)
        return JSONResponse({"error": "No autenticado"}, status_code=401)
    return await _forward_to_homeweb(
        request, _upstream_url("/camaras", rest, request.url.query), user
    )




@app.api_route("/files{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_files(request: Request, rest: str):
    """Archivos — the share browser, and the other dead tile in Casa apps.

    Not a widening of what the app can already reach: the chat's "My files"
    panel reads, downloads and deletes on the same share through /chat, and
    HomeCore applies the same per-person access here (own folder, `familia`,
    grants; the whole share only for Alex and Sam).
    """
    user = _current_user(request)
    if not user:
        if request.method == "GET":
            return RedirectResponse("/login", status_code=302)
        return JSONResponse({"error": "No autenticado"}, status_code=401)
    return await _forward_to_homeweb(
        request, _upstream_url("/files", rest, request.url.query), user
    )


# Read-only, because setting a theme is Alfred's job on the LAN side. These
# three are the ones every HomeCore page reaches for: the sheet, the version the
# 20s poller watches, and the backdrop the sheet points at.
_THEME_GET_PATHS = frozenset((".css", "/version", "/backdrop"))


@app.api_route("/theme{rest:path}", methods=["GET"])
async def proxy_theme(request: Request, rest: str):
    """`/theme.css`, `/theme/version` and `/theme/backdrop` -> HomeCore local.

    Without this the prefix is not routed here and FastAPI 404s before anything
    reaches HomeCore, so in the app every page falls back to The House colours
    baked into its own `:root` — the member's theme never paints, and the
    `AndroidTheme` bridge is handed the default olive on every load. Same shape
    as the /menu gap: works on the LAN, silently wrong off-VPN.
    """
    if rest not in _THEME_GET_PATHS:
        return JSONResponse({"error": "No encontrado"}, status_code=404)
    user = _current_user(request)
    if not user:
        # Never an error and never a redirect for the sheet — it is a <link> in
        # the head of every page, including the login page. An empty sheet
        # leaves The House exactly as it is, which is what HomeCore itself answers.
        if rest == ".css":
            return Response("", media_type="text/css",
                            headers={"Cache-Control": "no-cache"})
        return JSONResponse({"error": "No autenticado"}, status_code=401)
    return await _forward_to_homeweb(
        request, _upstream_url("/theme", rest, request.url.query), user
    )
