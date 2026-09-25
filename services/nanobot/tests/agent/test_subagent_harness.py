"""Which background tasks run on the pi harness, and which never may."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.subagent import SubagentManager, SubagentStatus, _SubagentHook
from nanobot.config.schema import HarnessConfig
from nanobot.harness import pi_runner

ON = HarnessConfig(enabled=True, base_url="http://host.docker.internal:11434/v1", model="gemma4")


def manager(tmp_path, harness=ON):
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_inbound = AsyncMock()
    return SubagentManager(provider=MagicMock(), workspace=tmp_path, bus=bus,
                           max_tool_result_chars=1000, model="m", harness=harness)


@pytest.fixture()
def pi_installed(monkeypatch):
    monkeypatch.setattr(pi_runner, "available", lambda: True)
    ran = []

    async def fake_run(task, endpoint, workdir, **kw):
        ran.append((task, endpoint, kw))
        return pi_runner.HarnessResult(text="Listo: download:tomi/x.pdf", links=["download:tomi/x.pdf"],
                                       rounds=1, delivered=True)
    monkeypatch.setattr(pi_runner, "run", fake_run)
    return ran


def go(m, session_key):
    status = SubagentStatus(task_id="t1", label="l", task_description="t", started_at=0.0)
    origin = {"channel": "websocket", "chat_id": session_key.split(":", 1)[1], "session_key": session_key}
    asyncio.run(m._run_subagent("t1", "armame un PDF", "l", origin, status))
    return m


def announced(m) -> str:
    return m.bus.publish_inbound.call_args.args[0].metadata["subagent_raw_result"]


def test_the_members_own_task_runs_on_pi(tmp_path, pi_installed):
    m = go(manager(tmp_path), "websocket:homeweb:999000111:2026-09-23")
    assert len(pi_installed) == 1 and pi_installed[0][1].model == "gemma4"
    assert announced(m) == "Listo: download:tomi/x.pdf"


@pytest.mark.parametrize("session,chat", [
    ("websocket:homeweb:999000111:2026-09-23:ev-ask", None),
    ("whatsapp:5691234@s.whatsapp.net", None),
    # unified_session: the key says nothing, the channel's own address does.
    ("unified:default", ("whatsapp", "5691234@s.whatsapp.net")),
])
def test_another_persons_task_never_reaches_pi(tmp_path, pi_installed, session, chat, monkeypatch):
    """A question from another member or a WhatsApp sender is not the member
    asking; pi has no rule that limits what it may do on their behalf."""
    monkeypatch.setattr(SubagentManager, "_harness_endpoint", lambda self: pytest.fail("asked"))
    m = manager(tmp_path)
    m.runner.run = AsyncMock(side_effect=RuntimeError("the loop ran"))
    if chat:
        status = SubagentStatus(task_id="t1", label="l", task_description="t", started_at=0.0)
        origin = {"channel": chat[0], "chat_id": chat[1], "session_key": session}
        asyncio.run(m._run_subagent("t1", "armame un PDF", "l", origin, status))
    else:
        go(m, session)
    assert pi_installed == []
    assert "the loop ran" in announced(m)


def test_off_by_default(tmp_path, pi_installed):
    m = manager(tmp_path, harness=None)
    m.runner.run = AsyncMock(side_effect=RuntimeError("the loop ran"))
    go(m, "websocket:homeweb:999000111:2026-09-23")
    assert pi_installed == []


def test_an_opencode_endpoint_stays_on_the_loop(tmp_path, pi_installed):
    m = manager(tmp_path, HarnessConfig(enabled=True, base_url="https://opencode.ai/zen/go/v1", model="k"))
    assert m._harness_endpoint() is None


def test_no_answer_is_reported_as_a_failure_with_its_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(pi_runner, "available", lambda: True)

    async def empty(*a, **k):
        return pi_runner.HarnessResult(text="", links=[], rounds=4, error="stopped after 900s",
                                       tools=[pi_runner.ToolCall(name="web", args={})])
    monkeypatch.setattr(pi_runner, "run", empty)
    m = go(manager(tmp_path), "websocket:homeweb:999000111:2026-09-23")
    text = announced(m)
    assert "stopped after 900s" in text and "web" in text


def test_the_result_is_announced_exactly_once(tmp_path, pi_installed):
    m = go(manager(tmp_path), "websocket:homeweb:999000111:2026-09-23")
    assert m.bus.publish_inbound.await_count == 1


# --- the models pi runs: the ones picked for the sub-agent ----------------------

from nanobot.providers.openai_compat_provider import OpenAICompatProvider


def zen(model):
    return OpenAICompatProvider(api_key="sk", api_base="https://opencode.ai/zen/v1", default_model=model)


def with_models(tmp_path, harness, model="deepseek-v4-flash", powerful="deepseek-v4-pro"):
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_inbound = AsyncMock()
    return SubagentManager(provider=zen(model), workspace=tmp_path, bus=bus, max_tool_result_chars=1000,
                           model=model, powerful_model=powerful, powerful_provider=zen(powerful),
                           harness=harness, main_model="everyday-model", main_provider=zen("everyday-model"))


def test_pi_runs_the_sub_agent_model_and_the_powerful_one_for_a_hard_task(tmp_path, monkeypatch):
    monkeypatch.setattr(pi_runner, "available", lambda: True)
    m = with_models(tmp_path, HarnessConfig(enabled=True))
    ep = m._harness_endpoint(powerful=False)
    assert ep.model == "deepseek-v4-flash" and ep.base_url == "https://opencode.ai/zen/v1"
    assert m._harness_endpoint(powerful=True).model == "deepseek-v4-pro"


def test_a_go_model_reaches_pi_only_when_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr(pi_runner, "available", lambda: True)
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-oc")
    m = with_models(tmp_path, HarnessConfig(enabled=True), model="opencode-go/kimi-k2.7-code")
    assert m._harness_endpoint() is None
    m = with_models(tmp_path, HarnessConfig(enabled=True, allow_go=True), model="opencode-go/kimi-k2.7-code")
    ep = m._harness_endpoint()
    assert ep.base_url == "https://opencode.ai/zen/go/v1" and ep.model == "kimi-k2.7-code"
    assert ep.api_key == "sk-oc"


def test_nanobots_own_loop_never_calls_a_go_model(tmp_path, monkeypatch):
    """A task that cannot go to pi -- here the harness is off -- runs on the
    everyday model instead of the Go one it was given."""
    m = with_models(tmp_path, HarnessConfig(enabled=False), model="opencode-go/kimi-k2.7-code")
    seen = {}

    async def fake_run(self, spec):
        seen["model"] = spec.model
        raise RuntimeError("stop here")
    monkeypatch.setattr("nanobot.agent.runner.AgentRunner.run", fake_run)
    go(m, "websocket:homeweb:999000111:2026-09-23")
    assert seen["model"] == "everyday-model"


def test_the_panel_sees_a_call_when_it_starts_and_each_nudge(monkeypatch, tmp_path):
    monkeypatch.setattr(pi_runner, "available", lambda: True)

    async def fake_run(task, endpoint, workdir, **kw):
        call = pi_runner.ToolCall(name="web", args={"action": "fetch"})
        await kw["on_start"](call)
        await kw["on_tool"](call)
        await kw["on_nudge"](1, 3, "no file")
        return pi_runner.HarnessResult(text="Listo: download:tomi/x.pdf", links=["download:tomi/x.pdf"],
                                       rounds=2, delivered=True)
    monkeypatch.setattr(pi_runner, "run", fake_run)
    m = go(manager(tmp_path), "websocket:tomi")
    events = [(c.args[0].metadata["kind"], c.args[0].metadata.get("text"))
              for c in m.bus.publish_outbound.call_args_list
              if c.args[0].metadata.get("_subagent_event") == "progress"]
    assert events == [("tool", "web"), ("tool_result", "web"),
                      ("phase", "asked to continue (1/3): no file")]
