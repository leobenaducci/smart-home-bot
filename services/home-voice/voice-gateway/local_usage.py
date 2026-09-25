"""Tell HomeCore what the house's own models did, so the usage page can say so.

The /stats page answered "what is Alfred costing" only for the half of the
work that is billed. Speaking a sentence and transcribing a room are real
work on real hardware -- on this house's single 12 GB card, the same card the
vision model and the camera detector are competing for -- and they were
invisible there because they cost no money.

Same shape as nanobot's `agent/usage_report.py`, and for the same reasons:
fire-and-forget, never blocks a turn, never raises. A voice turn must not get
slower, and must never fail, because the thing counting it is down. It is a
separate copy rather than a shared module because these are different
containers built from different contexts -- the same reasoning the i18n
catalogues are staged into each image rather than fetched from one origin.

Records are buffered and flushed in the background, so a portal that is
restarting costs the household a measurement rather than a pause in the
kitchen.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import aiohttp

LOG = logging.getLogger("voice-gateway.usage")

# Empty means "do not report", which is the default and is not a failure: a
# household running the gateway without the portal should not see a warning
# per sentence about a page it does not have.
HOMECORE_URL = os.environ.get("HOMECORE_URL", "").rstrip("/")
# Derived by the deployer from PROXY_SHARED_SECRET, never stored. See
# `_service_token` in HomeCore -- a service token, not a member's, so this
# container cannot reach anything that expects a person.
SERVICE_TOKEN = os.environ.get("USAGE_SERVICE_TOKEN", "")

_FLUSH_SECONDS = 15.0
_TIMEOUT_S = 10.0
# A bounded buffer, dropped from the front. An unreachable portal must cost a
# fixed amount of memory in a process that also holds audio: unbounded here
# would be a slow leak that ends as a killed gateway mid-sentence.
_MAX_BUFFERED = 500

_buffer: list[dict] = []
_task: asyncio.Task | None = None
# The loop the gateway runs on, remembered the first time a record
# arrives on it, so a record made from a worker thread can still be
# scheduled rather than silently buffered forever.
_loop: asyncio.AbstractEventLoop | None = None


def enabled() -> bool:
    return bool(HOMECORE_URL and SERVICE_TOKEN)


def record(kind: str, engine: str, model: str, ms: float,
           units: int = 0, ok: bool = True, route: str = "") -> None:
    """One completed call. Safe from anywhere; never raises.

    `units` is per kind, matching HomeCore's `_LOCAL_KINDS`: characters for
    tts, milliseconds of audio for asr. `route` is which internal path asked
    -- a room turn and an announcement are the same engine doing work for two
    different reasons, and only one of them is worth a slower, better voice.
    """
    if not enabled():
        return
    try:
        _buffer.append({"kind": kind, "engine": engine or "", "model": model or "",
                        "route": route or "", "ms": int(ms),
                        "units": int(units), "ok": bool(ok)})
        if len(_buffer) > _MAX_BUFFERED:
            del _buffer[:len(_buffer) - _MAX_BUFFERED]
        _ensure_worker()
    except Exception:              # pragma: no cover - instrumentation only
        LOG.debug("could not record a local usage row", exc_info=True)


def _ensure_worker() -> None:
    """Make sure something is draining the buffer.

    Records can arrive from a worker thread -- anything reached through
    `run_in_executor` -- where there is no running loop to create a task on.
    Returning silently there would be correct exactly once and wrong forever
    after: every record from that thread takes the same path, the buffer
    fills to _MAX_BUFFERED and nothing ever drains it unless some other call
    happens to land on the loop thread. So the loop is captured the first
    time one is seen, and later threads schedule onto it.
    """
    global _task, _loop
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = _loop               # called off the loop; use the one we saw
        if loop is None or loop.is_closed():
            return                 # nothing running yet; the next call retries
        try:
            loop.call_soon_threadsafe(_ensure_worker)
        except RuntimeError:
            pass
        return
    _loop = loop
    _task = loop.create_task(_flush_loop())


async def _flush_loop() -> None:
    while True:
        await asyncio.sleep(_FLUSH_SECONDS)
        if not _buffer:
            return                 # idle: stop, and start again on the next call
        batch, _buffer[:] = list(_buffer), []
        try:
            await _post(batch)
        except Exception as e:
            # The records are gone rather than retried forever. A retry queue
            # that survives an outage is a queue that replays a day of stale
            # timings into a page about what happened this morning.
            LOG.debug("usage flush failed, dropped %d rows: %s", len(batch), e)


async def _post(records: list[dict]) -> None:
    # verify=False in effect: HomeCore serves its own self-signed cert on the
    # home network, same as every other call to it from this stack.
    conn = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_S)
    async with aiohttp.ClientSession(connector=conn, timeout=timeout) as s:
        async with s.post(f"{HOMECORE_URL}/stats/api/local",
                          json={"records": records},
                          headers={"X-Service-Token": SERVICE_TOKEN}) as r:
            if r.status >= 400:
                LOG.debug("usage report refused (%s)", r.status)


class timed:
    """`with timed() as t:` ... `t.ms` -- wall time in milliseconds.

    A context manager rather than two `time.monotonic()` calls at each site,
    because the interesting paths here have three exits (success, a fallback
    to another engine, an exception) and the one that gets forgotten is
    always the one that matters.
    """

    __slots__ = ("_start", "ms")

    def __enter__(self) -> "timed":
        self._start = time.monotonic()
        self.ms = 0.0
        return self

    def __exit__(self, *exc) -> bool:
        self.ms = (time.monotonic() - self._start) * 1000.0
        return False
