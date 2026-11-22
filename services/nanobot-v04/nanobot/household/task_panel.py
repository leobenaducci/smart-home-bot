"""household: the HomeCore task panel's live timeline for sub-agents.

The fork's sub-agents narrated themselves: a ``start`` record, then one
``progress`` record per step (the model's own words before a tool call, each
tool call, each outcome), then ``done``. HomeCore's background-task panel
draws them as a timeline, and they are posted whether or not a page is open,
because the tasks run for hours. Upstream only projects task *status*
(``SubagentTaskChanged``), which ``channels/homeweb_durable.py`` relays as a
bare ``changed`` record.

The same records also go out as live ``subagent_status`` frames to the
connections attached to that chat (A16, fork ``WebSocketChannel.send``):
HomeCore relays them to an open page, which keeps its "working on…" badge
and the running task's last line current without polling.

Same record shape as the fork's ``WebSocketChannel.send`` posted:
``{"type": "task", "chat_id", "event": start|progress|done, "task_id",
"label", "status", [kind, text, detail, seq]}``. Only for ``homeweb:`` chats;
best effort: a panel line never fails a task.
"""

from __future__ import annotations

import json
import os
from typing import Any

from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext

PROGRESS_MAX_EVENTS = int(os.environ.get("NANOBOT_SUBAGENT_PROGRESS_MAX", "600"))
PROGRESS_MAX_CHARS = 1000

_RELAY: Any = None


def relay() -> Any:
    global _RELAY
    if _RELAY is None:
        from nanobot.channels.homeweb_relay import HomeCoreRelay

        _RELAY = HomeCoreRelay()
    return _RELAY


def clip(text: str, limit: int = PROGRESS_MAX_CHARS) -> str:
    text = (text or "")[: limit * 8]
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


_CHANNEL: Any = None  # the websocket channel, set by its constructor (A16)
_SENDS: set[Any] = set()


def attach_channel(channel: Any) -> None:
    global _CHANNEL
    _CHANNEL = channel


def _frame(chat_id: str, event: str, task_id: str, label: str, status: str,
           extra: dict[str, Any]) -> None:
    """A live ``subagent_status`` frame to the connections attached to *chat_id*."""
    import asyncio

    channel = _CHANNEL
    if channel is None or not chat_id:
        return
    connections = channel.webui_subscribers(chat_id)
    if not connections:
        return
    fields = {"chat_id": chat_id, "subagent_event": event, "task_id": task_id,
              "label": label, "status": status, **extra}

    async def send() -> None:
        for connection in connections:
            await channel.webui_send_event(connection, "subagent_status", **fields)

    try:
        task = asyncio.get_running_loop().create_task(send())
    except RuntimeError:
        return
    _SENDS.add(task)
    task.add_done_callback(_SENDS.discard)


def post(chat_id: str | None, event: str, task_id: str, label: str = "", **extra: Any) -> None:
    from nanobot.channels.homeweb_relay import HomeCoreRelay

    status = extra.pop("status", "")
    try:
        _frame(chat_id or "", event, task_id, label, status, extra)
    except Exception:  # noqa: BLE001 -- narration must never kill the task
        logger.debug("subagent_status frame failed", exc_info=True)
    if not HomeCoreRelay.owns(chat_id):
        return
    try:
        relay().post({"type": "task", "chat_id": chat_id, "event": event, "task_id": task_id,
                      "label": label, "status": status, **extra})
    except Exception:  # noqa: BLE001 -- narration must never kill the task
        logger.debug("task panel post failed", exc_info=True)


class Narrator:
    """Sequenced, capped progress lines for one task."""

    def __init__(self, chat_id: str | None, task_id: str, label: str) -> None:
        self.chat_id, self.task_id, self.label = chat_id, task_id, label
        self._seq = 0

    def __call__(self, kind: str, text: str = "", detail: str = "", status: str = "") -> None:
        if self._seq >= PROGRESS_MAX_EVENTS:
            return
        self._seq += 1
        post(self.chat_id, "progress", self.task_id, self.label, kind=kind, text=clip(text),
             detail=clip(detail), status=status, seq=self._seq)


class TaskPanelHook(AgentHook):
    """The fork's ``_SubagentHook`` narration, as an extra sub-agent turn hook."""

    def __init__(self, narrate: Narrator) -> None:
        super().__init__()
        self._narrate = narrate

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        from nanobot.utils.helpers import strip_think

        thought = strip_think((context.response.content if context.response else "") or "")
        if thought.strip():
            self._narrate("thought", thought)
        for call in context.tool_calls:
            self._narrate("tool", call.name, detail=json.dumps(call.arguments, ensure_ascii=False))

    async def after_iteration(self, context: AgentHookContext) -> None:
        for idx, event in enumerate(context.tool_events or []):
            if not isinstance(event, dict):
                continue
            name = event.get("name") or (context.tool_calls[idx].name if idx < len(context.tool_calls) else "")
            status = event.get("status") or ""
            self._narrate("tool_result", name, status=status,
                          detail=str(event.get("detail") or "") if status == "error" else "")


__all__ = ["Narrator", "TaskPanelHook", "post", "relay"]
