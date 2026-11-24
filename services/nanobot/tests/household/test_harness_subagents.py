"""household: sub-agent tasks on the pi harness (B2)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from agent.conftest import make_provider

from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Config
from nanobot.household import subagents as mod
from nanobot.household.loop import HouseholdAgentLoop
from nanobot.household.runtime import RUNTIME
from nanobot.utils.llm_runtime import LLMRuntime


def _loop(tmp_path):
    provider = make_provider(default_model="cheap")
    provider.provider_name = "custom"
    return HouseholdAgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                              model="cheap", context_window_tokens=128_000)


def _cfg(**harness):
    return Config.model_validate({"agents": {"defaults": {
        "model": "cheap", "provider": "custom",
        "harness": {"enabled": True, **harness},
    }}})


def test_no_endpoint_when_the_harness_is_off_or_pi_missing(monkeypatch):
    parent = LLMRuntime.capture(make_provider(), "cheap", context_window_tokens=1000)
    assert mod.harness_endpoint(parent, Config()) is None
    monkeypatch.setattr("nanobot.household.harness.pi_runner.available", lambda: False)
    assert mod.harness_endpoint(parent, _cfg()) is None


def test_explicit_endpoint_and_go_refused(monkeypatch):
    monkeypatch.setattr("nanobot.household.harness.pi_runner.available", lambda: True)
    ep = mod.harness_endpoint(None, _cfg(baseUrl="http://ollama:11434/v1", model="gemma4"))
    assert ep is not None and ep.model == "gemma4"
    assert mod.harness_endpoint(None, _cfg(baseUrl="https://opencode.ai/zen/go/v1", model="x")) is None


@pytest.mark.asyncio
async def test_own_task_runs_on_pi_and_a_third_party_one_does_not(tmp_path, monkeypatch):
    loop = _loop(tmp_path)
    RUNTIME.config = _cfg()
    (tmp_path / "USER.md").write_text("- **Name**: Tomi\n")
    monkeypatch.setattr(mod, "harness_endpoint", lambda runtime, config=None: SimpleNamespace(model="gemma4"))
    seen = {}

    async def fake_pi(task, endpoint, workdir, **kw):
        seen["context"] = kw.get("context")
        return SimpleNamespace(text="Done, the guide is at download:x.pdf", rounds=1, turns=2, seconds=3,
                               delivered=True, tools=[], error="", usage={"prompt_tokens": 12})

    monkeypatch.setattr("nanobot.household.harness.pi_runner.run", fake_pi)
    record = SimpleNamespace(announce=True, runtime=None, origin={"channel": "cli", "chat_id": "direct"},
                             status=SimpleNamespace(task_id="t1", label="guide", task_description="Make the guide", phase=""))
    monkeypatch.setattr(loop.subagents, "_save_status", lambda status: _noop())

    token = mod._HARNESS_OK.set(True)
    try:
        outcome = await loop.subagents._run_admitted_subagent(record)
    finally:
        mod._HARNESS_OK.reset(token)
    assert outcome.state == "done" and "download:x.pdf" in outcome.result
    assert "Tomi" in seen["context"]

    async def upstream(self, record):
        return SimpleNamespace(stop_reason="completed", state="done")

    monkeypatch.setattr("nanobot.agent.subagent.SubagentManager._run_admitted_subagent", upstream)
    seen.clear()
    outcome = await loop.subagents._run_admitted_subagent(record)  # _HARNESS_OK unset: third party/media
    assert seen == {} and outcome.stop_reason == "completed"


async def _noop():
    return None


@pytest.mark.asyncio
async def test_spawn_marks_harness_eligibility(tmp_path, monkeypatch):
    loop = _loop(tmp_path)
    seen = []

    async def fake_spawn(self, task, *args, runtime=None, **kwargs):
        seen.append(mod._HARNESS_OK.get())
        return "ok"

    monkeypatch.setattr("nanobot.agent.subagent.SubagentManager.spawn", fake_spawn)
    monkeypatch.setattr("nanobot.utils.homeweb_chat_id.is_third_party_session",
                        lambda key: bool(key) and key.startswith("whatsapp:"))
    await loop.subagents.spawn("task a", origin_channel="websocket", origin_chat_id="homeweb:999000111:1",
                               session_key="websocket:homeweb:999000111:1")
    await loop.subagents.spawn("task b", origin_channel="whatsapp", origin_chat_id="5490000",
                               session_key="whatsapp:5490000")
    bill = tmp_path / "bill.pdf"
    bill.write_bytes(b"%PDF")
    await loop.subagents.spawn("task c", media=[str(bill)], session_key="websocket:homeweb:999000111:1")
    assert seen == [True, False, False]
