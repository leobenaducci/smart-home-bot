"""household: telemetry the fork kept on every run (B5).

- **Profiler feed**: upstream already observes every physical provider call
  (``set_llm_call_observer``, for its own ``llm_usage`` store). The gateway
  chains this observer behind it, so ``household/profiling.py`` -- the data
  behind ``/v1/debug/profile`` -- sees the same calls, attributed to the turn
  or task span that is open, with the provider that served each one.
- **Spans**: every household turn is a ``turn`` span and every sub-agent run a
  ``task`` span (``HouseholdAgentLoop`` / ``HouseholdSubagentManager``).
- **Usage report**: one POST per completed run to HomeCore's ``/chat/usage``
  (``household/usage_report.py``, verbatim), which is where ``usage.db`` lives:
  model, tokens, tool names, **the provider that served it** (a fallback is
  billed to the fallback), stop reason, route (tier, label, source,
  escalated_from) and the turn block (latency, errors, event codes).
"""

from __future__ import annotations

from typing import Any, Callable

from loguru import logger

from nanobot.household.profiling import PROFILER, current_span


def usage_dict(usage: Any) -> dict[str, int]:
    """Upstream's ``LLMUsage`` (or a fork-shaped dict) as the fork's usage dict."""
    if not usage:
        return {}
    if isinstance(usage, dict):
        return {k: int(v) for k, v in usage.items() if isinstance(v, (int, float))}
    out = {"prompt_tokens": int(getattr(usage, "input_tokens", 0) or 0),
           "completion_tokens": int(getattr(usage, "output_tokens", 0) or 0),
           "total_tokens": int(getattr(usage, "total_tokens", 0) or 0)}
    cached = getattr(usage, "cache_read_tokens", None)
    if cached:
        out["cached_tokens"] = int(cached)
    return out


def observe_call(record: Any) -> None:
    """One upstream ``LLMCallRecord`` into the profiler, attributed to the open span."""
    try:
        span = current_span()
        if span is not None and getattr(record, "finish_reason", "") != "error":
            span.note(provider=record.provider, served_by=record.model)
        duration_s = max(0.0, float(record.duration_ms) / 1000.0)
        PROFILER.record_call(
            model=record.model, served_by=record.model, api_base=None,
            duration_s=duration_s, request_s=duration_s, attempts=1,
            finish_reason=record.finish_reason, usage=usage_dict(record.usage),
            stream=bool(record.stream), error=record.error_kind,
        )
    except Exception:  # noqa: BLE001 -- instrumentation never fails a call
        logger.debug("profiler observer failed", exc_info=True)


def chain(first: Callable[[Any], None] | None) -> Callable[[Any], None]:
    """Upstream's observer, then the household's."""
    def observer(record: Any) -> None:
        if first is not None:
            first(record)
        observe_call(record)

    return observer


def tool_names(messages: list[dict[str, Any]] | None) -> list[str]:
    names: list[str] = []
    for m in messages or []:
        for call in m.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call, dict) else None
            name = (fn or {}).get("name") if isinstance(fn, dict) else None
            if name:
                names.append(name)
    return names


def route_dict(route: Any) -> dict[str, Any] | None:
    if route is None:
        return None
    out = {"tier": route.tier, "label": route.label, "source": route.source}
    if getattr(route, "ms", None) is not None:
        out["classifier_ms"] = route.ms
    if getattr(route, "escalated_from", None):
        out["escalated"] = True
        out["escalated_from"] = route.escalated_from
    return out


def report(session_key: str | None, model: str | None, usage: Any, *, tools: list[str] | int = 0,
           route: dict[str, Any] | None = None, provider: str | None = None,
           stop_reason: str | None = None, span: Any = None,
           latency_ms: float | None = None) -> None:
    from nanobot.household.usage_report import report_usage

    span = span or current_span()
    provider = provider or (span.outcome.get("provider") if span is not None else None)
    try:
        report_usage(session_key, model, usage_dict(usage), tools, route=route,
                     provider=provider, stop_reason=stop_reason, span=span,
                     latency_ms=latency_ms)
    except Exception:  # noqa: BLE001
        logger.debug("usage report failed", exc_info=True)


__all__ = ["chain", "observe_call", "report", "report_cancelled", "route_dict", "tool_names",
           "usage_dict"]


def report_cancelled(session_key: str | None, model: str | None, span: Any,
                     route: dict[str, Any] | None = None) -> None:
    """A13: bill what a cancelled run spent (fork ``_billed_if_cut``).

    Stop, or the chat's time limit, cancels a turn before its report runs,
    so the costliest turns of the day were missing from usage.db."""
    from nanobot.household.usage_report import report_cut

    try:
        report_cut(session_key, model, span, route=route, stop_reason="cancelled")
    except Exception:  # noqa: BLE001 -- instrumentation never changes a cancel
        pass

