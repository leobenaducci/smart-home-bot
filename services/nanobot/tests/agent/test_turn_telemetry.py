"""What a turn reports about itself has to outlive the process.

The profiler's rings hold latency, failed calls and failed tools, and are gone
on the next restart; the runner's warnings -- DSML written as text, a skill
block that resolved to nothing, an empty answer retried -- went only to the
container log, which rolls over in days. The usage report is the one record
that is kept, so it now carries a turn's slice of both: joined by a turn id
across the rows one turn bills, as codes and counts and never as text.
"""
import asyncio

import pytest

from nanobot.agent import usage_report
from nanobot.utils.profiling import PROFILER, current_span, note_event


@pytest.fixture(autouse=True)
def _clean_profiler():
    PROFILER.clear()
    PROFILER.enabled = True
    yield
    PROFILER.clear()


def test_events_attach_to_the_turn_in_flight_and_nowhere_else():
    note_event("parse:dsml")  # no turn: dropped, not raised
    with PROFILER.span("turn", "homeweb:999000111:2026-09-29") as span:
        note_event("parse:dsml")
        note_event("parse:dsml")
        note_event("retry:empty")
        assert current_span() is span
    assert span.events == {"parse:dsml": 2, "retry:empty": 1}
    assert current_span() is None


def test_a_take_is_what_happened_since_the_last_one():
    with PROFILER.span("turn", "k") as span:
        PROFILER.record_call(model="cheap", served_by="cheap", api_base=None, duration_s=0.2,
                             request_s=0.2, attempts=1, finish_reason="error", usage=None,
                             stream=False)
        note_event("stop:repeated_tool_calls")
        first = span.take()
        PROFILER.record_tool(name="exec", duration_s=0.1, status="error", started_at=0.0)
        second = span.take()
    assert first["turn_id"] == second["turn_id"] == span.turn_id
    assert first["call_errors"] == 1 and first["tool_errors"] == 0
    assert first["events"] == {"stop:repeated_tool_calls": 1}
    # The escalated half bills its own failures, not the first attempt's again.
    assert second["call_errors"] == 0 and second["tool_errors"] == 1
    assert second["events"] == {"tool_error:exec": 1}
    assert first["latency_ms"] >= 0 and second["latency_ms"] >= 0


def test_a_fallback_and_an_empty_answer_are_events():
    with PROFILER.span("turn", "k") as span:
        PROFILER.record_call(model="asked", served_by="fallback", api_base=None,
                             duration_s=0.1, request_s=0.1, attempts=2,
                             finish_reason="length", usage=None, stream=False)
    assert span.events == {"llm:fallback": 1, "llm:empty": 1, "llm:truncated": 1}


def _capture(monkeypatch):
    sent = []

    async def post(payload):
        sent.append(payload)

    monkeypatch.setattr(usage_report, "_post", post)
    monkeypatch.setattr(usage_report, "_homeweb", lambda: ("https://hc", "999000111", "t"))
    return sent


async def _drain():
    await asyncio.gather(*list(usage_report._inflight))


@pytest.mark.asyncio
async def test_the_report_carries_the_stop_reason_and_the_turn(monkeypatch):
    sent = _capture(monkeypatch)
    with PROFILER.span("turn", "k") as span:
        note_event("parse:block_no_call")
    # After the span has closed, as the agent loop's final report is.
    usage_report.report_usage("k", "m", {"prompt_tokens": 10}, ["exec"],
                              stop_reason="empty_final_response", span=span)
    await _drain()
    (payload,) = sent
    assert payload["stop_reason"] == "empty_final_response"
    assert payload["turn"]["turn_id"] == span.turn_id
    assert payload["turn"]["events"] == {"parse:block_no_call": 1}


@pytest.mark.asyncio
async def test_a_plan_step_reports_its_own_time_and_leaves_the_turn_alone(monkeypatch):
    sent = _capture(monkeypatch)
    with PROFILER.span("turn", "k") as span:
        note_event("refused:read_only")
        usage_report.report_usage("k", "step-model", {"prompt_tokens": 5}, [],
                                  stop_reason="completed", latency_ms=1234)
        await _drain()
    assert sent[0]["turn"] == {"turn_id": span.turn_id, "latency_ms": 1234}
    # Still there for the turn's own report to carry.
    assert span.events == {"refused:read_only": 1}


