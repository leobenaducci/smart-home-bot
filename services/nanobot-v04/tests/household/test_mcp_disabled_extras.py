"""household: disabledTools applies to an MCP server's resources and prompts.

The house lists Home Assistant's `Assist` prompt and `assist_context_snapshot`
resource in disabledTools; v0.4.1 filtered tools only, so with enabledTools
"*" both became tools on every turn and each start warned they were not found.
"""

from __future__ import annotations

import pytest
from loguru import logger
from tools.test_mcp_tool import (  # noqa: F401 -- the fixture is used by name
    _clear_proxy_env,
    _fake_mcp_module,
    _make_fake_session_with_capabilities,
    connect_mcp_servers,
    fake_mcp_runtime,
)

from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import MCPServerConfig


@pytest.mark.asyncio
async def test_listed_resources_and_prompts_are_not_registered(fake_mcp_runtime):  # noqa: F811
    fake_mcp_runtime["session"] = _make_fake_session_with_capabilities(
        ["HassTurnOn", "GetDateTime"], ["assist_context_snapshot", "areas"], ["Assist"])
    warnings: list[str] = []
    handler = logger.add(lambda m: warnings.append(str(m)), level="WARNING", format="{message}")
    registry = ToolRegistry()
    cfg = MCPServerConfig(command="fake", disabled_tools=[
        "GetDateTime", "mcp_homeassistant_prompt_Assist",
        "mcp_homeassistant_resource_assist_context_snapshot"])
    stacks = await connect_mcp_servers({"homeassistant": cfg}, registry)
    try:
        assert set(registry.tool_names) == {"mcp_homeassistant_HassTurnOn",
                                            "mcp_homeassistant_resource_areas"}
        assert not any("not found" in w for w in warnings), warnings
    finally:
        logger.remove(handler)
        for stack in stacks.values():
            await stack.aclose()
