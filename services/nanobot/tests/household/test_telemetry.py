"""household B5: profiler feed, usage report per turn and per sub-agent task."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from agent.conftest import make_provider

from nanobot.bus.queue import MessageBus
from nanobot.household import telemetry, usage_report
from nanobot.household.loop import HouseholdAgentLoop
from nanobot.household.profiling import PROFILER
from nanobot.llm_usage.models import LLMCallRecord
from nanobot.llm_usage.types import LLMUsage
from nanobot.providers.base import LLMProvider, LLMResponse


@pytest.fixture()
def posts(monkeypatch):
    out: list[dict] = []

    async def fake_post(payload):
        out.append(payload)

    monkeypatch.setattr(usage_report, "_post", fake_post)
    monkeypatch.setenv("HOMECORE_URL", "https://homecore.invalid")
    monkeypatch.setenv("HOMECORE_USER_ID", "user1")
    monkeypatch.setenv("HOMECORE_PROXY_TOKEN", "t")
    PROFILER.enabled = True
    PROFILER.clear()
    return out


def test_usage_dict_maps_upstream_usage():
    u = LLMUsage(input_tokens=100, output_tokens=20, total_tokens=120, cache_read_tokens=60, reported_tokens=120)
    assert telemetry.usage_dict(u) == {"prompt_tokens": 100, "completion_tokens": 20,
                                       "total_tokens": 120, "cached_tokens": 60}


def test_the_chained_observer_feeds_the_profiler_and_names_the_provider(posts):
    seen = []
    observer = telemetry.chain(seen.append)
    record = LLMCallRecord(started_at_ms=1, duration_ms=250, provider="together_ai", model="qwen",
                           source="user", stream=False, finish_reason="stop",
                           usage=LLMUsage(input_tokens=10, output_tokens=5, total_tokens=15, reported_tokens=15))
    with PROFILER.span("turn", "whatsapp:5491100000000") as span:
        observer(record)
    assert seen == [record] and span.outcome["provider"] == "together_ai"
    assert span.tokens["prompt_tokens"] == 10
    assert PROFILER.snapshot()["totals"]


def _record(finish_reason="stop"):
    return LLMCallRecord(started_at_ms=1, duration_ms=100, provider="opencode", model="m",
                         source="user", stream=False, finish_reason=finish_reason,
                         usage=LLMUsage(input_tokens=10, output_tokens=5, total_tokens=15,
                                        reported_tokens=15))


def test_an_answer_is_not_counted_empty(posts):
    # 2026-11-23, the D4 soak: every call counted as llm:empty, because the
    # upstream record carries no content and the profiler read that as none.
    with PROFILER.span("turn", "t1") as span:
        with telemetry.response_shape(LLMResponse(content="hola")):
            telemetry.observe_call(_record())
        with telemetry.response_shape(LLMResponse(content=None, tool_calls=[object()])):
            telemetry.observe_call(_record("tool_calls"))
    assert "llm:empty" not in span.events


def test_an_empty_reply_still_is(posts):
    with PROFILER.span("turn", "t2") as span:
        with telemetry.response_shape(LLMResponse(content="", reasoning_content="pienso...",
                                                  finish_reason="length")):
            telemetry.observe_call(_record("length"))
    assert "llm:empty" in span.events


def test_a_call_seen_outside_the_seam_is_unknown_not_empty(posts):
    with PROFILER.span("turn", "t3") as span:
        telemetry.observe_call(_record())
    assert "llm:empty" not in span.events


class _Provider(LLMProvider):
    async def chat(self, *args, **kwargs):  # pragma: no cover - never called
        raise NotImplementedError

    def get_default_model(self) -> str:
        return "m"


def test_the_provider_seam_hands_the_reply_sizes_to_the_observer(posts):
    provider = _Provider(provider_name="opencode")
    provider.set_llm_call_observer(telemetry.chain(None))
    with PROFILER.span("turn", "t4") as span:
        provider._record_llm_call(
            LLMResponse(content="una respuesta"), {"model": "m"},
            observer=provider._llm_call_observer, usage=None,
            started_at_ms=1, started_at_ns=0, finished_at_ns=1_000_000, stream=False)
        provider._record_llm_call(
            LLMResponse(content=""), {"model": "m"},
            observer=provider._llm_call_observer, usage=None,
            started_at_ms=1, started_at_ns=0, finished_at_ns=1_000_000, stream=False)
    assert span.events.get("llm:empty") == 1


@pytest.mark.asyncio
async def test_a_turn_is_reported_with_model_tools_route_and_stop_reason(tmp_path, posts):
    provider = make_provider(default_model="cheap")
    provider.provider_name = "custom"
    replies = iter([
        LLMResponse(content="", tool_calls=[], finish_reason="stop",
                    usage=LLMUsage(input_tokens=50, output_tokens=7, total_tokens=57, reported_tokens=57)),
    ])

    async def chat(**kw):
        return next(replies, LLMResponse(content="Done.", usage=LLMUsage(input_tokens=50, output_tokens=7, total_tokens=57, reported_tokens=57)))

    provider.chat_with_retry = provider.chat_stream_with_retry = provider.chat = chat
    loop = HouseholdAgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                              model="cheap", context_window_tokens=128_000)
    await loop.process_direct("hello", session_key="email:tomi@example.org",
                              channel="email", chat_id="tomi@example.org")
    import asyncio

    await asyncio.sleep(0)
    assert posts, "one usage row per turn"
    row = posts[-1]
    assert row["session_key"] == "email:tomi@example.org" and row["model"] == "cheap"
    assert row["provider"] == "custom" and row["stop_reason"] and "turn" in row


@pytest.mark.asyncio
async def test_a_sub_agent_task_is_billed_to_sub_chat(tmp_path, posts, monkeypatch):
    loop = HouseholdAgentLoop(bus=MessageBus(), provider=make_provider(default_model="m"),
                              workspace=tmp_path, model="m", context_window_tokens=1000)

    async def upstream(self, record):
        record.status.usage = LLMUsage(input_tokens=900, output_tokens=40, total_tokens=940, reported_tokens=940)
        return SimpleNamespace(stop_reason="completed", state="done")

    monkeypatch.setattr("nanobot.agent.subagent.SubagentManager._run_admitted_subagent", upstream)
    record = SimpleNamespace(announce=False, runtime=SimpleNamespace(model="worker"),
                             origin={"channel": "whatsapp", "chat_id": "5491100000000"},
                             status=SimpleNamespace(task_id="t1", label="report", usage=None))
    await loop.subagents._run_admitted_subagent(record)
    import asyncio

    await asyncio.sleep(0)
    row = posts[-1]
    assert row["session_key"] == "sub:5491100000000" and row["model"] == "worker"
    assert row["usage"]["prompt_tokens"] == 900 and row["route"]["tier"] == "subagent"


@pytest.mark.asyncio
async def test_each_tool_call_is_timed_into_the_profiler(posts):
    from nanobot.household.hooks import ToolTimingHook
    from nanobot.providers.base import ToolCallRequest

    hook = ToolTimingHook()
    ok = ToolCallRequest(id="c1", name="web_search", arguments={})
    bad = ToolCallRequest(id="c2", name="exec", arguments={})
    with PROFILER.span("turn", "whatsapp:5491100000000") as span:
        await hook.before_execute_tool(None, ok, None, {})
        await hook.before_execute_tool(None, bad, None, {})
        await hook.after_execute_tool(None, ok, None, {}, "3 results")
        await hook.on_execute_tool_error(None, bad, None, {}, "Error: exit 1\nnot found")
    tools = {t["name"]: t for t in PROFILER.snapshot()["recent_tools"]}
    assert tools["web_search"]["status"] == "ok" and tools["web_search"]["result_chars"] == 9
    assert tools["exec"]["status"] == "error" and "\n" not in tools["exec"]["detail"]
    assert span.tools == 2


@pytest.mark.asyncio
async def test_a_cancelled_sub_agent_still_bills_what_it_spent(tmp_path, posts, monkeypatch):
    import asyncio

    loop = HouseholdAgentLoop(bus=MessageBus(), provider=make_provider(default_model="m"),
                              workspace=tmp_path, model="m", context_window_tokens=1000)

    async def upstream(self, record):
        from nanobot.household.profiling import current_span

        current_span().tokens.update(prompt_tokens=700, completion_tokens=30)
        raise asyncio.CancelledError

    monkeypatch.setattr("nanobot.agent.subagent.SubagentManager._run_admitted_subagent", upstream)
    record = SimpleNamespace(announce=False, runtime=SimpleNamespace(model="worker"),
                             origin={"channel": "whatsapp", "chat_id": "5491100000000"},
                             status=SimpleNamespace(task_id="t1", label="report", usage=None))
    with pytest.raises(asyncio.CancelledError):
        await loop.subagents._run_admitted_subagent(record)
    await asyncio.sleep(0)
    row = posts[-1]
    assert row["session_key"] == "sub:5491100000000" and row["stop_reason"] == "cancelled"
    assert row["usage"]["prompt_tokens"] == 700
