"""Structured progress-event helpers shared by agent runtimes."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from nanobot.agent.hook import AgentHookContext


def on_progress_accepts(cb: Callable[..., Any], name: str) -> bool:
    """True if *cb* can be passed a keyword arg named *name* (either it declares
    the parameter or it accepts **kwargs)."""
    try:
        sig = inspect.signature(cb)
    except (TypeError, ValueError):
        return False
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return True
    return name in sig.parameters


def on_progress_accepts_tool_events(cb: Callable[..., Any]) -> bool:
    return on_progress_accepts(cb, "tool_events")


async def invoke_on_progress(
    on_progress: Callable[..., Awaitable[None]],
    content: str,
    *,
    tool_hint: bool = False,
    tool_events: list[dict[str, Any]] | None = None,
    thinking: str | None = None,
    usage: dict[str, Any] | None = None,
    alive: bool = False,
    plan: dict[str, Any] | None = None,
) -> None:
    # A liveness tick carries no content, so a callback that cannot be told
    # what it is would receive an empty progress message and forward it to
    # whoever is reading. Skipped outright rather than downgraded.
    if alive and not on_progress_accepts(on_progress, "alive"):
        return
    kwargs: dict[str, Any] = {"tool_hint": tool_hint}
    if alive:
        kwargs["alive"] = True
    # Only forward the optional kwargs a given callback actually declares — the
    # outbound-bus fallback progress handler accepts none of them, so passing
    # one unconditionally kills the whole turn with "unexpected keyword
    # argument '<name>'".
    if tool_events and on_progress_accepts_tool_events(on_progress):
        kwargs["tool_events"] = tool_events
    if thinking is not None and on_progress_accepts(on_progress, "thinking"):
        kwargs["thinking"] = thinking
    if usage is not None and on_progress_accepts(on_progress, "usage"):
        kwargs["usage"] = usage
    if plan is not None:
        # The checklist of a turn working through a plan (tools/plan.py). A
        # callback that cannot draw one gets nothing rather than a line per
        # step: WhatsApp would otherwise receive the plan as messages.
        if not on_progress_accepts(on_progress, "plan"):
            return
        kwargs["plan"] = plan
    await on_progress(content, **kwargs)


def build_tool_event_start_payload(tool_call: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "phase": "start",
        "call_id": str(getattr(tool_call, "id", "") or ""),
        "name": getattr(tool_call, "name", ""),
        "arguments": getattr(tool_call, "arguments", {}) or {},
        "result": None,
        "error": None,
        "files": [],
        "embeds": [],
    }


def tool_event_result_extras(result: Any) -> tuple[list[Any], list[Any]]:
    if not isinstance(result, dict):
        return [], []
    files = result.get("files") if isinstance(result.get("files"), list) else []
    embeds = result.get("embeds") if isinstance(result.get("embeds"), list) else []
    return files, embeds


def build_tool_event_finish_payloads(context: AgentHookContext) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    count = min(len(context.tool_calls), len(context.tool_results), len(context.tool_events))
    for idx in range(count):
        tool_call = context.tool_calls[idx]
        result = context.tool_results[idx]
        event = context.tool_events[idx] if isinstance(context.tool_events[idx], dict) else {}
        status = event.get("status")
        phase = "end" if status == "ok" else "error"
        files, embeds = tool_event_result_extras(result)
        payload = {
            "version": 1,
            "phase": phase,
            "call_id": str(getattr(tool_call, "id", "") or ""),
            "name": getattr(tool_call, "name", ""),
            "arguments": getattr(tool_call, "arguments", {}) or {},
            "result": result if phase == "end" else None,
            "error": None,
            "files": files,
            "embeds": embeds,
        }
        if phase == "error":
            if isinstance(result, str) and result.strip():
                payload["error"] = result.strip()
            else:
                payload["error"] = str(event.get("detail") or "Tool execution failed")
        payloads.append(payload)
    return payloads
