"""household: a configured MCP server on a private address connects (fork parity).

Home Assistant and home-devices are reached through ``host.docker.internal``,
crawl4ai and the design bridge on the compose network; v0.4.1's SSRF guard refused all of
them. A real MCP server on 127.0.0.1 stands in for them here, with none of the
guard patched out.
"""

from __future__ import annotations

import asyncio
import multiprocessing

import pytest
from agent.test_mcp_reconnect_crash import _free_port, _run_mcp_server, _wait_for_server

from nanobot.agent.tools import mcp as mcp_module
from nanobot.agent.tools.mcp import MCPProvider
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import MCPServerConfig
from nanobot.household import mcp_trust


@pytest.fixture
def server_url():
    ctx = multiprocessing.get_context("spawn")
    port = _free_port()
    ready = ctx.Event()
    process = ctx.Process(target=_run_mcp_server, args=(port, ready), daemon=True)
    process.start()
    ready.wait(timeout=10.0)
    url = f"http://127.0.0.1:{port}/mcp"
    if not asyncio.run(_wait_for_server(url, timeout=10.0)):
        process.terminate()
        pytest.skip("MCP test server did not start")
    yield url
    process.terminate()
    process.join(timeout=5.0)


@pytest.fixture(autouse=True)
def _fresh_trust(monkeypatch):
    monkeypatch.setattr(mcp_trust, "_trusted", set())
    monkeypatch.setattr(mcp_module, "httpx_env_proxy_mounts", lambda: {})
    monkeypatch.setattr(mcp_module, "env_proxy_applies_to_url", lambda url: False)


async def _connect(url):
    registry = ToolRegistry()
    provider = MCPProvider({"devices": MCPServerConfig(type="streamableHttp", url=url,
                                                       enabled_tools=["*"])}, registry)
    await provider.connect()
    return provider, registry


@pytest.mark.asyncio
async def test_a_configured_server_on_a_private_address_connects_and_answers(server_url, monkeypatch):
    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")
    provider, registry = await _connect(server_url)
    try:
        assert provider.connected_server_names == {"devices"}
        assert "Hello, Tomi" in await registry.get("mcp_devices_greet").execute(name="Tomi")
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_without_the_household_upstream_still_refuses_it(server_url, monkeypatch):
    monkeypatch.delenv("NANOBOT_HOUSEHOLD", raising=False)
    monkeypatch.setattr("nanobot.household.runtime.RUNTIME.loop", None)
    provider, _ = await _connect(server_url)
    try:
        assert provider.connected_server_names == set()
    finally:
        await provider.aclose()


def test_only_the_configured_host_is_trusted(monkeypatch):
    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")
    refuse = lambda url: (False, "private")  # noqa: E731
    assert mcp_trust.check_configured("http://host.docker.internal:21068/mcp", refuse) == (True, "")
    assert mcp_trust.check_request("http://host.docker.internal:21068/mcp/x", refuse) == (True, "")
    assert mcp_trust.check_request("http://192.168.88.1/admin", refuse) == (False, "private")
    assert mcp_trust.transport_for("http://192.168.88.1/") is None
