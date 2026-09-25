"""Report what a turn cost, so the house can answer that question later.

Nothing recorded token usage anywhere. It is in the provider's response and it
is logged at DEBUG, and then it is gone — so "which model should Alfred run on"
and "what is actually expensive here" were answerable only by re-measuring with
synthetic prompts, which is a proxy for the real traffic and not the traffic.

One fire-and-forget POST per completed run, to HomeCore, which is the system of
record for everything else the family can look at. The session key goes over
raw and HomeCore decides what it means: the taxonomy of scopes (ev-notif,
ev-task, fin, dsg, a profession, an ordinary chat) lives there with CHAT_SPACES,
and duplicating it here would mean two places that disagree after the first
change.

Never blocks and never raises. This is instrumentation — a turn must not fail,
or slow down, because the thing counting it is down.
"""

import asyncio
import os
from typing import Any

import httpx
from loguru import logger

_TIMEOUT_S = 10.0

# Strong refs to in-flight posts. A bare create_task can be garbage-collected
# mid-flight — the same lesson homeweb_relay records, and the failure is
# invisible here because nobody is waiting for the result.
_inflight: set[asyncio.Task] = set()


def _homeweb() -> tuple[str, str, str]:
    url = os.environ.get("HOMECORE_URL") or os.environ.get(
        "TASKS_API_URL", "").replace("/tasks/api", "")
    return (url.rstrip("/"), os.environ.get("HOMECORE_USER_ID", ""),
            os.environ.get("HOMECORE_PROXY_TOKEN", ""))


async def _post(payload: dict[str, Any]) -> None:
    base, user, token = _homeweb()
    try:
        # verify=False: HomeCore serves its own self-signed cert on the home
        # network, same as every other call to it from here.
        async with httpx.AsyncClient(verify=False, timeout=_TIMEOUT_S) as client:
            r = await client.post(
                f"{base}/chat/usage", json=payload,
                headers={"X-Proxy-Secret": token, "X-Proxy-User": user})
            if r.status_code >= 400:
                logger.debug("usage report refused ({}): {}", r.status_code, r.text[:120])
    except Exception as e:
        logger.debug("usage report failed: {}", e)


# Enough names to see the shape of a turn, few enough that a runaway loop
# cannot post a megabyte of them. A turn that called forty tools is interesting
# for the fact that it called forty, not for the fortieth name.
_MAX_TOOL_NAMES = 40


def report_usage(session_key: str | None, model: str | None,
                 usage: dict[str, int] | None,
                 tools_used: int | list[str] | None = 0,
                 route: dict[str, Any] | None = None) -> None:
    """Record one completed run. Safe to call from anywhere in the loop.

    `tools_used` takes the runner's list of tool names. The count alone was
    what this sent at first, and it answers "was this turn busy" without ever
    answering "busy doing what" — which is the question actually worth the
    storage. An int is still accepted, because a caller that only has a count
    should not have to invent names to report it.
    """
    if not usage:
        return
    base, user, token = _homeweb()
    if not (base and user and token):
        return  # an instance with no HomeCore (the voice one) simply does not report
    names = list(tools_used) if isinstance(tools_used, (list, tuple)) else []
    payload = {
        "session_key": session_key or "",
        "model": model or "",
        "usage": {k: int(v) for k, v in usage.items() if isinstance(v, (int, float))},
        # Kept as well as the names: HomeCore has rows recorded before names
        # existed, and a count that stops meaning "how many" the moment the
        # list is truncated would make those rows disagree with these.
        "tools": len(names) if names else int(tools_used or 0),
        "tool_names": names[:_MAX_TOOL_NAMES],
    }
    if route:
        # Which tier answered and who decided -- see agent/classify.py. The
        # number worth watching is escalations per label: an escalation from
        # `action` is the classifier being wrong, and that is measurable only
        # if the label travels with the bill.
        payload["route"] = {k: route[k] for k in (
            "tier", "label", "source", "classifier_ms", "escalated", "escalated_from",
        ) if k in route}
    try:
        task = asyncio.create_task(_post(payload))
    except RuntimeError:
        return  # no running loop; nothing to attach to and nothing worth raising for
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
