"""household: the go-ahead gate holds on upstream's runner (A2 through the hook).

A skill that switches the house asks first; a model that sends the go-ahead
itself, before the person has answered, is refused through the runner's veto
(nothing runs), and the same call after the person said yes is translated and
runs.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from agent.runner_helpers import make_run_spec

from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.household.hooks import HouseholdResponseHook
from nanobot.providers.base import LLMProvider, LLMResponse

GUIDE = '''---
name: lights
description: House lights
---
# lights

## Python Translation Guide

```python
import json

def switch(room, state, confirmed=False):
    print(json.dumps({"room": room, "state": state, "confirmed": confirmed}))
```
'''


class _Exec(Tool):
    name = "exec"
    description = "Run a shell command."
    parameters = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def execute(self, **kwargs):
        self.commands.append(kwargs["command"])
        return "ok"


class _ReadFile(Tool):
    name = "read_file"
    description = "Read a file."
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    async def execute(self, **kwargs):
        return "file"


def _setup(tmp_path):
    skill = tmp_path / "skills" / "lights"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(GUIDE)
    tools = ToolRegistry()
    exec_tool = _Exec()
    tools.register(exec_tool)
    tools.register(_ReadFile())
    system = {
        "role": "system",
        "content": "### Workspace skills (`skills`)\n- **lights** — House lights  `lights/SKILL.md`\n",
    }
    return tools, exec_tool, system


def _provider(replies):
    provider = MagicMock(spec=LLMProvider)
    seen = []
    it = iter(replies)

    async def chat(*, messages, **kwargs):
        seen.append([dict(m) for m in messages])
        return next(it)

    provider.chat_with_retry = chat
    provider.chat_stream_with_retry = chat
    return provider, seen


BLOCK = json.dumps({"skill": "lights", "action": "switch", "room": "Oficina Tomi",
                    "state": "on", "confirmed": True})


@pytest.mark.asyncio
async def test_self_given_go_ahead_is_refused_and_nothing_runs(tmp_path):
    tools, exec_tool, system = _setup(tmp_path)
    provider, seen = _provider([
        LLMResponse(content=BLOCK, tool_calls=[]),
        LLMResponse(content="Shall I switch on Oficina Tomi?", tool_calls=[]),
    ])
    hook = HouseholdResponseHook(tools, workspace=tmp_path, session_key="websocket:homeweb:999000111:2026-11-17")
    result = await AgentRunner().run(make_run_spec(
        provider, initial_messages=[system, {"role": "user", "content": "light on"}],
        tools=tools, model="m", max_iterations=4, max_tool_result_chars=10_000, hook=hook,
    ))
    assert exec_tool.commands == []
    tool_msg = [m for m in seen[1] if m.get("role") == "tool"][0]
    assert "has not been asked" in str(tool_msg["content"])
    assert result.final_content == "Shall I switch on Oficina Tomi?"


@pytest.mark.asyncio
async def test_go_ahead_after_the_person_answered_runs_translated(tmp_path):
    tools, exec_tool, system = _setup(tmp_path)
    provider, seen = _provider([
        LLMResponse(content=BLOCK, tool_calls=[]),
        LLMResponse(content="Done.", tool_calls=[]),
    ])
    history = [
        system,
        {"role": "user", "content": "light on"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c0", "type": "function",
            "function": {"name": "lights", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c0", "name": "exec",
         "content": '{"pending": true, "would_switch": ["Oficina Tomi"]}'},
        {"role": "assistant", "content": "Switch on Oficina Tomi?"},
        {"role": "user", "content": "yes"},
    ]
    hook = HouseholdResponseHook(tools, workspace=tmp_path, session_key="websocket:homeweb:999000111:2026-11-17")
    result = await AgentRunner().run(make_run_spec(
        provider, initial_messages=history, tools=tools, model="m", max_iterations=4,
        max_tool_result_chars=10_000, hook=hook,
    ))
    assert len(exec_tool.commands) == 1 and "base64" in exec_tool.commands[0]
    # History keeps what was asked, not the base64 program.
    assistant = [m for m in seen[1] if m.get("role") == "assistant" and m.get("tool_calls")][-1]
    assert assistant["tool_calls"][0]["function"]["name"] == "lights"
    assert "base64" not in assistant["tool_calls"][0]["function"]["arguments"]
    assert result.final_content == "Done."
