"""household: the HomeCore task panel timeline for sub-agents."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobot.household import task_panel
from nanobot.providers.base import ToolCallRequest

CHAT = "homeweb:999000111:2026-11-17:3"


@pytest.fixture()
def posted(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(task_panel, "relay", lambda: SimpleNamespace(post=out.append))
    return out


def test_only_homeweb_chats_get_records(posted):
    task_panel.post("5491100000000", "start", "t1", "x")
    task_panel.post(CHAT, "start", "t1", "lights", powerful=False)
    assert posted == [{"type": "task", "chat_id": CHAT, "event": "start", "task_id": "t1",
                       "label": "lights", "status": "", "powerful": False}]


@pytest.mark.asyncio
async def test_the_hook_narrates_thoughts_calls_and_outcomes_in_order(posted):
    hook = task_panel.TaskPanelHook(task_panel.Narrator(CHAT, "t1", "lights"))
    call = ToolCallRequest(id="c1", name="exec", arguments={"command": "ls"})
    ctx = SimpleNamespace(response=SimpleNamespace(content="<think>hm</think>Checking the bulbs."),
                          tool_calls=[call], tool_events=[{"name": "exec", "status": "error", "detail": "boom"}])
    await hook.before_execute_tools(ctx)
    await hook.after_iteration(ctx)
    assert [(p["kind"], p["text"], p["seq"]) for p in posted] == [
        ("thought", "Checking the bulbs.", 1), ("tool", "exec", 2), ("tool_result", "exec", 3)]
    assert posted[2]["detail"] == "boom" and posted[1]["detail"] == '{"command": "ls"}'


def test_progress_is_capped(posted, monkeypatch):
    monkeypatch.setattr(task_panel, "PROGRESS_MAX_EVENTS", 2)
    narrate = task_panel.Narrator(CHAT, "t1", "x")
    for _ in range(5):
        narrate("thought", "again")
    assert len(posted) == 2


def test_the_household_manager_adds_the_hook(tmp_path):
    from agent.conftest import make_provider

    from nanobot.bus.queue import MessageBus
    from nanobot.household.loop import HouseholdAgentLoop

    loop = HouseholdAgentLoop(bus=MessageBus(), provider=make_provider(default_model="m"),
                              workspace=tmp_path, model="m", context_window_tokens=1000)
    record = SimpleNamespace(origin={"chat_id": CHAT}, status=SimpleNamespace(task_id="t1", label="x"))
    hooks = loop.subagents._extra_turn_hooks(record)
    assert any(isinstance(h, task_panel.TaskPanelHook) for h in hooks)


@pytest.mark.asyncio
async def test_an_attached_page_gets_live_subagent_status_frames(tmp_path, monkeypatch):
    import asyncio
    import json
    from unittest.mock import AsyncMock, MagicMock

    from nanobot.channels.websocket.runtime import WebSocketChannel
    from nanobot.channels.websocket.tests.test_websocket_channel import _basic_handler

    monkeypatch.setattr(task_panel, "relay", lambda: MagicMock())
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"], "streaming": True}, bus,
                               gateway=_basic_handler(bus, workspace_path=tmp_path))
    ws = AsyncMock()
    channel._attach(ws, CHAT)
    task_panel.post(CHAT, "start", "t1", "flights")
    task_panel.post(CHAT, "progress", "t1", "flights", kind="tool", text="web_search", detail="", seq=1)
    task_panel.post("homeweb:999000111:other", "done", "t2", "x", status="ok")  # nobody attached
    for _ in range(3):
        await asyncio.sleep(0)
    frames = [json.loads(call.args[0]) for call in ws.send.await_args_list]
    assert [f["subagent_event"] for f in frames] == ["start", "progress"]
    assert frames[0] == {"event": "subagent_status", "chat_id": CHAT, "subagent_event": "start",
                         "task_id": "t1", "label": "flights", "status": ""}
    assert frames[1]["kind"] == "tool" and frames[1]["text"] == "web_search"
