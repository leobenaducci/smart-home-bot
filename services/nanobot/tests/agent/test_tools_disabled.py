"""`tools.disabled`: built-in tools an instance does not offer.

Every tool's schema is in every prompt, used or not -- glob and notebook_edit
were ~2k characters of it on each of Alfred's turns, for tools nobody here uses.
"""
from pathlib import Path

from conftest import mock_provider

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ToolsConfig


def _loop(tmp_path: Path, disabled: list[str]) -> AgentLoop:
    return AgentLoop(bus=MessageBus(), provider=mock_provider("m"), workspace=tmp_path,
                     model="m", tools_config=ToolsConfig(disabled=disabled))


def test_named_tools_are_not_offered(tmp_path):
    loop = _loop(tmp_path, ["glob", "notebook_edit"])
    names = loop.tools.tool_names
    assert "glob" not in names and "notebook_edit" not in names
    assert "grep" in names and "read_file" in names      # the rest untouched


def test_by_default_nothing_is_taken_away(tmp_path):
    names = _loop(tmp_path, []).tools.tool_names
    assert "glob" in names and "notebook_edit" in names


def test_an_unknown_name_is_ignored(tmp_path):
    assert "grep" in _loop(tmp_path, ["no_such_tool"]).tools.tool_names


def test_the_shipped_config_parses_with_it():
    import json
    for p in ("config/config.json", "config/instances/house/config.json"):
        doc = json.loads(Path(p).read_text())
        assert ToolsConfig.model_validate(doc["tools"]).disabled == ["glob", "notebook_edit"]
