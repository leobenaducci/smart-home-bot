"""Deliver websocket-channel output to HomeCore when nobody is subscribed.

The WebSocket channel can only fan a message out to connections that are
attached to its chat_id *at that moment*. That is fine for a conversation, and
wrong for everything the agent produces on its own schedule: a subagent that
finishes three hours later has no one to talk to, so ``send()`` logged
"no active subscribers" and dropped the answer on the floor.

HomeCore (the family portal that owns every user's identity, chat history and
push notifications) is the real destination for those. This posts them there —
history plus an ntfy push — over the same per-user derived proxy token the
skills use, so an instance can only ever act as its own user.

Only ``homeweb:<user>:<day>[:<conv>]`` chat_ids are relayed (``<conv>`` is the
conversation-start ms HomeCore appends so each conversation gets its own model
session); anything else belongs to a different client and is left alone.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from loguru import logger

HOMEWEB_CHAT_PREFIX = "homeweb:"
# Generous: HomeCore writes history and may push ntfy before answering, and this
# never blocks the agent loop (see `post`).
_TIMEOUT_S = 30.0
# Commentary lines waiting to be relayed. Bounded because a task narrating
# faster than the portal accepts must cost a backlog, not the container's
# memory; the oldest are dropped first (see `_post_progress`).
_PROGRESS_QUEUE_MAX = 500


def _homeweb_base_url() -> str:
    """Where HomeCore lives, derived the same way the skills derive it."""
    url = os.environ.get("HOMECORE_URL") or os.environ.get(
        "TASKS_API_URL", "").replace("/tasks/api", "")
    return url.rstrip("/")


class HomeCoreRelay:
    """Fire-and-forget POSTs to HomeCore's /chat/agent-event."""

    def __init__(self) -> None:
        self.url = _homeweb_base_url()
        self.user = os.environ.get("HOMECORE_USER_ID", "")
        self.token = os.environ.get("HOMECORE_PROXY_TOKEN", "")
        # Keep strong refs to in-flight posts; a bare create_task can be GC'd
        # mid-flight, which would silently lose exactly the message we are here
        # to rescue.
        self._inflight: set[asyncio.Task[None]] = set()
        # Progress commentary goes through a queue with a single worker instead
        # of one task per event, for two reasons the other payloads don't have:
        # there are hundreds of them per task, not two, so a fresh TLS
        # connection each would be absurd; and they only make sense in order,
        # which concurrent fire-and-forget posts do not preserve.
        self._progress_q: asyncio.Queue[dict[str, Any]] | None = None
        self._progress_worker: asyncio.Task[None] | None = None
        self._progress_dropped = 0
        if not self.enabled:
            logger.info(
                "homeweb relay disabled (needs HOMECORE_URL/TASKS_API_URL, "
                "HOMECORE_USER_ID and HOMECORE_PROXY_TOKEN)"
            )

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.user and self.token)

    @staticmethod
    def owns(chat_id: Any) -> bool:
        return isinstance(chat_id, str) and chat_id.startswith(HOMEWEB_CHAT_PREFIX)

    def post(self, payload: dict[str, Any]) -> None:
        """Schedule one relay POST. Never awaited by the caller: the agent loop
        must not slow down (or fail) because the portal is having a bad day."""
        if not self.enabled:
            return
        if payload.get("event") == "progress":
            self._post_progress(payload)
            return
        try:
            task = asyncio.get_running_loop().create_task(self._post(payload))
        except RuntimeError:
            logger.warning("homeweb relay: no event loop, dropping {}", payload.get("type"))
            return
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    def _post_progress(self, payload: dict[str, Any]) -> None:
        """Enqueue one commentary line for the ordered worker.

        Dropping the *oldest* when the queue is full is deliberate: if the
        portal is slow, what someone opening the panel wants to see is what the
        task is doing now, not the backlog from ten minutes ago.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop — nothing to relay to, and nothing to log about
        if self._progress_q is None:
            self._progress_q = asyncio.Queue(maxsize=_PROGRESS_QUEUE_MAX)
        if self._progress_worker is None or self._progress_worker.done():
            self._progress_worker = loop.create_task(self._drain_progress())
        while True:
            try:
                self._progress_q.put_nowait(payload)
                return
            except asyncio.QueueFull:
                try:
                    self._progress_q.get_nowait()
                    self._progress_q.task_done()
                    self._progress_dropped += 1
                    if self._progress_dropped % 100 == 1:
                        logger.warning(
                            "homeweb relay: progress backlog full, dropped {} lines",
                            self._progress_dropped,
                        )
                except asyncio.QueueEmpty:
                    return

    async def _send(self, client: "httpx.AsyncClient", payload: dict[str, Any],
                    *, quiet: bool = False) -> None:
        """The one request this relay makes. The endpoint and the two auth
        header names live here only — a token-scheme change that fixed the
        start/done path and missed the progress path would be invisible.

        *quiet* keeps a rejected commentary line at debug: hundreds of them ride
        this path per task, and losing one costs a row in a panel.
        """
        r = await client.post(
            f"{self.url}/chat/agent-event",
            json=payload,
            headers={"X-Proxy-Secret": self.token, "X-Proxy-User": self.user},
        )
        if r.status_code >= 400:
            (logger.debug if quiet else logger.warning)(
                "homeweb relay: {} rejected ({}): {}",
                payload.get("type"), r.status_code, r.text[:200],
            )

    async def _drain_progress(self) -> None:
        """One connection, one line at a time, in order."""
        assert self._progress_q is not None
        try:
            async with httpx.AsyncClient(verify=False, timeout=_TIMEOUT_S) as client:
                while True:
                    payload = await self._progress_q.get()
                    try:
                        await self._send(client, payload, quiet=True)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # noqa: BLE001 — a line, not the task
                        logger.debug("homeweb relay: progress failed: {}", e)
                    finally:
                        self._progress_q.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # The worker restarts on the next _post_progress (it checks .done()),
            # so a dead client costs the backlog, not the feature.
            logger.warning("homeweb relay: progress worker stopped: {}", e)

    async def _post(self, payload: dict[str, Any]) -> None:
        try:
            # verify=False: HomeCore serves its own self-signed cert on the home
            # network — same reason the skills call it with `curl -k`.
            async with httpx.AsyncClient(verify=False, timeout=_TIMEOUT_S) as client:
                await self._send(client, payload)
            logger.debug("homeweb relay: {} delivered", payload.get("type"))
        except Exception as e:
            logger.warning("homeweb relay: {} failed: {}", payload.get("type"), e)

    async def aclose(self) -> None:
        """Wait briefly for in-flight posts (used on channel shutdown)."""
        if self._progress_worker is not None:
            # The backlog is commentary about tasks that are themselves being
            # torn down. Cancel rather than drain — nobody is waiting on it, and
            # shutdown should not block on a portal that may be down too.
            self._progress_worker.cancel()
            try:
                await self._progress_worker
            except asyncio.CancelledError:
                # The worker's cancellation, not ours. Swallowing it as part of
                # `except (CancelledError, Exception)` also swallowed a
                # cancellation delivered into aclose itself, which then went on
                # to block for _TIMEOUT_S on the wait below.
                pass
            except Exception as e:  # noqa: BLE001
                logger.warning("homeweb relay: progress worker teardown: {}", e)
            self._progress_worker = None
            self._progress_q = None      # a reused relay must not replay a dead task's backlog
        if not self._inflight:
            return
        await asyncio.wait(set(self._inflight), timeout=_TIMEOUT_S)