@pytest.mark.asyncio
async def test_no_turn_means_no_turn_block(monkeypatch):
    sent = _capture(monkeypatch)
    usage_report.report_usage("ev-heartbeat", "m", {"prompt_tokens": 5}, 0)
    await _drain()
    assert "turn" not in sent[0] and sent[0]["stop_reason"] == ""


def test_unbilled_is_what_no_report_has_taken_yet():
    with PROFILER.span("turn", "k") as span:
        PROFILER.record_call(model="m", served_by="m", api_base=None, duration_s=0.1, request_s=0.1,
                             attempts=1, finish_reason="stop", stream=False,
                             usage={"prompt_tokens": 100, "completion_tokens": 10})
        PROFILER.record_tool(name="exec", duration_s=0.1, status="ok", started_at=0.0)
        span.take()   # an escalation's first report took these
        PROFILER.record_call(model="m", served_by="m", api_base=None, duration_s=0.1, request_s=0.1,
                             attempts=1, finish_reason="stop", stream=False,
                             usage={"prompt_tokens": 300, "completion_tokens": 30})
        PROFILER.record_tool(name="exec", duration_s=0.1, status="ok", started_at=0.0)
        PROFILER.record_tool(name="read_file", duration_s=0.1, status="ok", started_at=0.0)
        tokens, tools = span.unbilled()
    assert tokens == {"prompt_tokens": 300, "completion_tokens": 30}
    assert sorted(tools) == ["exec", "read_file"]


@pytest.mark.asyncio
async def test_a_cancelled_turn_bills_what_it_spent(monkeypatch):
    """The chat's time limit hands a silent turn to a background task and
    cancels it; Stop cancels it too. Either way the report at the end of the
    turn never ran, and the costliest turns of the day went unrecorded."""
    from nanobot.agent import loop as L
    sent = _capture(monkeypatch)

    async def turn():
        with PROFILER.span("turn", "k") as span, L._billed_if_cut(
                span, lambda: ("websocket:homeweb:999000111:2026-09-29:1", "cheap", {"tier": "everyday"})):
            PROFILER.record_call(model="cheap", served_by="cheap", api_base=None, duration_s=0.1,
                                 request_s=0.1, attempts=1, finish_reason="stop", stream=False,
                                 usage={"prompt_tokens": 5000, "completion_tokens": 40})
            PROFILER.record_tool(name="exec", duration_s=0.1, status="ok", started_at=0.0)
            await asyncio.sleep(10)

    task = asyncio.ensure_future(turn())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _drain()
    (payload,) = sent
    assert payload["stop_reason"] == "cancelled" and payload["model"] == "cheap"
    assert payload["usage"] == {"prompt_tokens": 5000, "completion_tokens": 40}
    assert payload["tool_names"] == ["exec"] and payload["route"]["tier"] == "everyday"


@pytest.mark.asyncio
async def test_a_cut_with_nothing_spent_reports_nothing(monkeypatch):
    from nanobot.agent import usage_report as U
    sent = _capture(monkeypatch)
    with PROFILER.span("task", "k") as span:
        pass
    U.report_cut("sub:x", "m", span)
    await _drain()
    assert sent == []


def test_a_skill_knows_which_conversation_it_runs_in():
    """A fix request continues the Programmer session of the conversation it
    came from, and only the runtime knows which that is: the turn's span."""
    from nanobot.agent.tools.shell import with_session_key
    with PROFILER.span("turn", "websocket:homeweb:999000111:2026-09-29:1790700000001"):
        env = with_session_key({"HOME": "/h"})
    assert env["NANOBOT_SESSION_KEY"] == "websocket:homeweb:999000111:2026-09-29:1790700000001"
    assert "NANOBOT_SESSION_KEY" not in with_session_key({"HOME": "/h"})   # no turn, no key
