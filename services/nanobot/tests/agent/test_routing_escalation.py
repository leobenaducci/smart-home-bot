"""Routing: the classifier picks where a turn starts, escalation fixes what it got wrong.

Before 2026-09-21 a turn's model was the caller's choice alone. Now, in
`active` mode, a `complex` label starts the turn on the strong model, a cheap
attempt that ends badly is continued on it, and a session that escalated
starts strong for a few turns. In `shadow` mode all of that is recorded and
none of it happens; in `off` mode nothing is even asked.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import mock_provider

from nanobot.agent.loop import AgentLoop
from nanobot.agent.runner import AgentRunResult
from nanobot.agent.subagent import SubagentManager
from nanobot.config.schema import RoutingConfig
from nanobot.providers.base import LLMResponse
from nanobot.session.manager import Session


def _classifier_provider(label: str, calls: list | None = None):
    provider = mock_provider("tiny")

    async def chat(**kwargs):
        if calls is not None:
            calls.append(kwargs["messages"][0]["content"])
        return LLMResponse(content='{"label": "%s", "reason": "test"}' % label)

    provider.chat = chat
    return provider


def _loop(tmp_path: Path, *, label="action", mode="active", calls=None, **routing) -> AgentLoop:
    return AgentLoop(
        bus=MagicMock(),
        provider=mock_provider("fast-model"),
        workspace=tmp_path,
        model="fast-model",
        powerful_provider=mock_provider("pro-model"),
        powerful_model="pro-model",
        classifier_provider=_classifier_provider(label, calls),
        classifier_model="tiny",
        routing=RoutingConfig(mode=mode, **routing),
    )


def _capture_runs(loop: AgentLoop, monkeypatch, *, fast_result=None):
    """Record which runner ran and with what, answering `fast_result` on the fast one."""
    runs: list[tuple[str, object]] = []

    async def fast_run(spec):
        runs.append(("fast", spec))
        return fast_result or AgentRunResult(final_content="ok", messages=list(spec.initial_messages))

    async def pro_run(spec):
        runs.append(("pro", spec))
        return AgentRunResult(final_content="pro ok", messages=list(spec.initial_messages))

    monkeypatch.setattr(loop.runner, "run", fast_run)
    monkeypatch.setattr(loop._powerful_runner, "run", pro_run)
    return runs


def _reports(monkeypatch):
    seen = []
    monkeypatch.setattr("nanobot.agent.loop.report_usage",
                        lambda *a, **kw: seen.append((a, kw)))
    return seen


HOLA = [{"role": "user", "content": "hola"}]


# --- where a turn starts ------------------------------------------------------

@pytest.mark.asyncio
async def test_a_complex_label_starts_on_the_strong_model(tmp_path, monkeypatch):
    loop = _loop(tmp_path, label="complex")
    runs = _capture_runs(loop, monkeypatch)
    await loop._run_agent_loop(HOLA)
    assert [r[0] for r in runs] == ["pro"]


@pytest.mark.asyncio
async def test_an_action_label_stays_on_the_fast_model(tmp_path, monkeypatch):
    loop = _loop(tmp_path, label="action")
    runs = _capture_runs(loop, monkeypatch)
    await loop._run_agent_loop(HOLA)
    assert [r[0] for r in runs] == ["fast"]


@pytest.mark.asyncio
async def test_shadow_mode_records_the_label_and_runs_cheap(tmp_path, monkeypatch):
    loop = _loop(tmp_path, label="complex", mode="shadow")
    runs = _capture_runs(loop, monkeypatch)
    seen = _reports(monkeypatch)
    await loop._run_agent_loop(HOLA)
    assert [r[0] for r in runs] == ["fast"]
    route = seen[-1][1]["route"]
    assert route["label"] == "complex" and route["tier"] == "everyday"
    assert route["source"] == "shadow:model"


@pytest.mark.asyncio
async def test_off_mode_never_asks(tmp_path, monkeypatch):
    calls: list = []
    loop = _loop(tmp_path, label="complex", mode="off", calls=calls)
    runs = _capture_runs(loop, monkeypatch)
    await loop._run_agent_loop(HOLA)
    assert calls == [] and [r[0] for r in runs] == ["fast"]


@pytest.mark.asyncio
async def test_the_callers_flag_skips_the_classifier(tmp_path, monkeypatch):
    calls: list = []
    loop = _loop(tmp_path, label="chat", calls=calls)
    runs = _capture_runs(loop, monkeypatch)
    seen = _reports(monkeypatch)
    await loop._run_agent_loop(HOLA, powerful=True)
    assert calls == [] and [r[0] for r in runs] == ["pro"]
    assert seen[-1][1]["route"]["source"] == "forced"


@pytest.mark.asyncio
async def test_no_classifier_configured_means_the_old_ladder(tmp_path, monkeypatch):
    loop = AgentLoop(bus=MagicMock(), provider=mock_provider("fast-model"), workspace=tmp_path,
                     model="fast-model", powerful_provider=mock_provider("pro-model"),
                     powerful_model="pro-model", routing=RoutingConfig(mode="active"))
    runs = _capture_runs(loop, monkeypatch)
    await loop._run_agent_loop(HOLA)
    await loop._run_agent_loop(HOLA, powerful=True)
    assert [r[0] for r in runs] == ["fast", "pro"]


# --- escalation after the fact ------------------------------------------------

@pytest.mark.asyncio
async def test_a_bad_invocation_is_continued_on_the_strong_model(tmp_path, monkeypatch):
    loop = _loop(tmp_path, label="action")
    failed = AgentRunResult(
        final_content="[Escribí una invocación que no se pudo interpretar]",
        messages=HOLA + [{"role": "assistant", "content": "[Escribí una invocación...]"}],
        stop_reason="bad_invocation", usage={"prompt_tokens": 10},
    )
    runs = _capture_runs(loop, monkeypatch, fast_result=failed)
    seen = _reports(monkeypatch)
    session = Session(key="api:x")
    content, _, _, stop, _ = await loop._run_agent_loop(HOLA, session=session)
    assert [r[0] for r in runs] == ["fast", "pro"]
    assert content == "pro ok" and stop == "completed"
    # the strong model saw the conversation without the failed note
    pro_spec = runs[1][1]
    assert pro_spec.model == "pro-model"
    assert [m["role"] for m in pro_spec.initial_messages] == ["user"]
    # both attempts are on the bill, and both say why
    assert len(seen) == 2
    assert seen[0][1]["route"] == {"tier": "everyday", "label": "action", "source": "model",
                                   "classifier_ms": seen[0][1]["route"]["classifier_ms"],
                                   "escalated": True, "escalated_from": "bad_invocation"}
    assert seen[1][1]["route"]["source"] == "escalation"
    assert seen[1][1]["route"]["tier"] == "powerful"
    # and the session starts strong for a while
    assert session.metadata["sticky_powerful"] == 3


@pytest.mark.asyncio
async def test_escalation_keeps_tool_results_and_adds_the_note(tmp_path, monkeypatch):
    loop = _loop(tmp_path, label="action")
    msgs = HOLA + [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "added"},
        {"role": "assistant", "content": "[loop cut]"},
    ]
    failed = AgentRunResult(final_content="[loop cut]", messages=msgs,
                            stop_reason="repeated_tool_calls", tools_used=["skill:grocery"])
    runs = _capture_runs(loop, monkeypatch, fast_result=failed)
    await loop._run_agent_loop(HOLA)
    pro_msgs = runs[1][1].initial_messages
    assert pro_msgs[:3] == msgs[:3]
    assert pro_msgs[-1]["role"] == "user" and "repeated_tool_calls" in pro_msgs[-1]["content"]


@pytest.mark.asyncio
async def test_a_completed_turn_is_not_escalated(tmp_path, monkeypatch):
    loop = _loop(tmp_path, label="action")
    runs = _capture_runs(loop, monkeypatch)
    await loop._run_agent_loop(HOLA)
    assert [r[0] for r in runs] == ["fast"]


@pytest.mark.asyncio
async def test_only_the_configured_reasons_escalate(tmp_path, monkeypatch):
    loop = _loop(tmp_path, label="action", escalate_on=["error"])
    failed = AgentRunResult(final_content="x", messages=HOLA, stop_reason="bad_invocation")
    runs = _capture_runs(loop, monkeypatch, fast_result=failed)
    await loop._run_agent_loop(HOLA)
    assert [r[0] for r in runs] == ["fast"]


@pytest.mark.asyncio
async def test_shadow_mode_never_escalates(tmp_path, monkeypatch):
    loop = _loop(tmp_path, label="action", mode="shadow")
    failed = AgentRunResult(final_content="x", messages=HOLA, stop_reason="bad_invocation")
    runs = _capture_runs(loop, monkeypatch, fast_result=failed)
    await loop._run_agent_loop(HOLA)
    assert [r[0] for r in runs] == ["fast"]


@pytest.mark.asyncio
async def test_a_strong_turn_that_fails_is_not_escalated_again(tmp_path, monkeypatch):
    loop = _loop(tmp_path, label="complex")
    runs: list = []

    async def pro_run(spec):
        runs.append("pro")
        return AgentRunResult(final_content="x", messages=HOLA, stop_reason="bad_invocation")

    monkeypatch.setattr(loop._powerful_runner, "run", pro_run)
    await loop._run_agent_loop(HOLA)
    assert runs == ["pro"]


# --- sticky sessions ----------------------------------------------------------

@pytest.mark.asyncio
async def test_after_an_escalation_the_session_starts_strong_for_a_while(tmp_path, monkeypatch):
    calls: list = []
    loop = _loop(tmp_path, label="action", calls=calls, sticky_turns=2)
    session = Session(key="api:x")
    session.metadata["sticky_powerful"] = 2
    runs = _capture_runs(loop, monkeypatch)
    seen = _reports(monkeypatch)
    await loop._run_agent_loop(HOLA, session=session)
    await loop._run_agent_loop(HOLA, session=session)
    await loop._run_agent_loop(HOLA, session=session)
    assert [r[0] for r in runs] == ["pro", "pro", "fast"]
    assert [s[1]["route"]["source"] for s in seen] == ["sticky", "sticky", "model"]
    assert len(calls) == 1          # the classifier was only asked once the stickiness ran out
    assert session.metadata["sticky_powerful"] == 0


# --- sub-agents ---------------------------------------------------------------

async def _settle(m: SubagentManager) -> None:
    """`spawn` returns a message, not the task; the run is a scheduled task."""
    for t in list(m._running_tasks.values()):
        await t


def _manager(tmp_path, label, mode="active", powerful_model="pro-model"):
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    return SubagentManager(
        provider=mock_provider("fast-model"), workspace=tmp_path, bus=bus,
        max_tool_result_chars=1000, model="fast-model",
        powerful_model=powerful_model, powerful_provider=mock_provider("pro-model"),
        classifier=__import__("nanobot.agent.classify", fromlist=["TurnClassifier"]).TurnClassifier(
            _classifier_provider(label), "tiny"),
        routing=RoutingConfig(mode=mode),
    )


@pytest.mark.asyncio
async def test_an_unmarked_task_is_classified(tmp_path, monkeypatch):
    m = _manager(tmp_path, "complex")
    started = {}

    async def fake_run(task_id, task, label, origin, status, powerful, context):
        started.update(task_id=task_id, powerful=powerful)

    monkeypatch.setattr(m, "_run_subagent", fake_run)
    await m.spawn("compará las tres cámaras y decime cuál conviene")
    await _settle(m)                     # spawn schedules the run; let it happen
    assert started["powerful"] is True
    assert m._task_routes[started["task_id"]].label == "complex"


@pytest.mark.asyncio
async def test_the_spawners_word_wins(tmp_path, monkeypatch):
    m = _manager(tmp_path, "complex")
    started = {}

    async def fake_run(task_id, task, label, origin, status, powerful, context):
        started.update(powerful=powerful)

    monkeypatch.setattr(m, "_run_subagent", fake_run)
    await m.spawn("x", complex=False)
    await _settle(m)
    assert started["powerful"] is False


@pytest.mark.asyncio
async def test_shadow_mode_labels_a_task_but_runs_it_cheap(tmp_path, monkeypatch):
    m = _manager(tmp_path, "complex", mode="shadow")
    started = {}

    async def fake_run(task_id, task, label, origin, status, powerful, context):
        started.update(task_id=task_id, powerful=powerful)

    monkeypatch.setattr(m, "_run_subagent", fake_run)
    await m.spawn("x")
    await _settle(m)
    assert started["powerful"] is False
    assert m._task_routes[started["task_id"]].label == "complex"


def test_a_failed_cheap_task_escalates_only_in_active_mode(tmp_path):
    failed = AgentRunResult(final_content="x", messages=[], stop_reason="max_iterations")
    assert _manager(tmp_path, "action")._should_escalate(failed, powerful=False)
    assert not _manager(tmp_path, "action")._should_escalate(failed, powerful=True)
    assert not _manager(tmp_path, "action", mode="shadow")._should_escalate(failed, powerful=False)
    assert not _manager(tmp_path, "action", powerful_model=None)._should_escalate(failed, powerful=False)
    done = AgentRunResult(final_content="x", messages=[], stop_reason="completed")
    assert not _manager(tmp_path, "action")._should_escalate(done, powerful=False)


# --- the cheap tier's budget --------------------------------------------------

@pytest.mark.asyncio
async def test_a_classifier_routed_cheap_turn_gets_the_cheap_budget(tmp_path, monkeypatch):
    loop = _loop(tmp_path, label="action", everyday_iterations=40)
    loop.max_iterations = 200
    runs = _capture_runs(loop, monkeypatch)
    await loop._run_agent_loop(HOLA)
    assert runs[0][1].max_iterations == 40


@pytest.mark.asyncio
async def test_forced_and_strong_turns_keep_the_configured_budget(tmp_path, monkeypatch):
    loop = _loop(tmp_path, label="complex", everyday_iterations=40)
    loop.max_iterations = 200
    runs = _capture_runs(loop, monkeypatch)
    await loop._run_agent_loop(HOLA)                    # complex -> pro, full budget
    await loop._run_agent_loop(HOLA, powerful=True)     # forced -> pro, full budget
    assert [r[1].max_iterations for r in runs] == [200, 200]


@pytest.mark.asyncio
async def test_running_out_of_the_cheap_budget_escalates(tmp_path, monkeypatch):
    loop = _loop(tmp_path, label="action", everyday_iterations=40)
    spent = AgentRunResult(final_content="", messages=HOLA + [{"role": "assistant", "content": ""}],
                           stop_reason="max_iterations", tools_used=["exec"] * 40)
    runs = _capture_runs(loop, monkeypatch, fast_result=spent)
    await loop._run_agent_loop(HOLA)
    assert [r[0] for r in runs] == ["fast", "pro"]
    assert runs[1][1].max_iterations == max(loop.max_iterations, 80)
