"""The running commentary a subagent publishes while it works.

HomeCore's "En segundo plano" panel draws a live timeline from these events. The
task itself must never depend on them: a publish that fails, or a cap that is
hit, costs a line in a panel and nothing else.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.hook import AgentHookContext
from nanobot.agent.subagent import PROGRESS_MAX_CHARS, _SubagentHook, _clip


def _call(name, arguments, call_id="c1"):
    return SimpleNamespace(id=call_id, name=name, arguments=arguments)


def _ctx(**kw):
    ctx = AgentHookContext(iteration=kw.pop("iteration", 1), messages=[])
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


@pytest.mark.asyncio
async def test_publishes_thought_then_each_tool_call():
    seen = []

    async def emit(payload):
        seen.append(payload)

    hook = _SubagentHook("t1", None, emit=emit)
    await hook.before_execute_tools(_ctx(
        response=SimpleNamespace(content="Voy a mirar tres sitios."),
        tool_calls=[_call("web_search", {"query": "arriendos"}),
                    _call("web_fetch", {"url": "https://x"}, "c2")],
    ))

    assert [e["kind"] for e in seen] == ["thought", "tool", "tool"]
    assert seen[0]["text"] == "Voy a mirar tres sitios."
    assert seen[1]["text"] == "web_search"
    assert json.loads(seen[1]["detail"]) == {"query": "arriendos"}
    # seq is monotonic so a consumer can order what arrives out of order
    assert [e["seq"] for e in seen] == [1, 2, 3]


@pytest.mark.asyncio
async def test_strips_think_wrapper_and_skips_empty_thoughts():
    seen = []

    async def emit(payload):
        seen.append(payload)

    hook = _SubagentHook("t1", None, emit=emit)
    await hook.before_execute_tools(_ctx(
        response=SimpleNamespace(content="<think>ruido interno</think>  "),
        tool_calls=[],
    ))
    assert seen == []

    await hook.before_execute_tools(_ctx(
        response=SimpleNamespace(content="<think>ruido</think> Reviso el primero."),
        tool_calls=[],
    ))
    assert [e["text"] for e in seen] == ["Reviso el primero."]


@pytest.mark.asyncio
async def test_tool_outcomes_carry_status_and_only_errors_carry_detail():
    seen = []

    async def emit(payload):
        seen.append(payload)

    hook = _SubagentHook("t1", None, emit=emit)
    await hook.after_iteration(_ctx(
        tool_calls=[_call("web_search", {}), _call("web_fetch", {}, "c2")],
        tool_events=[{"name": "web_search", "status": "ok"},
                     {"name": "web_fetch", "status": "error", "detail": "timeout"}],
        usage={},
    ))

    assert [(e["kind"], e["text"], e["status"]) for e in seen] == [
        ("tool_result", "web_search", "ok"),
        ("tool_result", "web_fetch", "error"),
    ]
    # A successful call's payload is not commentary — only failures explain
    # themselves, and a whole tool result would swamp the panel.
    assert seen[0]["detail"] == ""
    assert seen[1]["detail"] == "timeout"


@pytest.mark.asyncio
async def test_status_is_still_updated_without_an_emit_callback():
    """Narration is additive: the pre-existing status bookkeeping must survive
    a subagent nobody is listening to."""
    status = MagicMock(iteration=0, tool_events=[], usage={}, error=None)
    hook = _SubagentHook("t1", status, emit=None)
    await hook.before_execute_tools(_ctx(
        response=SimpleNamespace(content="algo"), tool_calls=[_call("exec", {})]))
    await hook.after_iteration(_ctx(
        iteration=7, tool_calls=[], tool_events=[{"name": "exec", "status": "ok"}],
        usage={"prompt_tokens": 5}))
    assert status.iteration == 7
    assert status.usage == {"prompt_tokens": 5}


@pytest.mark.asyncio
async def test_a_failing_publish_never_reaches_the_task():
    """A task with hours of work behind it must not die because the panel is
    unreachable."""
    async def emit(_payload):
        raise RuntimeError("portal caído")

    hook = _SubagentHook("t1", None, emit=emit)
    await hook.before_execute_tools(_ctx(
        response=SimpleNamespace(content="sigo"), tool_calls=[_call("exec", {})]))
    await hook.after_iteration(_ctx(
        tool_calls=[_call("exec", {})],
        tool_events=[{"name": "exec", "status": "ok"}], usage={}))


@pytest.mark.asyncio
async def test_event_cap_stops_narrating_not_working(monkeypatch):
    monkeypatch.setattr("nanobot.agent.subagent.PROGRESS_MAX_EVENTS", 3)
    seen = []

    async def emit(payload):
        seen.append(payload)

    hook = _SubagentHook("t1", None, emit=emit)
    for _ in range(10):
        await hook.before_execute_tools(_ctx(
            response=SimpleNamespace(content="paso"), tool_calls=[]))
    assert len(seen) == 3


def test_clip_collapses_whitespace_and_truncates():
    assert _clip("  hola   mundo \n ") == "hola mundo"
    long = _clip("x" * (PROGRESS_MAX_CHARS + 500))
    assert len(long) == PROGRESS_MAX_CHARS
    assert long.endswith("…")
