"""household: sub-agent model, dedup, media, gone sessions, `spawn` (B1)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent.conftest import make_provider

from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Config
from nanobot.household.loop import HouseholdAgentLoop
from nanobot.household.runtime import RUNTIME
from nanobot.household.subagents import HouseholdSubagentManager, subagent_runtime
from nanobot.utils.llm_runtime import LLMRuntime


def _loop(tmp_path):
    provider = make_provider(default_model="cheap")
    provider.provider_name = "custom"
    return HouseholdAgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                              model="cheap", context_window_tokens=128_000)


def _cfg():
    return Config.model_validate({"agents": {"defaults": {
        "model": "cheap", "provider": "custom",
        "subagentModel": "worker", "subagentModelPowerful": "thinker",
    }}})


def test_household_loop_installs_manager_and_spawn(tmp_path):
    loop = _loop(tmp_path)
    assert isinstance(loop.subagents, HouseholdSubagentManager)
    assert loop.tools.has("spawn") and loop.tools.has("subagent")


def test_sub_agent_runs_on_its_own_model():
    parent = LLMRuntime.capture(make_provider(), "cheap", context_window_tokens=1000)
    cfg = _cfg()
    assert subagent_runtime(parent, config=cfg).model == "worker"
    assert subagent_runtime(parent, complex=True, config=cfg).model == "thinker"
    assert subagent_runtime(parent, config=Config()).model == "cheap"


@pytest.mark.asyncio
async def test_a_running_duplicate_is_not_started_twice(tmp_path):
    loop = _loop(tmp_path)
    manager = loop.subagents
    status = SimpleNamespace(finished_at=None, owner="websocket:homeweb:999000111:2026-11-17:1",
                             task_description="Prepare the watering report")
    manager._tasks["t1"] = SimpleNamespace(status=status)
    out = await manager.spawn("prepare the   WATERING report", runtime=None,
                              session_key="websocket:homeweb:999000111:2026-11-17:1")
    assert "t1" in out


@pytest.mark.asyncio
async def test_media_is_handed_over_and_the_model_swapped(tmp_path, monkeypatch):
    loop = _loop(tmp_path)
    RUNTIME.config = _cfg()
    captured = {}

    async def fake_spawn(self, task, *args, runtime=None, **kwargs):
        captured.update(task=task, model=runtime.model)
        return "ok"

    monkeypatch.setattr("nanobot.agent.subagent.SubagentManager.spawn", fake_spawn)
    parent = LLMRuntime.capture(loop.provider, "cheap", context_window_tokens=1000)
    bill = tmp_path / "bill.pdf"
    bill.write_bytes(b"%PDF")
    await loop.subagents.spawn("Check the bill", runtime=parent,
                               media=[str(bill), str(tmp_path / "gone.pdf")], session_key="k")
    assert str(bill) in captured["task"] and "gone.pdf" not in captured["task"]
    assert captured["model"] == "worker"


@pytest.mark.asyncio
async def test_a_result_for_a_gone_session_is_delivered_not_dropped(tmp_path):
    loop = _loop(tmp_path)
    manager = loop.subagents
    manager.sessions = SimpleNamespace(exists=lambda key: False)
    manager.bus.publish_outbound = AsyncMock()
    await manager._announce_result("t9", "watering", "task", "The report is ready",
                                   {"channel": "websocket", "chat_id": "homeweb:999000111:2026-07-01:5",
                                    "session_key": "websocket:homeweb:999000111:2026-07-01:5"}, "ok")
    out = manager.bus.publish_outbound.await_args.args[0]
    assert out.content == "The report is ready" and out.chat_id.endswith(":5")


def _active_cfg():
    return Config.model_validate({"agents": {"defaults": {
        "model": "cheap", "provider": "custom", "modelPowerful": "big",
        "subagentModel": "worker", "subagentModelPowerful": "thinker",
        "classifierModel": "tiny", "routing": {"mode": "active"},
    }}})


def test_escalation_rule():
    from nanobot.household.subagents import should_escalate

    cfg = _active_cfg()
    assert should_escalate("max_iterations", "worker", config=cfg)
    assert not should_escalate("max_iterations", "thinker", config=cfg)  # already strong
    assert not should_escalate("completed", "worker", config=cfg)
    assert not should_escalate("max_iterations", "worker", config=_cfg())  # routing off


@pytest.mark.asyncio
async def test_a_stalled_cheap_task_continues_on_the_strong_model(tmp_path, monkeypatch):
    loop = _loop(tmp_path)
    RUNTIME.config = _active_cfg()
    runs = []

    async def fake_run(self, record):
        runs.append(record.runtime.model)
        return SimpleNamespace(stop_reason="max_iterations" if len(runs) == 1 else "completed")

    monkeypatch.setattr("nanobot.agent.subagent.SubagentManager._run_admitted_subagent", fake_run)
    session = SimpleNamespace(messages=[
        {"role": "user", "content": "Turn on Oficina Tomi"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ])
    record = SimpleNamespace(runtime=LLMRuntime.capture(loop.provider, "worker", context_window_tokens=1000),
                             session=session, status=SimpleNamespace(task_id="t1", label="lights"),
                             origin={"channel": "whatsapp", "chat_id": "5491100000000"})
    outcome = await loop.subagents._run_admitted_subagent(record)
    assert runs == ["worker", "thinker"] and outcome.stop_reason == "completed"
    assert session.messages[-1]["role"] == "user" and "max_iterations" in session.messages[-1]["content"]
    assert len([m for m in session.messages if m["role"] == "tool"]) == 1  # not re-run


@pytest.mark.asyncio
async def test_a_task_the_classifier_calls_hard_gets_the_strong_model(tmp_path, monkeypatch):
    from nanobot.household import subagents as mod

    loop = _loop(tmp_path)
    RUNTIME.config = _active_cfg()
    classifier = SimpleNamespace(classify=AsyncMock(return_value=SimpleNamespace(tier="powerful", source="model")))
    monkeypatch.setattr("nanobot.household.turn_router._classifier", lambda _loop, _cfg: classifier)
    assert await mod.classify_complex(loop, "Plan the whole garden for spring")
    captured = {}

    async def fake_spawn(self, task, *args, runtime=None, **kwargs):
        captured["model"] = runtime.model
        return "ok"

    monkeypatch.setattr("nanobot.agent.subagent.SubagentManager.spawn", fake_spawn)
    parent = LLMRuntime.capture(loop.provider, "cheap", context_window_tokens=1000)
    await loop.subagents.spawn("Plan the whole garden for spring", runtime=parent, session_key="k")
    assert captured["model"] == "thinker"


def test_a_strong_task_gets_complex_iterations_and_a_cheap_one_the_everyday_budget():
    from nanobot.household.subagents import iteration_budget

    cfg = Config.model_validate({"agents": {"defaults": {
        "model": "cheap", "provider": "custom", "subagentModel": "worker",
        "subagentModelPowerful": "thinker",
        "routing": {"complexIterations": 150, "everydayIterations": 30},
    }}})
    assert iteration_budget("thinker", config=cfg) == 150
    assert iteration_budget("worker", config=cfg) == 30


@pytest.mark.asyncio
async def test_the_budget_reaches_the_run_spec_even_above_the_loops(tmp_path):
    from nanobot.agent.runner import CURRENT_RUN_SPEC
    from nanobot.household.plan import TurnBudgetHook

    loop = _loop(tmp_path)
    RUNTIME.config = Config.model_validate({"agents": {"defaults": {
        "model": "cheap", "provider": "custom", "subagentModelPowerful": "thinker",
        "routing": {"complexIterations": 500}}}})
    record = SimpleNamespace(runtime=SimpleNamespace(model="thinker"), origin={"chat_id": "c"},
                             status=SimpleNamespace(task_id="t1", label="research"))
    hooks = [h for h in loop.subagents._extra_turn_hooks(record) if isinstance(h, TurnBudgetHook)]
    spec = SimpleNamespace(max_iterations=200)
    token = CURRENT_RUN_SPEC.set(spec)
    try:
        await hooks[0].before_run(None)
    finally:
        CURRENT_RUN_SPEC.reset(token)
    assert spec.max_iterations == 500


@pytest.mark.asyncio
async def test_a_task_past_its_time_limit_stops_with_what_it_managed(tmp_path, monkeypatch):
    import asyncio

    loop = _loop(tmp_path)
    RUNTIME.config = _cfg()
    monkeypatch.setenv("NANOBOT_SUBAGENT_MAX_RUNTIME_S", "0.05")
    stopped = asyncio.Event()

    async def slow_run(self, record):
        record.status.tool_events = [{"name": "web_search", "status": "ok", "detail": "3 results"},
                                     {"name": "web_fetch", "status": "error", "detail": "403"}]
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            stopped.set()
            raise

    monkeypatch.setattr("nanobot.agent.subagent.SubagentManager._run_admitted_subagent", slow_run)
    status = SimpleNamespace(task_id="t1", label="flights", tool_events=[], result="interim")
    record = SimpleNamespace(runtime=LLMRuntime.capture(loop.provider, "worker", context_window_tokens=1000),
                             session=SimpleNamespace(messages=[]), status=status, announce=False,
                             origin={"channel": "whatsapp", "chat_id": "5491100000000"})
    outcome = await loop.subagents._run_admitted_subagent(record)
    assert stopped.is_set()
    assert outcome.state == "incomplete" and outcome.stop_reason == "timeout"
    assert "web_search: 3 results" in outcome.result and "web_fetch" not in outcome.result
    assert status.result == outcome.result      # upstream's finish() reports this one


def test_a_sub_agent_can_describe_an_attached_picture(tmp_path):
    loop = _loop(tmp_path)
    RUNTIME.config = Config.model_validate({"agents": {"defaults": {
        "model": "cheap", "provider": "custom", "visionModel": "qwen3-vl:8b"}}})
    assert loop.subagents._build_tools().has("describe_image")
    RUNTIME.config = _cfg()                                  # no vision model
    assert not _loop(tmp_path / "b").subagents._build_tools().has("describe_image")


def _result_turn(task_id="t7"):
    from nanobot.bus.events import InboundMessage

    msg = InboundMessage(channel="system", sender_id="subagent", chat_id="whatsapp:5491100000000",
                         content="[Subagent 'flights' completed]",
                         metadata={"injected_event": "subagent_result", "subagent_task_id": task_id})
    return SimpleNamespace(msg=msg, session_key="whatsapp:5491100000000", transcript_input=None,
                           all_messages=[], final_content=None, stop_reason="", streamed_content=False)


@pytest.mark.asyncio
async def test_a_stalled_summary_delivers_the_raw_result(tmp_path, monkeypatch):
    import asyncio

    from nanobot.household import loop as household_loop

    loop = _loop(tmp_path)
    loop.subagents._raw_results = {"t7": "Cheapest flight: 12 March, 340 USD"}
    monkeypatch.setattr(household_loop, "SUMMARY_TIMEOUT_S", 0.05)

    async def stalled(self, ctx):
        await asyncio.sleep(30)

    monkeypatch.setattr(household_loop.HouseholdAgentLoop, "_household_run_turn", stalled)
    ctx = _result_turn()
    await loop._summarise_capped(ctx)
    assert ctx.final_content == "Cheapest flight: 12 March, 340 USD"
    assert ctx.all_messages[-1] == {"role": "assistant", "content": ctx.final_content}
    assert ctx.stop_reason == "summary_timeout" and loop.subagents._raw_results == {}


@pytest.mark.asyncio
async def test_a_summary_in_time_is_left_alone(tmp_path, monkeypatch):
    from nanobot.household import loop as household_loop

    loop = _loop(tmp_path)
    loop.subagents._raw_results = {"t7": "raw"}

    async def quick(self, ctx):
        ctx.final_content = "Found it: 12 March is cheapest."

    monkeypatch.setattr(household_loop.HouseholdAgentLoop, "_household_run_turn", quick)
    ctx = _result_turn()
    await loop._summarise_capped(ctx)
    assert ctx.final_content == "Found it: 12 March is cheapest."
    assert household_loop._is_subagent_result(ctx)


@pytest.mark.asyncio
async def test_the_raw_result_is_kept_for_the_summary_turn(tmp_path):
    loop = _loop(tmp_path)
    manager = loop.subagents
    manager.sessions = None
    manager.bus.publish_inbound = AsyncMock()
    await manager._announce_result("t8", "flights", "find flights", "340 USD",
                                   {"channel": "whatsapp", "chat_id": "5491100000000"}, "ok")
    assert manager.take_raw_result("t8") == "340 USD" and manager.take_raw_result("t8") is None


@pytest.mark.asyncio
async def test_a_sub_agent_runs_a_tool_call_its_model_wrote_as_text(tmp_path):
    """The fork's sub-agents ran the chat turn's runner: a call written as text
    (or a skill block) was executed. Upstream's sub-agent runner ignored it and
    returned the text as the result."""
    from unittest.mock import MagicMock

    from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse

    (tmp_path / "notes.txt").write_text("Water the roses on Tuesday.")
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "worker"
    provider.generation = GenerationSettings()
    seen: list[list[dict]] = []
    replies = iter([LLMResponse(content='read_file(path="notes.txt")'),
                    LLMResponse(content="The roses get water on Tuesday.")])

    async def chat(**kw):
        seen.append(kw["messages"])
        return next(replies)

    provider.chat_stream_with_retry = chat
    provider.chat_with_retry = chat
    loop = _loop(tmp_path)
    RUNTIME.config = _cfg()
    result = await loop.subagents.run_inline(
        task="When do the roses get water?", session_key="websocket:homeweb:999000111:2026-11-22:1",
        runtime=LLMRuntime.capture(provider, "worker", context_window_tokens=128_000))
    assert result == "The roses get water on Tuesday."
    assert any("Water the roses on Tuesday." in str(m.get("content")) for m in seen[-1])
