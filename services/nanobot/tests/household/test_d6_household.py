"""household D6: describe_image on image turns, always-skill paths, remote skill sync start."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from agent.conftest import make_provider

from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Config
from nanobot.household.loop import HouseholdAgentLoop
from nanobot.household.runtime import RUNTIME
from nanobot.providers.base import LLMResponse


def _loop(tmp_path):
    provider = make_provider(default_model="cheap")
    provider.provider_name = "custom"
    seen: list[tuple[str, list]] = []

    async def chat(**kw):
        seen.append((kw.get("model"), kw["messages"]))
        return LLMResponse(content="A cat on the sofa.")

    provider.chat_with_retry = provider.chat_stream_with_retry = provider.chat = chat
    return HouseholdAgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                              model="cheap", context_window_tokens=128_000), seen


@pytest.mark.asyncio
async def test_an_image_turn_stays_on_the_main_model_with_describe_image(tmp_path):
    loop, seen = _loop(tmp_path)
    RUNTIME.config = Config.model_validate({"agents": {"defaults": {
        "model": "cheap", "provider": "custom", "visionModel": "eyes"}}})
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 64)
    await loop.process_direct("what is this?", session_key="whatsapp:5491100000000",
                              channel="whatsapp", chat_id="5491100000000", media=[str(photo)])
    model, messages = seen[-1]
    assert model == "cheap" and loop.tools.has("describe_image")
    last = messages[-1]["content"]
    text = last if isinstance(last, str) else " ".join(b.get("text", "") for b in last if isinstance(b, dict))
    assert str(photo) in text and "what is this?" in text
    assert not any(isinstance(b, dict) and b.get("type") == "image_url"
                   for m in messages for b in (m["content"] if isinstance(m["content"], list) else []))


def test_always_skills_keep_their_path_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")
    skill = tmp_path / "skills" / "chores"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: chores\ndescription: The house chores.\nalways: true\n---\n"
                                    "Call it with a JSON block.\n")
    loop, _ = _loop(tmp_path)
    prompt = loop.context.build_system_prompt()
    assert "# Active Skill Paths" in prompt
    paths = prompt.split("# Active Skill Paths", 1)[1]
    assert "chores" in paths and "SKILL.md" in paths


def test_binding_the_loop_starts_the_remote_skill_sync(tmp_path, monkeypatch):
    from nanobot.household import bind_household_loop

    started = MagicMock()
    monkeypatch.setattr("nanobot.household.remote_skills.RemoteSkillSync.ensure_started", started)
    loop = MagicMock(workspace=tmp_path)
    bind_household_loop(loop, Config())
    assert started.call_args.args[0] == tmp_path
