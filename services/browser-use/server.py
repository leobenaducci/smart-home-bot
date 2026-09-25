"""browser-use as an MCP server over HTTP, so the assistants can reach it.

browser-use's own MCP mode is **stdio** -- it expects to be spawned by the thing
that talks to it. That does not fit here twice over: the assistants run in their
own containers, and spawning this one from inside them would mean Chromium in
the assistant image, which is the ~1.5 GB this service exists to keep out of it.
nanobot speaks stdio, sse and streamableHttp (`nanobot/agent/tools/mcp.py`), so
the fix is to serve the same tools over HTTP from here.

Two tools, deliberately:

    browse(task, url="")  open a page and do something on it, in words
    read(url)             open a page and give back what it says

`read` exists so the common case never reaches the agent loop. "What does this
page say" is a fetch, and routing it through a model that decides what to click
costs a minute and can click something. If the page is static, this is the tool.

Everything below is built around one idea: **a browser session lasts one task**.
No profile is persisted, nothing is logged in across calls, and the browser is
closed when the task ends. A household assistant that stays logged into things
is a different and much larger decision than the one switching this on makes.

And one more, which is the reason `_guard` exists: **the URL is untrusted**. It
is chosen by a model that has just read somebody else's web page, so a page can
ask for one. This only opens the public internet -- see "Where this is allowed
to go" below for what that rules out, and for the one knob that widens it.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import socket
import time
from urllib.parse import urlparse

import uvicorn
from fastapi import Body, FastAPI, HTTPException
from mcp.server.fastmcp import FastMCP

LOG = logging.getLogger("browser-use")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

PORT = int(os.environ.get("BROWSER_USE_PORT", "21034"))

# The model that decides what to click. The house's own by default -- slower at
# this than a frontier model, and the pages it reads stay on this machine, which
# is the trade this stack makes everywhere else. Pointing it at a paid API means
# every page it opens is sent there.
MODEL = os.environ.get("BROWSER_USE_MODEL", "ollama:qwen3.5:4b")
BASE_URL = os.environ.get("BROWSER_USE_BASE_URL", "")

# A task that has not finished by now is not going to. browser-use loops --
# look, decide, click, look -- and a page that never satisfies the goal loops
# until something stops it. The assistant's own turn timeout is 180s, so this
# has to be under it or the caller gives up first and leaves a browser running.
TASK_TIMEOUT_S = float(os.environ.get("BROWSER_USE_TIMEOUT_S", "150"))

# How many steps the agent may take. Belt to the timeout's braces: a fast site
# can loop a lot inside 150s, and every step is a model call.
MAX_STEPS = int(os.environ.get("BROWSER_USE_MAX_STEPS", "25"))

# ---------------------------------------------------------------------------
# Where this is allowed to go
# ---------------------------------------------------------------------------
# The URL is chosen by a model reading somebody else's web page, which makes it
# untrusted input in a way the rest of this stack's URLs are not. This container
# sits on nanobot-net and is given `host.docker.internal` on purpose, for
# Ollama -- so without a guard, a page that says "now open
# http://192.168.1.10:21002" is an instruction this carries out.
#
# Deliberately **not** wired to `site.lan_cidr`, which is what the assistants'
# own `ssrfWhitelist` reads. That exemption says the household's own network is
# the household's own, and it is granted to nanobot, which takes its
# instructions from a person. This takes its instructions from whatever page it
# just read, so the house LAN is the one place it must not reach. Empty is the
# shipped state and blocks every private range; a household that genuinely
# needs one names it here and nowhere else.
ALLOW_CIDRS = [c.strip() for c in
               os.environ.get("BROWSER_USE_ALLOW_CIDRS", "").split(",") if c.strip()]

_ALLOW_NETS = []
for _cidr in ALLOW_CIDRS:
    try:
        _ALLOW_NETS.append(ipaddress.ip_network(_cidr, strict=False))
    except ValueError:
        # Skipped rather than fatal, the same way the assistants' whitelist
        # treats an unparseable entry: a typo should narrow what is reachable,
        # never stop the service from booting.
        LOG.warning("BROWSER_USE_ALLOW_CIDRS: ignoring unparseable entry %r", _cidr)

# An IP literal is refused by `block_ip_addresses` below, so what is left is
# names -- and these are the names that mean this house rather than the
# internet. `.home` is the suffix `dns:` uses; host.docker.internal is the host
# gateway this container is handed for Ollama; the last two globs are the
# containers the deployer renders per member (`nanobot-<id>`).
#
# Read this for what it is: a **denylist**, and upstream applies it by name
# only. A bare name it does not spell is allowed -- Docker's resolver answers
# for every container on nanobot-net by bare name, and a plugin's container is
# one this list has never heard of. So this narrows the second hop; it does not
# close it. The guarantee is `_guard`, which resolves before anything opens.
PROHIBITED_DOMAINS = [
    "localhost", "*.localhost", "*.local", "*.home", "*.lan", "*.internal",
    "host.docker.internal",
    # The containers this stack itself puts on nanobot-net. crawl4ai is a
    # fetch-anything primitive and code-broker hands out the household's code,
    # so these are the two worth spelling even though the list cannot be
    # complete.
    "crawl4ai", "audio-cpp", "code-broker", "browser-use",
    "nanobot-*", "whatsapp-bridge-*",
]

# Only when nothing is allowed, because the flag is all-or-nothing: it refuses
# every IP-literal URL, which is the right default and the wrong one for a
# household that has just said which addresses it means. Upstream checks it
# *before* any allow list, so there is no way to keep it on and still reach the
# one address the household named.
#
# The cost is worth saying out loud rather than discovering: naming any CIDR
# turns IP-literal blocking off for *every* address on every navigation after
# the first, including loopback and 169.254.169.254. `_guard` still refuses
# those at the door, so the entry URL is unaffected -- what widens is the link
# the model may follow on a page it has just read.
_BLOCK_IPS = not _ALLOW_NETS
if _ALLOW_NETS:
    LOG.warning("BROWSER_USE_ALLOW_CIDRS is set (%s): the browser will no "
                "longer refuse IP-literal URLs on navigations after the first. "
                "The entry URL is still checked.",
                ", ".join(str(n) for n in _ALLOW_NETS))


async def _guard(url: str) -> str | None:
    """Why this URL may not be opened, or None if it may.

    Said as a sentence, like every other refusal here: a model reads it and
    decides what to do next, and "blocked" tells it nothing it can act on.

    The resolution is the point. A literal address is the easy case and the
    browser profile refuses those on its own; a name pointed at 192.168.x.x is
    the one that gets through a check written against the string.

    Every way a URL can be malformed answers with a sentence too. This URL was
    chosen by a model reading somebody else's page, so "malformed" is a thing
    that page gets to choose: `http://[::1` raises from `urlparse` and a label
    over 63 characters raises `UnicodeError` -- a ValueError, not a
    `gaierror` -- out of the resolver. Either one escaping this function is a
    500 on a route whose whole job is to refuse politely.
    """
    try:
        parsed = urlparse(url)
        scheme, host = parsed.scheme, parsed.hostname
    except ValueError:
        return "that is not a URL this can parse."
    if scheme not in ("http", "https"):
        # file:// reads this container's own disk, and chrome:// and
        # devtools:// drive the browser rather than a page in it.
        return (f"{scheme or 'that'}: is not a scheme this can open. "
                f"Use http or https.")
    if not host:
        return "that URL has no host in it."
    try:
        # On the loop's resolver thread, not this one. `getaddrinfo` blocks
        # until the resolver answers or gives up -- seconds, for a name chosen
        # to be slow -- and doing that inline stops uvicorn answering anything,
        # /healthz included, which is how a refused URL becomes an unhealthy
        # container.
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return f"{host} does not resolve to anything."
    except Exception as exc:                                      # noqa: BLE001
        LOG.warning("could not resolve %r: %s: %s", host, type(exc).__name__, exc)
        return f"{host} is not a host name this can look up."
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:                    # nothing sane resolves to this
            return f"{host} resolves to something this cannot read as an address."
        if any(ip in net for net in _ALLOW_NETS):
            continue
        # `is_global` is the whole judgement: false for private ranges,
        # loopback, link-local, CGNAT (which is Tailscale) and IPv6 ULA.
        if not ip.is_global:
            return (f"{host} resolves to {ip}, which is on a private network. "
                    f"This browser only opens the public internet.")
    return None

# The MCP server the assistants actually talk to. The REST routes below are
# kept as they were: `test/smoke.py` drives `POST /read`, and it is the only
# thing that ever checks whether the browser in this image still works.
#
# `stateless_http=True` because there is nothing for a session id to point at.
# A browser session here is created and killed inside a single call (`_run`),
# so no state outlives the request that made it. Stateless also lets a caller
# POST `tools/list` without an `initialize` handshake first -- one fewer round
# trip, and one fewer thing that can be broken on a caller this service cannot
# see.
#
# The route is spliced into this app's own router at the bottom of this file
# rather than mounted with `app.mount("/mcp", ...)`. `streamable_http_app()`
# does not return a bare handler: it returns a Starlette app that *already*
# carries the route at `streamable_http_path`, so mounting it under `/mcp`
# serves it at `/mcp/mcp` and leaves `/mcp` a 404. The URL the assistants carry
# is `http://browser-use:21034/mcp`, exactly, so the route has to answer at
# exactly that -- and splicing the inner route into this router is what puts
# it there. (Mounting the app at `/` instead would work, but it would also
# shadow every route above with the MCP app's own 404s.)
mcp_server = FastMCP("browser-use", stateless_http=True,
                     streamable_http_path="/mcp")
_mcp_app = mcp_server.streamable_http_app()


@contextlib.asynccontextmanager
async def _lifespan(_app):
    """Run the MCP session manager's task group for the life of the process.

    Not optional and not obvious: without it the route exists, answers, and
    fails on every request -- which looks like a protocol problem rather than a
    missing startup step.
    """
    async with mcp_server.session_manager.run():
        yield


app = FastAPI(title="browser-use", lifespan=_lifespan)

# One at a time. Chromium is the memory here, and two agents browsing at once on
# a box that also holds the assistants is how this becomes the reason something
# else got OOM-killed.
_lock = asyncio.Lock()
_ready = False


def _llm():
    """The model, built the way browser-use wants it."""
    from browser_use import ChatOpenAI          # OpenAI-compatible, incl. Ollama

    kwargs = {"model": MODEL.split(":", 1)[-1] if MODEL.startswith("ollama:") else MODEL}
    if BASE_URL:
        kwargs["base_url"] = BASE_URL
    elif MODEL.startswith("ollama:"):
        kwargs["base_url"] = os.environ.get(
            "OLLAMA_URL", "http://host.docker.internal:11434") + "/v1"
        # Ollama's OpenAI surface wants *a* key and does not check it.
        kwargs.setdefault("api_key", "ollama")
    return ChatOpenAI(**kwargs)


async def _run(task: str, url: str | None) -> dict:
    from browser_use import Agent, BrowserSession
    from browser_use.browser.profile import BrowserProfile

    if url:
        refused = await _guard(url)
        if refused:
            return {"ok": False, "error": refused}

    # A fresh session per task, closed in `finally`. See the module docstring:
    # nothing is kept, so nothing is logged in the next time.
    #
    # The profile is the half of the guard that outlives the first page.
    # `_guard` judges the URL the caller handed over; these judge every
    # navigation after it, including a link the model decides to click on a
    # page that asked it to -- which is the case a check at the door cannot
    # see, and the one prompt injection actually uses.
    #
    # Narrower than `_guard`, and knowing which is which matters: upstream
    # matches `prohibited_domains` against the name in the URL and never
    # resolves it, so a public name pointed at a private address and any bare
    # container name this list does not spell are both allowed on the second
    # hop. `block_ip_addresses` is the rest of it, and it is off entirely once
    # a household names a CIDR (see `_BLOCK_IPS`).
    session = BrowserSession(browser_profile=BrowserProfile(
        headless=True, keep_alive=False,
        block_ip_addresses=_BLOCK_IPS,
        prohibited_domains=PROHIBITED_DOMAINS))
    goal = f"Go to {url} and then: {task}" if url else task
    started = time.monotonic()
    try:
        agent = Agent(task=goal, llm=_llm(), browser_session=session)
        result = await asyncio.wait_for(
            agent.run(max_steps=MAX_STEPS), timeout=TASK_TIMEOUT_S)
        return {
            "ok": True,
            "text": str(result.final_result() or "").strip(),
            "steps": len(getattr(result, "history", []) or []),
            "seconds": round(time.monotonic() - started, 1),
        }
    except asyncio.TimeoutError:
        # Said as a sentence, because a model reads this and decides what to do
        # next. "504" tells it nothing it can act on.
        return {"ok": False,
                "error": f"the page did not get there within {TASK_TIMEOUT_S:.0f}s. "
                         f"It may need a login, or the task may need to be smaller."}
    except Exception as exc:                                      # noqa: BLE001
        LOG.exception("browse failed")
        return {"ok": False, "error": f"the browser could not do that: {exc}"}
    finally:
        # Always. A session left open holds a Chromium process for the life of
        # the container, and nothing else here would ever close it.
        try:
            await session.kill()
        except Exception:                                         # noqa: BLE001
            LOG.warning("could not close the browser session cleanly")


@app.get("/healthz")
async def healthz() -> dict:
    """Whether this can do its job, not merely whether it is listening.

    The import is the check: `browser_use` pulls playwright, and a browser
    binary that did not install leaves an import that raises rather than a
    process that fails on the first real request.
    """
    global _ready
    if not _ready:
        try:
            import browser_use  # noqa: F401
            _ready = True
        except Exception as exc:                                  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"not ready: {exc}")
    return {"ok": True, "model": MODEL, "busy": _lock.locked()}


# The words `read` hands the agent. Named once because the REST route and the
# MCP tool both hand over the same ones, and a difference between the two would
# be one service quietly doing two things.
READ_TASK = "Read the page and report its main text content."


async def _browse(task: str, url: str | None) -> dict:
    """`browse`, for whichever surface asked. See the two callers below."""
    if not task:
        return {"ok": False, "error": "'task' is missing: say what to do on the page"}
    if _lock.locked():
        # Refused rather than queued: the caller has a 180s turn and queueing
        # behind another task spends it waiting for something it cannot see.
        return {"ok": False, "error": "the browser is busy with another task; try again shortly"}
    async with _lock:
        return await _run(task, url)


async def _read(url: str) -> dict:
    """`read`, for whichever surface asked.

    No busy check, unlike `_browse`: this one queues. It is the short call, and
    smoke.py is usually its only caller.
    """
    if not url:
        return {"ok": False, "error": "'url' is missing"}
    async with _lock:
        return await _run(READ_TASK, url)


@app.post("/browse")
async def browse_route(payload: dict = Body(...)) -> dict:
    return await _browse(str(payload.get("task") or "").strip(),
                         str(payload.get("url") or "").strip() or None)


@app.post("/read")
async def read_route(payload: dict = Body(...)) -> dict:
    """The static case, without the agent loop. See the module docstring."""
    return await _read(str(payload.get("url") or "").strip())


# ---------------------------------------------------------------------------
# The same two things, as MCP tools
# ---------------------------------------------------------------------------
# The docstrings below are not documentation. They are the tool descriptions
# the model reads on every turn, and they are the whole basis on which it
# chooses between this and the crawler -- so each says what it is *for* and
# what it costs, rather than what it does.


def _as_text(out: dict) -> str:
    """What a model gets back: a sentence, either way.

    `_run` already phrases its failures for a reader that has to decide what to
    do next, so they are handed over as they are. A tool that answers a model
    with `{"ok": false}` leaves it to guess whether to retry, and it guesses
    yes.
    """
    if not out.get("ok"):
        return out.get("error") or "the browser could not do that."
    text = str(out.get("text") or "").strip()
    if not text:
        # Deliberately not phrased as a failure: the browser did its job and
        # the page had nothing to give, which is not a thing to retry.
        return "the browser opened the page but it gave back no text."
    return text


@mcp_server.tool()
async def browse(task: str, url: str = "") -> str:
    """Open a page in a real browser and carry out a task on it.

    For what reading a page cannot do: something behind a login, or behind a
    form that has to be filled in. It clicks and types as this household, so
    when the answer is simply somewhere in the page's text, read the page
    instead.

    Slow, and one at a time: a model decides every click, so a task runs for
    tens of seconds, and if the browser is already busy this says so rather
    than making you wait.
    """
    return _as_text(await _browse(task.strip(), url.strip() or None))


@mcp_server.tool()
async def read(url: str) -> str:
    """Open one page in a real browser and give back what it says.

    Only for a page an ordinary fetch cannot get: one that renders its text
    with JavaScript, or that refuses a crawler. It spends a browser and a model
    to do it, so it is the fallback and not the first thing to reach for.
    """
    return _as_text(await _read(url.strip()))


# Spliced into this app's router rather than mounted -- see the comment on
# `mcp_server`. Last, so everything above is already registered.
app.router.routes.extend(_mcp_app.router.routes)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
