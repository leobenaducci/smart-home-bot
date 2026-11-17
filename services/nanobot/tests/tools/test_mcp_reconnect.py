"""A dead MCP session is replaced without restarting nanobot.

After upstream HKUDS/nanobot#4171 (reconnect terminated sessions) and #5535
(retry servers that were not connected), re-expressed for this fork's loop.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

import nanobot.agent.tools.mcp as mcp_mod
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.mcp import MCPToolWrapper


class _FakeMcpError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.error = SimpleNamespace(message=message)


class ClosedResourceError(Exception):  # name matches the transient list
    pass


def _text_result(text: str):
    from mcp import types
    return SimpleNamespace(content=[types.TextContent(type="text", text=text)])


def _wrapper(session) -> MCPToolWrapper:
    tool_def = SimpleNamespace(name="demo", description="d", inputSchema={"type": "object", "properties": {}})
    return MCPToolWrapper(session, "ha", tool_def, tool_timeout=1)


def _session(*outcomes):
    calls = []

    async def call_tool(name, arguments):
        calls.append(name)
        out = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(out, BaseException):
            raise out
        return _text_result(out)

    return SimpleNamespace(call_tool=call_tool, calls=calls)


@pytest.mark.asyncio
async def test_terminated_session_is_replaced_and_the_call_retried() -> None:
    dead = _session(_FakeMcpError("Session terminated"))
    live = _session("lights on")
    asked = []

    async def reconnect(server, stale):
        asked.append((server, stale))
        return live

    w = _wrapper(dead)
    w._reconnect = reconnect
    assert await w.execute() == "lights on"
    assert asked == [("ha", dead)]
    assert w._session is live  # later calls use the new session too


@pytest.mark.asyncio
async def test_transient_twice_renews_the_session(monkeypatch) -> None:
    real_sleep = asyncio.sleep
    monkeypatch.setattr(mcp_mod.asyncio, "sleep", lambda *_: real_sleep(0))
    dead = _session(ClosedResourceError(), ClosedResourceError())
    live = _session("ok")

    async def reconnect(server, stale):
        return live

    w = _wrapper(dead)
    w._reconnect = reconnect
    assert await w.execute() == "ok"
    assert len(dead.calls) == 2  # the same-session retry still comes first


@pytest.mark.asyncio
async def test_no_reconnect_handler_keeps_the_old_answer() -> None:
    w = _wrapper(_session(_FakeMcpError("Connection closed")))
    assert await w.execute() == "(MCP tool call failed: _FakeMcpError)"


@pytest.mark.asyncio
async def test_unreachable_server_fails_once_not_in_a_loop() -> None:
    dead = _session(_FakeMcpError("Session terminated"))
    asked = []

    async def reconnect(server, stale):
        asked.append(server)
        return None

    w = _wrapper(dead)
    w._reconnect = reconnect
    assert await w.execute() == "(MCP tool call failed: _FakeMcpError)"
    assert asked == ["ha"]


def _loop_state(servers):
    st = SimpleNamespace(
        _mcp_servers=servers, _mcp_stacks={}, _mcp_sessions={}, _mcp_retired=[],
        _mcp_reconnect_locks={}, _mcp_last_attempt=0.0, _mcp_connected=False,
        _mcp_connecting=False, _MCP_RETRY_S=AgentLoop._MCP_RETRY_S, tools=object(),
    )
    st._reconnect_mcp_server = lambda *a: AgentLoop._reconnect_mcp_server(st, *a)
    return st


@pytest.mark.asyncio
async def test_concurrent_wrappers_reconnect_a_server_once(monkeypatch) -> None:
    connects = []

    async def fake_connect(servers, registry, *, reconnect=None, sessions=None):
        connects.append(set(servers))
        await asyncio.sleep(0.01)
        new = SimpleNamespace(tag="fresh")
        sessions.update({n: new for n in servers})
        return {n: SimpleNamespace(tag="stack") for n in servers}

    monkeypatch.setattr(mcp_mod, "connect_mcp_servers", fake_connect)
    st = _loop_state({"ha": object()})
    dead = object()
    old_stack = object()
    st._mcp_sessions["ha"], st._mcp_stacks["ha"] = dead, old_stack

    a, b = await asyncio.gather(AgentLoop._reconnect_mcp_server(st, "ha", dead),
                                AgentLoop._reconnect_mcp_server(st, "ha", dead))
    assert connects == [{"ha"}]
    assert a is b and a.tag == "fresh"
    assert st._mcp_retired == [old_stack]  # closed at shutdown, not from the turn


@pytest.mark.asyncio
async def test_missing_servers_are_retried_but_not_every_turn(monkeypatch) -> None:
    up = {"ha"}
    connects = []

    async def fake_connect(servers, registry, *, reconnect=None, sessions=None):
        connects.append(sorted(servers))
        return {n: object() for n in servers if n in up}

    monkeypatch.setattr(mcp_mod, "connect_mcp_servers", fake_connect)
    st = _loop_state({"ha": object(), "crawl": object()})

    await AgentLoop._connect_mcp(st)
    assert connects == [["crawl", "ha"]] and set(st._mcp_stacks) == {"ha"}

    await AgentLoop._connect_mcp(st)  # too soon: no second attempt
    assert len(connects) == 1

    up.add("crawl")
    st._mcp_last_attempt = time.monotonic() - AgentLoop._MCP_RETRY_S - 1
    await AgentLoop._connect_mcp(st)
    assert connects[-1] == ["crawl"]  # only the missing one
    assert set(st._mcp_stacks) == {"ha", "crawl"}

    await AgentLoop._connect_mcp(st)  # all connected: nothing to do
    assert len(connects) == 2
