"""Per-turn escalation to a stronger model.

The house runs a fast model because most questions are "¿qué tiempo hace?".
HomeCore marks the turns inside a Profesión (Finanzas, Programador, Profesor,
Diseñador) as worth the slower, better one. The flag is per turn and never
sticky — the same session holds a hard question and "gracias".
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from conftest import mock_provider

from nanobot.agent.loop import AgentLoop
from nanobot.agent.runner import AgentRunResult


def _loop(tmp_path: Path, *, powerful_provider=None, powerful_model=None) -> AgentLoop:
    provider = mock_provider("fast-model")
    return AgentLoop(
        bus=MagicMock(),
        provider=provider,
        workspace=tmp_path,
        model="fast-model",
        powerful_provider=powerful_provider,
        powerful_model=powerful_model,
    )


def test_unconfigured_powerful_falls_back_to_the_main_model(tmp_path):
    """Asking for a stronger model where none is configured answers on the
    normal one rather than failing — a deployment without the key still works."""
    loop = _loop(tmp_path)
    assert loop.powerful_model == "fast-model"
    assert loop._powerful_runner is loop.runner


def test_configured_powerful_gets_its_own_runner(tmp_path):
    alt = mock_provider("pro-model")
    loop = _loop(tmp_path, powerful_provider=alt, powerful_model="pro-model")
    assert loop.powerful_model == "pro-model"
    assert loop._powerful_runner is not loop.runner


@pytest.mark.asyncio
async def test_the_flag_selects_the_model_for_that_turn_only(tmp_path, monkeypatch):
    alt = mock_provider("pro-model")
    loop = _loop(tmp_path, powerful_provider=alt, powerful_model="pro-model")

    used: list[str] = []


    async def fake_run(spec):
        used.append(spec.model)
        return AgentRunResult(final_content="ok", messages=[])

    monkeypatch.setattr(loop.runner, "run", fake_run)
    monkeypatch.setattr(loop._powerful_runner, "run", fake_run)

    await loop._run_agent_loop([{"role": "user", "content": "hola"}], powerful=True)
    await loop._run_agent_loop([{"role": "user", "content": "hola"}])
    await loop._run_agent_loop([{"role": "user", "content": "hola"}], powerful=True)

    assert used == ["pro-model", "fast-model", "pro-model"]


@pytest.mark.asyncio
async def test_process_direct_forwards_the_flag(tmp_path):
    loop = _loop(tmp_path)
    seen = {}

    async def fake_process(msg, **kw):
        seen.update(kw)
        return None

    loop._connect_mcp = lambda: _noop()
    loop._process_message = fake_process

    async def _noop():
        return None

    await loop.process_direct(content="hola", powerful=True)
    assert seen["powerful"] is True

    seen.clear()
    await loop.process_direct(content="hola")
    assert seen["powerful"] is False


# --- One model per Profesión -------------------------------------------------
# The Diseñador and the Programador want different models, and the roster lives
# in nanobot's config so a client names the *role* and never the model.

def _loop_with_profiles(tmp_path):
    provider = mock_provider("fast-model")
    dsg, dev = mock_provider("design-model"), mock_provider("code-model")
    return AgentLoop(
        bus=MagicMock(), provider=provider, workspace=tmp_path, model="fast-model",
        powerful_model="pro-model", powerful_provider=mock_provider("pro-model"),
        model_profiles={"designer": (dsg, "design-model"),
                        "programmer": (dev, "code-model")},
    )


@pytest.mark.asyncio
async def test_each_role_gets_its_own_model(tmp_path, monkeypatch):
    loop = _loop_with_profiles(tmp_path)
    used = []


    async def fake_run(spec):
        used.append(spec.model)
        return AgentRunResult(final_content="ok", messages=[])

    for runner, _ in list(loop._profiles.values()) + [
            (loop.runner, None), (loop._powerful_runner, None)]:
        monkeypatch.setattr(runner, "run", fake_run)

    msgs = [{"role": "user", "content": "hola"}]
    await loop._run_agent_loop(msgs, profile="designer", powerful=True)
    await loop._run_agent_loop(msgs, profile="programmer", powerful=True)
    # A role nobody configured falls back rather than erroring.
    await loop._run_agent_loop(msgs, profile="teacher", powerful=True)
    await loop._run_agent_loop(msgs)

    assert used == ["design-model", "code-model", "pro-model", "fast-model"]


def test_a_deployment_with_no_profiles_is_unaffected(tmp_path):
    provider = mock_provider("fast-model")
    loop = AgentLoop(bus=MagicMock(), provider=provider, workspace=tmp_path,
                     model="fast-model")
    assert loop._profiles == {}
