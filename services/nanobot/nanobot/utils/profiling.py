"""Where a turn's time and tokens actually went.

The logs answer "what happened" and the usage report answers "what did the
month cost". Neither answers the question you ask when something is slow or
expensive: *which part*. A turn is one wall-clock number hiding several — time
waiting on the model, time waiting on retries the model caused, time in tools,
and our own overhead between them — and a turn's bill is one token number
hiding the only lever that matters here, which is how much of the prompt the
gateway served from cache. This module records all of it, in memory, and hands
it back through /v1/debug/profile.

Three record kinds and one derived view:

* **call** — one trip through `LLMProvider._run_with_retry`, which is every
  LLM request this process makes: the main turn, sub-agents, `describe_image`,
  memory consolidation, the heartbeat. It carries the model asked for and the
  one that answered, the ladder (how many requests, how long spent waiting
  between them), and the token split including `cached_tokens`.
* **tool** — one `_run_tool`, with its duration and whether it failed. Tools in
  one iteration run concurrently, so their durations overlap; the span keeps
  the union of their intervals as well as the sum, and only the union can be
  subtracted from wall time.
* **span** — a `turn` (AgentLoop), a `task` (a sub-agent) or a `job` (dream,
  consolidation, heartbeat). Calls and tools attach to whichever span is
  current, through a ContextVar, so nothing has to be threaded through the
  provider signature. Spans do not nest: a sub-agent's runner records into the
  task span, not into a turn inside it.
* **chat** — spans grouped by session key, aggregated on read.

Everything is bounded ring buffers and small integers. The cost per call is a
`perf_counter()` and a dict append, which is nothing next to a network round
trip, so this is on by default: a profiler you have to turn on is one that is
off during the incident you needed it for.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urlparse

from nanobot.utils.shell_log import redact

# Ring sizes. A busy family instance does ~300 notification turns a day plus a
# couple of dozen of everything else, so these hold hours of calls and days of
# spans — long enough to still be there when somebody says "it was slow this
# morning", small enough to be a rounding error against the process.
_MAX_CALLS = 1000
_MAX_TOOLS = 1000
_MAX_SPANS = 500

_TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens")


def _host(api_base: str | None) -> str:
    """`https://opencode.ai/zen/v1` -> `opencode.ai`. Empty for locals."""
    if not api_base:
        return ""
    try:
        return urlparse(api_base).hostname or ""
    except ValueError:
        return ""


def _merge_spans(intervals: list[tuple[float, float]]) -> float:
    """Total wall time covered by *intervals*, counting overlap once.

    Tools in one iteration are gathered, so three tools of 2s each that ran
    together cost the turn 2 seconds and not 6. Summing them would make the
    breakdown add up to more than the turn took, which is the kind of number
    that sends somebody optimising the wrong thing.
    """
    if not intervals:
        return 0.0
    ordered = sorted(intervals)
    total = 0.0
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        if start > cur_end:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    return total + (cur_end - cur_start)


@dataclass
class Span:
    """One turn, task or job in flight, and the counters it collects."""

    kind: str                       # turn | task | job
    session_key: str
    label: str = ""                 # profile name, task label, job name
    channel: str = ""
    chat_id: str = ""
    model: str = ""                 # what the routing asked for
    started_at: float = field(default_factory=time.time)
    _t0: float = field(default_factory=time.perf_counter)

    calls: int = 0
    call_s: float = 0.0             # time inside provider requests
    retry_wait_s: float = 0.0       # time asleep between them
    call_errors: int = 0
    tokens: dict[str, int] = field(default_factory=dict)
    models_served: dict[str, int] = field(default_factory=dict)

    tools: int = 0
    tool_errors: int = 0
    tool_s: float = 0.0             # sum, overlap included
    _tool_intervals: list[tuple[float, float]] = field(default_factory=list)
    tool_names: dict[str, int] = field(default_factory=dict)

    outcome: dict[str, Any] = field(default_factory=dict)

    def note(self, **fields: Any) -> None:
        """Attach outcome fields known only once the span is done."""
        self.outcome.update({k: v for k, v in fields.items() if v is not None})

    def _record(self) -> dict[str, Any]:
        duration_s = time.perf_counter() - self._t0
        tools_wall_s = _merge_spans(self._tool_intervals)
        # What is left after the model and the tools: building the prompt,
        # loading skills, consolidating memory inline, writing the session.
        # It is the number nobody looks at, and the one that is ours to fix.
        other_s = max(0.0, duration_s - self.call_s - self.retry_wait_s - tools_wall_s)
        prompt = self.tokens.get("prompt_tokens", 0)
        cached = self.tokens.get("cached_tokens", 0)
        return {
            "ts": self.started_at,
            "kind": self.kind,
            "session_key": self.session_key,
            "label": self.label,
            "channel": self.channel,
            "chat_id": self.chat_id,
            "model": self.model,
            "duration_ms": round(duration_s * 1000, 1),
            "llm_ms": round(self.call_s * 1000, 1),
            "retry_wait_ms": round(self.retry_wait_s * 1000, 1),
            "tools_wall_ms": round(tools_wall_s * 1000, 1),
            "tools_sum_ms": round(self.tool_s * 1000, 1),
            "other_ms": round(other_s * 1000, 1),
            "calls": self.calls,
            "call_errors": self.call_errors,
            "tools": self.tools,
            "tool_errors": self.tool_errors,
            "tool_names": dict(self.tool_names),
            "models_served": dict(self.models_served),
            "tokens": dict(self.tokens),
            # The lever, precomputed because it is the first thing anyone
            # looks at: an ordinary turn re-sends the whole history and the
            # gateway serves most of it from cache. When this drops, the bill
            # moved and the model did not.
            "cache_hit_pct": round(100.0 * cached / prompt, 1) if prompt else None,
            **self.outcome,
        }


_CURRENT: ContextVar[Span | None] = ContextVar("nanobot_profiling_span", default=None)


class Profiler:
    """In-memory profile of recent calls, tools and spans."""

    def __init__(self) -> None:
        self.enabled = True
        self._lock = threading.Lock()
        self._calls: deque[dict[str, Any]] = deque(maxlen=_MAX_CALLS)
        self._tools: deque[dict[str, Any]] = deque(maxlen=_MAX_TOOLS)
        self._spans: deque[dict[str, Any]] = deque(maxlen=_MAX_SPANS)
        self._live: dict[int, Span] = {}
        self._started_at = time.time()

    # --- collection --------------------------------------------------------

    @contextmanager
    def span(
        self,
        kind: str,
        session_key: str = "",
        *,
        label: str = "",
        channel: str = "",
        chat_id: str = "",
        model: str = "",
    ) -> Iterator[Span]:
        """Open a span and make it the one calls and tools attach to.

        A no-op shell when disabled, so call sites need no `if`. Nested spans
        are allowed and the inner one wins for attribution — that only happens
        when a job runs inside a turn, and the inner label is the useful one.
        """
        current = Span(
            kind=kind, session_key=session_key, label=label,
            channel=channel, chat_id=chat_id, model=model,
        )
        if not self.enabled:
            yield current
            return
        token = _CURRENT.set(current)
        with self._lock:
            self._live[id(current)] = current
        try:
            yield current
        finally:
            _CURRENT.reset(token)
            with self._lock:
                self._live.pop(id(current), None)
                self._spans.append(current._record())

    def record_call(
        self,
        *,
        model: str,
        served_by: str | None,
        api_base: str | None,
        duration_s: float,
        request_s: float,
        attempts: int,
        finish_reason: str,
        usage: dict[str, Any] | None,
        stream: bool,
        error: str | None = None,
        messages: int = 0,
        content_chars: int = 0,
        reasoning_chars: int = 0,
        tool_calls: int = 0,
        answerless: bool = False,
    ) -> None:
        """One trip through the retry ladder, however many requests that took."""
        if not self.enabled:
            return
        wait_s = max(0.0, duration_s - request_s)
        tokens = {k: int(usage.get(k) or 0) for k in _TOKEN_KEYS if usage and usage.get(k)}
        prompt = tokens.get("prompt_tokens", 0)
        record = {
            "ts": time.time(),
            "model": model,
            "served_by": served_by or model,
            "gateway": _host(api_base),
            "duration_ms": round(duration_s * 1000, 1),
            "request_ms": round(request_s * 1000, 1),
            "retry_wait_ms": round(wait_s * 1000, 1),
            "attempts": attempts,
            "finish_reason": finish_reason,
            "stream": stream,
            "messages": messages,
            "content_chars": content_chars,
            "reasoning_chars": reasoning_chars,
            "tool_calls": tool_calls,
            # The failure this house has already been bitten by and cannot see
            # in a log: the model spends its whole completion budget reasoning
            # and returns `length` with an empty message, which reaches the
            # family as Alfred not answering and reaches the log as a success.
            # `answerless` is the same event seen one layer up: the retry
            # ladder now turns an empty completion into an error so the
            # fallback can answer, which would otherwise erase the very thing
            # this field exists to count. The cause is the model's; the
            # `error` finish_reason is ours.
            "empty": answerless or (
                finish_reason != "error" and not content_chars and not tool_calls),
            "truncated": finish_reason == "length",
            "tokens": tokens,
            "cache_hit_pct": (
                round(100.0 * tokens.get("cached_tokens", 0) / prompt, 1) if prompt else None
            ),
        }
        if error:
            # Through `redact` for the same reason the shell log is: this
            # buffer holds provider exception text, it is served by
            # /v1/debug/profile and proxied to a browser, and a credential
            # echoed back in a 401 body is exactly the shape that leaked once
            # already. Redact before truncating, or a marker gets cut in half.
            record["error"] = redact(error)[:200]
        span = _CURRENT.get()
        if span is not None:
            record["session_key"] = span.session_key
            record["scope"] = f"{span.kind}:{span.label}" if span.label else span.kind
            span.calls += 1
            span.call_s += request_s
            span.retry_wait_s += wait_s
            if finish_reason == "error":
                span.call_errors += 1
            served = served_by or model
            span.models_served[served] = span.models_served.get(served, 0) + 1
            for key, value in tokens.items():
                span.tokens[key] = span.tokens.get(key, 0) + value
        else:
            record["scope"] = "unscoped"
        with self._lock:
            self._calls.append(record)

    def record_tool(
        self,
        *,
        name: str,
        duration_s: float,
        status: str,
        started_at: float,
        detail: str = "",
        result_chars: int = 0,
    ) -> None:
        """One tool execution. *started_at* is a perf_counter reading, so the
        overlapping ones can be unioned rather than summed."""
        if not self.enabled:
            return
        record = {
            "ts": time.time(),
            "name": name,
            "duration_ms": round(duration_s * 1000, 1),
            "status": status,
            "result_chars": result_chars,
        }
        if detail:
            # A tool's failure text is raw stdout/stderr for the exec tool --
            # only the copy handed to log_exec is redacted, not the one it
            # returns. Same buffer, same doorway, same treatment.
            record["detail"] = redact(detail)[:200]
        span = _CURRENT.get()
        if span is not None:
            record["session_key"] = span.session_key
            record["scope"] = f"{span.kind}:{span.label}" if span.label else span.kind
            span.tools += 1
            span.tool_s += duration_s
            span._tool_intervals.append((started_at, started_at + duration_s))
            span.tool_names[name] = span.tool_names.get(name, 0) + 1
            if status == "error":
                span.tool_errors += 1
        else:
            record["scope"] = "unscoped"
        with self._lock:
            self._tools.append(record)

    def clear(self) -> None:
        with self._lock:
            self._calls.clear()
            self._tools.clear()
            self._spans.clear()
            self._started_at = time.time()

    # --- reading -----------------------------------------------------------

    def snapshot(self, *, limit: int = 50, session_key: str | None = None) -> dict[str, Any]:
        """Everything the panel shows: aggregates first, then recent records."""
        with self._lock:
            calls = list(self._calls)
            tools = list(self._tools)
            spans = list(self._spans)
            # Built through the same `_record`, so a turn still running is a
            # row of the same shape and can sit in the same table. A span only
            # reaches the ring buffer when it ends, and the turn somebody is
            # asking about is usually the one that has not — a panel that shows
            # nothing until it finishes looks broken at exactly the wrong
            # moment. Its numbers are what it has spent so far.
            live = [{**s._record(), "running": True} for s in self._live.values()]
            since = self._started_at
        if session_key:
            calls = [c for c in calls if c.get("session_key") == session_key]
            tools = [t for t in tools if t.get("session_key") == session_key]
            spans = [s for s in spans if s.get("session_key") == session_key]
        if session_key:
            live = [s for s in live if s.get("session_key") == session_key]
        # Live spans count in the tables and the totals, and are marked so a
        # half-finished turn is never mistaken for a fast one.
        spans_all = live + spans
        return {
            "since": since,
            "now": time.time(),
            "enabled": self.enabled,
            "totals": _totals(spans_all, calls, tools),
            "by_kind": _group(spans_all, lambda s: s["kind"]),
            "by_model": _model_table(calls),
            "by_tool": _tool_table(tools),
            "by_chat": _chat_table(spans_all),
            "slowest_spans": sorted(spans_all, key=lambda s: -s["duration_ms"])[:limit],
            "slowest_calls": sorted(calls, key=lambda c: -c["duration_ms"])[:limit],
            "live": live,
            "recent_spans": (live + list(reversed(spans)))[:limit],
            "recent_calls": list(reversed(calls))[:limit],
            "recent_tools": list(reversed(tools))[:limit],
            "counts": {
                "spans": len(spans), "running": len(live),
                "calls": len(calls), "tools": len(tools),
            },
        }


def _pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile. No numpy in this process and no reason for one."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[idx], 1)


def _sum_tokens(records: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in records:
        for key, value in (r.get("tokens") or {}).items():
            out[key] = out.get(key, 0) + value
    return out


def _totals(spans: list[dict], calls: list[dict], tools: list[dict]) -> dict[str, Any]:
    durations = [s["duration_ms"] for s in spans]
    tokens = _sum_tokens(calls)
    prompt = tokens.get("prompt_tokens", 0)
    return {
        "spans": len(spans),
        "calls": len(calls),
        "tools": len(tools),
        "call_errors": sum(1 for c in calls if c.get("finish_reason") == "error"),
        "tool_errors": sum(1 for t in tools if t.get("status") == "error"),
        "retries": sum(max(0, c.get("attempts", 1) - 1) for c in calls),
        "retry_wait_ms": round(sum(c.get("retry_wait_ms", 0) for c in calls), 1),
        "fallbacks": sum(1 for c in calls if c.get("served_by") != c.get("model")),
        "empty_replies": sum(1 for c in calls if c.get("empty")),
        "truncated_replies": sum(1 for c in calls if c.get("truncated")),
        "tokens": tokens,
        "cache_hit_pct": (
            round(100.0 * tokens.get("cached_tokens", 0) / prompt, 1) if prompt else None
        ),
        "span_ms": {
            "p50": _pct(durations, 0.5),
            "p95": _pct(durations, 0.95),
            "max": round(max(durations), 1) if durations else 0.0,
        },
        # Where a turn's wall time goes, added up over everything held here.
        "time_split_ms": {
            "llm": round(sum(s.get("llm_ms", 0) for s in spans), 1),
            "retry_wait": round(sum(s.get("retry_wait_ms", 0) for s in spans), 1),
            "tools": round(sum(s.get("tools_wall_ms", 0) for s in spans), 1),
            "other": round(sum(s.get("other_ms", 0) for s in spans), 1),
        },
    }


def _group(spans: list[dict], key) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for span in spans:
        bucket = out.setdefault(key(span), {"count": 0, "ms": []})
        bucket["count"] += 1
        bucket["ms"].append(span["duration_ms"])
    return {
        name: {
            "count": b["count"],
            "p50_ms": _pct(b["ms"], 0.5),
            "p95_ms": _pct(b["ms"], 0.95),
            "total_ms": round(sum(b["ms"]), 1),
        }
        for name, b in sorted(out.items(), key=lambda kv: -sum(kv[1]["ms"]))
    }


def _model_table(calls: list[dict]) -> dict[str, dict[str, Any]]:
    """Per model that actually answered: how often, how slow, what it cost."""
    out: dict[str, dict[str, Any]] = {}
    for call in calls:
        bucket = out.setdefault(call.get("served_by") or "?", {
            "calls": 0, "errors": 0, "retries": 0, "ms": [], "tokens": {}, "gateway": "",
        })
        bucket["calls"] += 1
        bucket["gateway"] = call.get("gateway") or bucket["gateway"]
        bucket["ms"].append(call["duration_ms"])
        bucket["retries"] += max(0, call.get("attempts", 1) - 1)
        if call.get("finish_reason") == "error":
            bucket["errors"] += 1
        for key, value in (call.get("tokens") or {}).items():
            bucket["tokens"][key] = bucket["tokens"].get(key, 0) + value
    table = {}
    for name, b in sorted(out.items(), key=lambda kv: -kv[1]["calls"]):
        prompt = b["tokens"].get("prompt_tokens", 0)
        table[name] = {
            "calls": b["calls"],
            "errors": b["errors"],
            "retries": b["retries"],
            "gateway": b["gateway"],
            "p50_ms": _pct(b["ms"], 0.5),
            "p95_ms": _pct(b["ms"], 0.95),
            "tokens": b["tokens"],
            "cache_hit_pct": (
                round(100.0 * b["tokens"].get("cached_tokens", 0) / prompt, 1) if prompt else None
            ),
        }
    return table


def _tool_table(tools: list[dict]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for tool in tools:
        bucket = out.setdefault(tool["name"], {"calls": 0, "errors": 0, "ms": [], "chars": 0})
        bucket["calls"] += 1
        bucket["ms"].append(tool["duration_ms"])
        bucket["chars"] += tool.get("result_chars", 0)
        if tool.get("status") == "error":
            bucket["errors"] += 1
    return {
        name: {
            "calls": b["calls"],
            "errors": b["errors"],
            "p50_ms": _pct(b["ms"], 0.5),
            "p95_ms": _pct(b["ms"], 0.95),
            "total_ms": round(sum(b["ms"]), 1),
            "result_chars": b["chars"],
        }
        # Ordered by total time, which is the order you want to read it in:
        # a 200ms tool called forty times outranks a 3s one called once.
        for name, b in sorted(out.items(), key=lambda kv: -sum(kv[1]["ms"]))
    }


def _chat_table(spans: list[dict]) -> dict[str, dict[str, Any]]:
    """Per session: the shape of one conversation's cost over time."""
    out: dict[str, dict[str, Any]] = {}
    for span in spans:
        key = span.get("session_key") or "(none)"
        bucket = out.setdefault(key, {
            "spans": 0, "ms": [], "tokens": {}, "calls": 0, "tools": 0,
            "last_ts": 0.0, "models": {},
        })
        bucket["spans"] += 1
        bucket["ms"].append(span["duration_ms"])
        bucket["calls"] += span.get("calls", 0)
        bucket["tools"] += span.get("tools", 0)
        bucket["last_ts"] = max(bucket["last_ts"], span.get("ts", 0.0))
        for key_, value in (span.get("tokens") or {}).items():
            bucket["tokens"][key_] = bucket["tokens"].get(key_, 0) + value
        for model, count in (span.get("models_served") or {}).items():
            bucket["models"][model] = bucket["models"].get(model, 0) + count
    table = {}
    for name, b in sorted(out.items(), key=lambda kv: -kv[1]["tokens"].get("prompt_tokens", 0)):
        prompt = b["tokens"].get("prompt_tokens", 0)
        table[name] = {
            "spans": b["spans"],
            "calls": b["calls"],
            "tools": b["tools"],
            "p50_ms": _pct(b["ms"], 0.5),
            "p95_ms": _pct(b["ms"], 0.95),
            "tokens": b["tokens"],
            "cache_hit_pct": (
                round(100.0 * b["tokens"].get("cached_tokens", 0) / prompt, 1) if prompt else None
            ),
            # Prompt tokens per call is how a session's history growth shows
            # up: the same question costs more in a chat that never forgets.
            "prompt_per_call": round(prompt / b["calls"], 1) if b["calls"] else 0,
            "models": b["models"],
            "last_ts": b["last_ts"],
        }
    return table


PROFILER = Profiler()
