"""household: our config.json validates and no key is silently dropped.

v0.4.1's ``Config`` forbids unknown top-level keys (our ``n8n`` block made the
whole file fail) and its nested models ignore unknown keys (every per-role
setting vanished without a word). The fixture is the base config.json plus
every key the deployer renders into per-member overlays, with invented values.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.channels.email.runtime import EmailConfig
from nanobot.config.household import map_model_fallback
from nanobot.config.schema import Config, InlineFallbackConfig

FIXTURE = Path(__file__).parent / "fixtures" / "household_config.json"


def _flat(obj: object, prefix: str = "") -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            out.add(path)
            out |= _flat(value, path)
    return out


def _norm(path: str) -> str:
    return path.replace("_", "").lower()


@pytest.fixture()
def raw() -> dict:
    return json.loads(FIXTURE.read_text())


def test_household_config_validates_and_keeps_every_key(raw: dict) -> None:
    cfg = Config.model_validate(raw)
    dumped = cfg.model_dump(by_alias=True, mode="json")
    have = {_norm(p) for p in _flat(dumped)}
    # Channel blocks are parsed by each channel; checked separately below.
    # Provider extras are custom providers; their contents are checked below.
    missing = sorted(
        p for p in _flat(raw)
        if _norm(p) not in have and not p.startswith("channels.email.")
    )
    assert missing == []


def test_household_agent_fields_reach_the_model(raw: dict) -> None:
    d = Config.model_validate(raw).agents.defaults
    assert d.model_powerful == "deepseek-v4-pro"
    assert d.subagent_model == "deepseek-v4-pro"
    assert d.subagent_provider_powerful == "custom"
    assert d.model_profiles["programmer"] == "kimi-k3"
    assert d.provider_profiles["doctor"] == "custom"
    assert d.reasoning_effort_profiles == {"notifications": "none"}
    assert d.vision_model == "qwen3-vl:8b" and d.vision_provider == "ollama"
    assert d.heartbeat_model and d.heartbeat_reasoning_effort == "none"
    assert d.classifier_model == "gemma4:e4b"
    assert d.plan_step_model == "qwen3.5:9b"
    assert d.routing.mode == "active" and d.routing.plan_executor == "everyday"
    assert d.routing.sticky_turns == 2
    assert d.harness.enabled is True and d.harness.context_window == 65536
    assert d.context_block_limit == 12000
    assert d.dream.write_skills is False


def test_model_fallback_maps_onto_inline_fallback_models(raw: dict) -> None:
    d = Config.model_validate(raw).agents.defaults
    assert d.model_fallback == "deepseek-v4-flash"
    assert len(d.fallback_models) == 1
    entry = d.fallback_models[0]
    assert isinstance(entry, InlineFallbackConfig)
    assert entry.model == "deepseek-v4-flash"
    assert entry.provider == "custom"  # the provider serving the role


def test_model_fallback_list_with_provider_entries() -> None:
    data = map_model_fallback({
        "provider": "custom",
        "modelFallback": ["a", {"model": "b", "provider": "together_ai"}, {"model": "c"}, ""],
    })
    assert data["fallbackModels"] == [
        {"model": "a", "provider": "custom"},
        {"model": "b", "provider": "together_ai"},
        {"model": "c", "provider": "custom"},
    ]


def test_explicit_fallback_models_win() -> None:
    data = {"modelFallback": "a", "fallbackModels": []}
    assert map_model_fallback(data) is data


def test_top_level_household_blocks(raw: dict) -> None:
    cfg = Config.model_validate(raw)
    assert cfg.n8n.base_url == "http://n8n.example:5678"
    assert cfg.n8n.workflows_endpoint == "/api/v1/workflows/"
    assert cfg.profiling.enabled is True
    assert cfg.tools.disabled == ["glob", "notebook_edit"]
    assert cfg.gateway.heartbeat.channel == "websocket"
    assert cfg.gateway.heartbeat.chat_id.startswith("homeweb:999000111:")
    assert cfg.gateway.morning_greeting.enabled is True
    assert cfg.channels.transcription_language == "es"


def test_household_provider_blocks_become_custom_providers(raw: dict) -> None:
    cfg = Config.model_validate(raw)
    extras = cfg.providers.model_extra or {}
    for name in ("ollama_cloud", "together_ai", "ollama_vision", "nanogpt", "openai_compatible"):
        assert name in extras, name
        assert extras[name].api_base


def test_email_rules_survive(raw: dict) -> None:
    email = EmailConfig.model_validate(raw["channels"]["email"])
    assert email.rules == [{"from": "tomi@example.org", "allow": ["reply"]}]
    assert email.rules_enforced is True
    assert email.alfred_addresses == ["alfred@example.org"]


def test_mcp_disabled_tools_survive(raw: dict) -> None:
    cfg = Config.model_validate(raw)
    assert "todo_get_items" in cfg.tools.mcp_servers["homeassistant"].disabled_tools
