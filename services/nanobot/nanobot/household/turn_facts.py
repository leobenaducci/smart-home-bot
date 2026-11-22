"""household: the current time and the answering model, on every turn (fork parity).

v0.4.1 no longer tells the model the date or time at all (upstream removed the
``Current Time`` line from its runtime context), so "what do I have today?",
"remind me tomorrow" or "is the shop open now?" are answered blind. The fork
put ``Current Time`` first in Runtime Context, with the model serving the turn
under it, and ``(outage fallback - X is not answering)`` while a fallback
stood in, because asked which model it is the agent otherwise answers from the
roster in AGENTS.md. Both lines go in a runtime-context block, which upstream
appends after the user's text and never stores, so the cached prompt prefix
is untouched.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from nanobot.runtime_context import RuntimeContextBlock, wrap_runtime_context_lines

SOURCE = "household_turn_facts"


def current_time_str(timezone: str | None = None, now: datetime | None = None) -> str:
    """The fork's ``Current Time`` text: ``2026-11-22 10:05 (Sunday) (America/Santiago, UTC-03:00)``."""
    try:
        tz = ZoneInfo(timezone) if timezone else None
    except Exception:  # noqa: BLE001 -- an unknown zone falls back to the host's
        tz = None
    if now is None:
        now = datetime.now(tz=tz) if tz else datetime.now().astimezone()
    offset = now.strftime("%z")
    offset_fmt = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset
    tz_name = (timezone if tz else None) or (time.strftime("%Z") or "UTC")
    return f"{now.strftime('%Y-%m-%d %H:%M (%A)')} ({tz_name}, UTC{offset_fmt})"


def model_line(loop: Any, runtime: Any) -> str | None:
    model = getattr(runtime, "model", None) or getattr(loop, "model", None)
    if not model:
        return None
    provider = getattr(runtime, "provider", None) or getattr(loop, "provider", None)
    try:
        from nanobot.household.outages import serving_label

        label = serving_label(provider, model)
    except Exception:  # noqa: BLE001 -- the time line must not depend on this
        label = model
    if label != model and label.endswith(f" (fallback for {model})"):
        serving = label[: -len(f" (fallback for {model})")]
        return f"Model: {serving} (outage fallback — {model} is not answering)"
    return f"Model: {model}"


def provider_for(loop: Any):
    """A runtime-context provider bound to *loop*."""

    async def turn_facts(request: Any) -> RuntimeContextBlock | None:
        timezone = getattr(getattr(loop, "context", None), "timezone", None)
        lines = [f"Current Time: {current_time_str(timezone)}",
                 model_line(loop, getattr(request, "runtime", None))]
        return RuntimeContextBlock(source=SOURCE, content=wrap_runtime_context_lines(lines))

    return turn_facts
