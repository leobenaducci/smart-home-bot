"""household: behavioural defaults restored where v0.4.1 changed them."""

import inspect

from nanobot.agent.memory import MemoryStore
from nanobot.config.schema import Config


def test_defaults():
    cfg = Config()
    assert cfg.agents.defaults.session_ttl_minutes == 0  # idle compaction off
    assert cfg.channels.send_tool_hints is False
    assert cfg.gateway.serve_webui is False
    assert cfg.agents.defaults.dream.write_skills is False


def test_concurrency_default_is_three():
    import nanobot.agent.loop as loop

    assert '"NANOBOT_MAX_CONCURRENT_REQUESTS", "3"' in inspect.getsource(loop)


def test_gateway_does_not_serve_the_webui_unless_asked():
    import nanobot.cli.gateway_runtime as gr

    assert "webui_static_dist and config.gateway.serve_webui" in inspect.getsource(gr)


def test_dream_cannot_write_skills_when_off(tmp_path):
    store = MemoryStore(tmp_path)
    tools = store.build_dream_tools(write_skills=False)
    write = tools.get("write_file")
    target = tmp_path / "skills" / "new-skill" / "SKILL.md"
    import asyncio

    result = asyncio.run(write.execute(path=str(target), content="---\nname: x\n---\n"))
    assert not target.exists(), result
    tools_on = store.build_dream_tools(write_skills=True)
    asyncio.run(tools_on.get("write_file").execute(path=str(target), content="x"))
    assert target.exists()


def test_restored_defaults_item9(monkeypatch):
    from nanobot.agent.tools.mcp import _sanitize_mcp_tool_name
    from nanobot.config.schema import Config
    from nanobot.household import health_ok_while_starting
    from nanobot.providers.base import DEFAULT_STREAM_IDLE_TIMEOUT_S

    cfg = Config()
    assert cfg.agents.defaults.max_concurrent_subagents == 2  # the fork's NANOBOT_SUBAGENT_CONCURRENCY
    assert DEFAULT_STREAM_IDLE_TIMEOUT_S == 90.0  # same as the fork's: nothing to restore
    monkeypatch.delenv("NANOBOT_HOUSEHOLD", raising=False)
    assert not health_ok_while_starting(cfg)
    assert _sanitize_mcp_tool_name("mcp_ha_light__turn_on") == "mcp_ha_light_turn_on"
    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")
    assert health_ok_while_starting(cfg)
    cfg.gateway.health_ok_while_starting = False
    assert not health_ok_while_starting(cfg)
    assert _sanitize_mcp_tool_name("mcp_ha_light__turn_on") == "mcp_ha_light__turn_on"


def test_the_api_block_survives_v042_retiring_it(monkeypatch):
    """Every member's config sets api.timeout for the household API server,
    which the gateway mounts; v0.4.2 strips the block as a legacy leftover."""
    from nanobot.config.schema import Config

    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")
    cfg = Config.model_validate({"api": {"timeout": 300}})
    assert cfg.api.timeout == 300
