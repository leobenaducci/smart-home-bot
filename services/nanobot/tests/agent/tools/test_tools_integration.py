"""Integration tests for nanobot tools and skills.

Tests each tool in isolation, the tool registry, skills loading,
and the full tool-calling loop with real tool execution.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMResponse, ToolCallRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry(*tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


def _make_provider_with_tool_calls(
    tool_name: str,
    tool_args: dict[str, Any],
    final_response: str = "done",
) -> MagicMock:
    """Return a mock provider that issues one tool call then returns a final response."""
    provider = MagicMock()
    call_count = {"n": 0}

    async def chat_with_retry(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="tc_001", name=tool_name, arguments=tool_args)
                ],
                finish_reason="tool_calls",
            )
        return LLMResponse(content=final_response, tool_calls=[], finish_reason="stop")

    provider.chat_with_retry = chat_with_retry
    return provider


# ===========================================================================
# 1. Tool Registry
# ===========================================================================

class _EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echo the input"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    async def execute(self, text: str = "", **kwargs: Any) -> str:
        return text


class _FailTool(Tool):
    @property
    def name(self) -> str:
        return "fail"

    @property
    def description(self) -> str:
        return "Always fails"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        raise RuntimeError("deliberate failure")


def test_registry_register_and_get():
    reg = ToolRegistry()
    tool = _EchoTool()
    reg.register(tool)
    assert reg.has("echo")
    assert reg.get("echo") is tool
    assert "echo" in reg.tool_names


def test_registry_unregister():
    reg = ToolRegistry()
    reg.register(_EchoTool())
    reg.unregister("echo")
    assert not reg.has("echo")
    assert reg.get("echo") is None


def test_registry_get_definitions_returns_openai_schema():
    reg = _make_registry(_EchoTool())
    defs = reg.get_definitions()
    assert len(defs) == 1
    d = defs[0]
    assert d["type"] == "function"
    assert d["function"]["name"] == "echo"


def test_registry_definitions_cached_until_change():
    reg = _make_registry(_EchoTool())
    first = reg.get_definitions()
    second = reg.get_definitions()
    assert first is second  # cached reference
    reg.register(_FailTool())
    third = reg.get_definitions()
    assert third is not second  # invalidated


def test_registry_prepare_call_valid():
    reg = _make_registry(_EchoTool())
    tool, params, err = reg.prepare_call("echo", {"text": "hello"})
    assert err is None
    assert tool is not None
    assert params == {"text": "hello"}


def test_registry_prepare_call_missing_tool():
    reg = _make_registry(_EchoTool())
    tool, params, err = reg.prepare_call("no_such_tool", {})
    assert err is not None
    assert "not found" in err.lower() or "no_such_tool" in err


def test_registry_prepare_call_missing_required_param():
    reg = _make_registry(_EchoTool())
    tool, params, err = reg.prepare_call("echo", {})
    assert err is not None
    assert "text" in err


@pytest.mark.asyncio
async def test_registry_execute_ok():
    reg = _make_registry(_EchoTool())
    result = await reg.execute("echo", {"text": "ping"})
    assert result == "ping"


@pytest.mark.asyncio
async def test_registry_execute_tool_raises():
    reg = _make_registry(_FailTool())
    result = await reg.execute("fail", {})
    assert "Error" in result


# ===========================================================================
# 2. ReadFileTool
# ===========================================================================

@pytest.mark.asyncio
async def test_read_file_reads_text(tmp_path: Path):
    from nanobot.agent.tools.filesystem import ReadFileTool

    fp = tmp_path / "hello.txt"
    fp.write_text("line one\nline two\n", encoding="utf-8")

    tool = ReadFileTool(workspace=tmp_path)
    result = await tool.execute(path="hello.txt")
    assert isinstance(result, str)
    assert "line one" in result
    assert "line two" in result


@pytest.mark.asyncio
async def test_read_file_missing(tmp_path: Path):
    from nanobot.agent.tools.filesystem import ReadFileTool

    tool = ReadFileTool(workspace=tmp_path)
    result = await tool.execute(path="nonexistent.txt")
    assert result.startswith("Error")


@pytest.mark.asyncio
async def test_read_file_is_directory(tmp_path: Path):
    from nanobot.agent.tools.filesystem import ReadFileTool

    (tmp_path / "subdir").mkdir()
    tool = ReadFileTool(workspace=tmp_path)
    result = await tool.execute(path="subdir")
    assert result.startswith("Error")


@pytest.mark.asyncio
async def test_read_file_line_numbers_in_output(tmp_path: Path):
    from nanobot.agent.tools.filesystem import ReadFileTool

    fp = tmp_path / "numbered.txt"
    fp.write_text("a\nb\nc\n", encoding="utf-8")
    tool = ReadFileTool(workspace=tmp_path)
    result = await tool.execute(path="numbered.txt")
    assert "1|" in result or "1| " in result


@pytest.mark.asyncio
async def test_read_file_offset_and_limit(tmp_path: Path):
    from nanobot.agent.tools.filesystem import ReadFileTool

    fp = tmp_path / "big.txt"
    fp.write_text("\n".join(f"line {i}" for i in range(1, 21)), encoding="utf-8")
    tool = ReadFileTool(workspace=tmp_path)
    result = await tool.execute(path="big.txt", offset=5, limit=3)
    assert "line 5" in result
    assert "line 7" in result
    assert "line 8" not in result


@pytest.mark.asyncio
async def test_read_file_no_path():
    from nanobot.agent.tools.filesystem import ReadFileTool

    tool = ReadFileTool()
    result = await tool.execute(path=None)
    assert result.startswith("Error")


# ===========================================================================
# 3. WriteFileTool
# ===========================================================================

@pytest.mark.asyncio
async def test_write_file_creates_file(tmp_path: Path):
    from nanobot.agent.tools.filesystem import WriteFileTool

    tool = WriteFileTool(workspace=tmp_path)
    result = await tool.execute(path="new.txt", content="hello world")
    assert "Successfully" in result
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello world"


@pytest.mark.asyncio
async def test_write_file_overwrites(tmp_path: Path):
    from nanobot.agent.tools.filesystem import WriteFileTool

    fp = tmp_path / "existing.txt"
    fp.write_text("old content", encoding="utf-8")
    tool = WriteFileTool(workspace=tmp_path)
    await tool.execute(path="existing.txt", content="new content")
    assert fp.read_text(encoding="utf-8") == "new content"


@pytest.mark.asyncio
async def test_write_file_creates_parent_dirs(tmp_path: Path):
    from nanobot.agent.tools.filesystem import WriteFileTool

    tool = WriteFileTool(workspace=tmp_path)
    result = await tool.execute(path="deep/nested/file.txt", content="data")
    assert "Successfully" in result
    assert (tmp_path / "deep" / "nested" / "file.txt").exists()


@pytest.mark.asyncio
async def test_write_file_no_path():
    from nanobot.agent.tools.filesystem import WriteFileTool

    tool = WriteFileTool()
    result = await tool.execute(path=None, content="data")
    assert result.startswith("Error")


# ===========================================================================
# 4. EditFileTool
# ===========================================================================

@pytest.mark.asyncio
async def test_edit_file_replaces_text(tmp_path: Path):
    from nanobot.agent.tools.filesystem import EditFileTool

    fp = tmp_path / "edit.txt"
    fp.write_text("foo bar baz", encoding="utf-8")
    tool = EditFileTool(workspace=tmp_path)
    result = await tool.execute(path="edit.txt", old_text="bar", new_text="qux")
    assert "Successfully" in result
    assert fp.read_text(encoding="utf-8") == "foo qux baz"


@pytest.mark.asyncio
async def test_edit_file_creates_new_file(tmp_path: Path):
    from nanobot.agent.tools.filesystem import EditFileTool

    tool = EditFileTool(workspace=tmp_path)
    result = await tool.execute(path="new.py", old_text="", new_text="print('hello')")
    assert "Successfully" in result
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == "print('hello')"


@pytest.mark.asyncio
async def test_edit_file_not_found_error(tmp_path: Path):
    from nanobot.agent.tools.filesystem import EditFileTool

    tool = EditFileTool(workspace=tmp_path)
    result = await tool.execute(path="missing.py", old_text="x = 1", new_text="x = 2")
    assert result.startswith("Error")


@pytest.mark.asyncio
async def test_edit_file_old_text_not_found(tmp_path: Path):
    from nanobot.agent.tools.filesystem import EditFileTool

    fp = tmp_path / "code.py"
    fp.write_text("x = 1\n", encoding="utf-8")
    tool = EditFileTool(workspace=tmp_path)
    result = await tool.execute(path="code.py", old_text="y = 99", new_text="y = 100")
    assert result.startswith("Error")


@pytest.mark.asyncio
async def test_edit_file_replace_all(tmp_path: Path):
    from nanobot.agent.tools.filesystem import EditFileTool

    fp = tmp_path / "multi.txt"
    fp.write_text("foo foo foo", encoding="utf-8")
    tool = EditFileTool(workspace=tmp_path)
    result = await tool.execute(path="multi.txt", old_text="foo", new_text="bar", replace_all=True)
    assert "Successfully" in result
    assert fp.read_text(encoding="utf-8") == "bar bar bar"


@pytest.mark.asyncio
async def test_edit_file_multiple_match_error(tmp_path: Path):
    from nanobot.agent.tools.filesystem import EditFileTool

    fp = tmp_path / "dup.txt"
    fp.write_text("ab ab ab", encoding="utf-8")
    tool = EditFileTool(workspace=tmp_path)
    result = await tool.execute(path="dup.txt", old_text="ab", new_text="xy")
    assert "Warning" in result or "Error" in result


# ===========================================================================
# 5. ListDirTool
# ===========================================================================

@pytest.mark.asyncio
async def test_list_dir_basic(tmp_path: Path):
    from nanobot.agent.tools.filesystem import ListDirTool

    (tmp_path / "a.txt").write_text("", encoding="utf-8")
    (tmp_path / "b.py").write_text("", encoding="utf-8")
    (tmp_path / "subdir").mkdir()

    tool = ListDirTool(workspace=tmp_path)
    result = await tool.execute(path=str(tmp_path))
    assert "a.txt" in result
    assert "b.py" in result
    assert "subdir" in result


@pytest.mark.asyncio
async def test_list_dir_empty(tmp_path: Path):
    from nanobot.agent.tools.filesystem import ListDirTool

    empty = tmp_path / "empty_dir"
    empty.mkdir()
    tool = ListDirTool()
    result = await tool.execute(path=str(empty))
    assert "empty" in result.lower()


@pytest.mark.asyncio
async def test_list_dir_missing(tmp_path: Path):
    from nanobot.agent.tools.filesystem import ListDirTool

    tool = ListDirTool()
    result = await tool.execute(path=str(tmp_path / "no_such_dir"))
    assert result.startswith("Error")


@pytest.mark.asyncio
async def test_list_dir_recursive(tmp_path: Path):
    from nanobot.agent.tools.filesystem import ListDirTool

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.txt").write_text("", encoding="utf-8")

    tool = ListDirTool(workspace=tmp_path)
    result = await tool.execute(path=str(tmp_path), recursive=True)
    assert "deep.txt" in result


# ===========================================================================
# 6. GlobTool
# ===========================================================================

@pytest.mark.asyncio
async def test_glob_finds_py_files(tmp_path: Path):
    from nanobot.agent.tools.search import GlobTool

    (tmp_path / "main.py").write_text("", encoding="utf-8")
    (tmp_path / "utils.py").write_text("", encoding="utf-8")
    (tmp_path / "readme.md").write_text("", encoding="utf-8")

    tool = GlobTool(workspace=tmp_path)
    result = await tool.execute(pattern="*.py", path=str(tmp_path))
    assert "main.py" in result
    assert "utils.py" in result
    assert "readme.md" not in result


@pytest.mark.asyncio
async def test_glob_no_matches(tmp_path: Path):
    from nanobot.agent.tools.search import GlobTool

    tool = GlobTool(workspace=tmp_path)
    result = await tool.execute(pattern="*.xyz", path=str(tmp_path))
    assert "No" in result or result.strip() == "" or "0" in result


@pytest.mark.asyncio
async def test_glob_nested(tmp_path: Path):
    from nanobot.agent.tools.search import GlobTool

    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "module.py").write_text("", encoding="utf-8")

    tool = GlobTool(workspace=tmp_path)
    result = await tool.execute(pattern="**/*.py", path=str(tmp_path))
    assert "module.py" in result


# ===========================================================================
# 7. GrepTool
# ===========================================================================

@pytest.mark.asyncio
async def test_grep_finds_pattern(tmp_path: Path):
    from nanobot.agent.tools.search import GrepTool

    (tmp_path / "code.py").write_text("def hello():\n    pass\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("x = 1\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path)
    result = await tool.execute(pattern="def hello", path=str(tmp_path))
    assert "code.py" in result
    assert "other.py" not in result


@pytest.mark.asyncio
async def test_grep_no_matches(tmp_path: Path):
    from nanobot.agent.tools.search import GrepTool

    (tmp_path / "file.txt").write_text("nothing here", encoding="utf-8")
    tool = GrepTool(workspace=tmp_path)
    result = await tool.execute(pattern="ZZZNOMATCH", path=str(tmp_path))
    assert "No" in result or "0" in result or result.strip() == ""


@pytest.mark.asyncio
async def test_grep_case_insensitive(tmp_path: Path):
    from nanobot.agent.tools.search import GrepTool

    (tmp_path / "note.txt").write_text("Hello World\n", encoding="utf-8")
    tool = GrepTool(workspace=tmp_path)
    result = await tool.execute(pattern="hello world", path=str(tmp_path), case_insensitive=True)
    assert "note.txt" in result


@pytest.mark.asyncio
async def test_grep_file_type_filter(tmp_path: Path):
    from nanobot.agent.tools.search import GrepTool

    (tmp_path / "a.py").write_text("pattern_here\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("pattern_here\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path)
    result = await tool.execute(pattern="pattern_here", path=str(tmp_path), type="py")
    assert "a.py" in result
    # b.txt should be filtered out (not a .py file)
    assert "b.txt" not in result


# ===========================================================================
# 8. ExecTool
# ===========================================================================

@pytest.mark.asyncio
async def test_exec_simple_command():
    from nanobot.agent.tools.shell import ExecTool

    tool = ExecTool(timeout=10)
    if sys.platform == "win32":
        result = await tool.execute(command="echo hello_world")
    else:
        result = await tool.execute(command="echo hello_world")
    assert "hello_world" in result


@pytest.mark.asyncio
async def test_exec_blocked_rm_rf():
    from nanobot.agent.tools.shell import ExecTool

    tool = ExecTool()
    result = await tool.execute(command="rm -rf /tmp/test")
    assert result.startswith("Error")


@pytest.mark.asyncio
async def test_exec_working_dir(tmp_path: Path):
    from nanobot.agent.tools.shell import ExecTool

    (tmp_path / "marker.txt").write_text("found_it", encoding="utf-8")
    tool = ExecTool(timeout=10)
    if sys.platform == "win32":
        result = await tool.execute(command="type marker.txt", working_dir=str(tmp_path))
    else:
        result = await tool.execute(command="cat marker.txt", working_dir=str(tmp_path))
    assert "found_it" in result


@pytest.mark.asyncio
async def test_exec_timeout():
    from nanobot.agent.tools.shell import ExecTool

    tool = ExecTool(timeout=1)
    if sys.platform == "win32":
        result = await tool.execute(command="ping -n 10 127.0.0.1", timeout=1)
    else:
        result = await tool.execute(command="sleep 10", timeout=1)
    assert "timeout" in result.lower() or "timed" in result.lower() or "Error" in result


@pytest.mark.asyncio
async def test_exec_nonexistent_command():
    from nanobot.agent.tools.shell import ExecTool

    tool = ExecTool(timeout=10)
    result = await tool.execute(command="nonexistent_command_xyz_12345")
    # Should return error, not raise exception
    assert isinstance(result, str)
    assert len(result) > 0


# ===========================================================================
# 9. MessageTool
# ===========================================================================

@pytest.mark.asyncio
async def test_message_tool_sends_to_callback():
    from nanobot.agent.tools.message import MessageTool

    sent_messages = []

    async def capture(msg):
        sent_messages.append(msg)

    tool = MessageTool(
        send_callback=capture,
        default_channel="test",
        default_chat_id="chat123",
    )
    result = await tool.execute(content="Hello, user!")
    assert "sent" in result.lower()
    assert len(sent_messages) == 1
    assert sent_messages[0].content == "Hello, user!"


@pytest.mark.asyncio
async def test_message_tool_no_callback():
    from nanobot.agent.tools.message import MessageTool

    tool = MessageTool(default_channel="test", default_chat_id="chat123")
    result = await tool.execute(content="Hello")
    assert result.startswith("Error")


@pytest.mark.asyncio
async def test_message_tool_no_channel():
    from nanobot.agent.tools.message import MessageTool

    async def noop(msg):
        pass

    tool = MessageTool(send_callback=noop)
    result = await tool.execute(content="test")
    assert result.startswith("Error")


@pytest.mark.asyncio
async def test_message_tool_with_media():
    from nanobot.agent.tools.message import MessageTool

    sent = []

    async def capture(msg):
        sent.append(msg)

    tool = MessageTool(
        send_callback=capture,
        default_channel="cli",
        default_chat_id="u1",
    )
    result = await tool.execute(content="here", media=["/path/to/file.png"])
    assert "sent" in result.lower()
    assert sent[0].media == ["/path/to/file.png"]


@pytest.mark.asyncio
async def test_message_tool_invalid_buttons():
    from nanobot.agent.tools.message import MessageTool

    async def noop(msg):
        pass

    tool = MessageTool(
        send_callback=noop,
        default_channel="cli",
        default_chat_id="u1",
    )
    result = await tool.execute(content="test", buttons="not_a_list")
    assert result.startswith("Error")


# ===========================================================================
# 10. Skills Loader
# ===========================================================================

def test_skills_loader_lists_builtin_skills(tmp_path: Path):
    from nanobot.agent.skills import SkillsLoader

    loader = SkillsLoader(workspace=tmp_path)
    skills = loader.list_skills(filter_unavailable=False)
    assert isinstance(skills, list)
    # There should be at least some built-in skills
    assert len(skills) >= 1
    for skill in skills:
        assert "name" in skill
        assert "path" in skill
        assert "source" in skill


def test_skills_loader_loads_skill_content(tmp_path: Path):
    from nanobot.agent.skills import SkillsLoader

    loader = SkillsLoader(workspace=tmp_path)
    skills = loader.list_skills(filter_unavailable=False)
    assert skills, "Need at least one builtin skill to test"

    # Load the first available skill
    skill_name = skills[0]["name"]
    content = loader.load_skill(skill_name)
    assert content is not None
    assert len(content) > 0


def test_skills_loader_workspace_skill_overrides_builtin(tmp_path: Path):
    from nanobot.agent.skills import SkillsLoader

    # Create a workspace skill that overrides a builtin
    skills_dir = tmp_path / "skills" / "custom_skill"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: custom_skill\ndescription: A custom test skill\n---\n# Custom Skill\nThis is custom content.",
        encoding="utf-8",
    )

    loader = SkillsLoader(workspace=tmp_path)
    skills = loader.list_skills(filter_unavailable=False)
    names = [s["name"] for s in skills]
    assert "custom_skill" in names

    # Should load from workspace
    content = loader.load_skill("custom_skill")
    assert content is not None
    assert "custom content" in content


def test_skills_loader_disabled_skill_excluded(tmp_path: Path):
    from nanobot.agent.skills import SkillsLoader

    loader_all = SkillsLoader(workspace=tmp_path)
    all_skills = loader_all.list_skills(filter_unavailable=False)
    if not all_skills:
        pytest.skip("No builtin skills to test")

    first_name = all_skills[0]["name"]
    loader_disabled = SkillsLoader(workspace=tmp_path, disabled_skills={first_name})
    filtered_skills = loader_disabled.list_skills(filter_unavailable=False)
    filtered_names = [s["name"] for s in filtered_skills]
    assert first_name not in filtered_names


def test_skills_loader_missing_skill_returns_none(tmp_path: Path):
    from nanobot.agent.skills import SkillsLoader

    loader = SkillsLoader(workspace=tmp_path)
    result = loader.load_skill("definitely_nonexistent_skill_xyz")
    assert result is None


# ===========================================================================
# 11. Full Runner Loop with Real Tools
# ===========================================================================

@pytest.mark.asyncio
async def test_runner_loop_executes_read_file(tmp_path: Path):
    """Runner calls read_file tool and returns its result to the LLM."""
    from nanobot.agent.runner import AgentRunSpec, AgentRunner
    from nanobot.agent.tools.filesystem import ReadFileTool

    test_file = tmp_path / "data.txt"
    test_file.write_text("secret_content_42", encoding="utf-8")

    tool = ReadFileTool(workspace=tmp_path)
    registry = _make_registry(tool)

    file_read_results: list[str] = []
    call_count = {"n": 0}

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(
                    id="tc_read",
                    name="read_file",
                    arguments={"path": "data.txt"},
                )],
                finish_reason="tool_calls",
            )
        # Record tool results visible to LLM on second call
        for msg in messages:
            if msg.get("role") == "tool":
                file_read_results.append(str(msg.get("content", "")))
        return LLMResponse(content="file content received", tool_calls=[], finish_reason="stop")

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "read the file"}],
        tools=registry,
        model="test-model",
        max_iterations=5,
        max_tool_result_chars=16000,
        workspace=tmp_path,
        session_key="test",
    ))

    assert result.final_content == "file content received"
    assert result.tools_used == ["read_file"]
    assert any("secret_content_42" in r for r in file_read_results)


@pytest.mark.asyncio
async def test_runner_loop_executes_write_then_read(tmp_path: Path):
    """Runner writes a file, then reads it back in consecutive tool calls."""
    from nanobot.agent.runner import AgentRunSpec, AgentRunner
    from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool

    registry = _make_registry(
        WriteFileTool(workspace=tmp_path),
        ReadFileTool(workspace=tmp_path),
    )

    call_count = {"n": 0}
    read_contents: list[str] = []

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(
                    id="tc_write",
                    name="write_file",
                    arguments={"path": "output.txt", "content": "written_data"},
                )],
                finish_reason="tool_calls",
            )
        if call_count["n"] == 2:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(
                    id="tc_read",
                    name="read_file",
                    arguments={"path": "output.txt"},
                )],
                finish_reason="tool_calls",
            )
        for msg in messages:
            if msg.get("role") == "tool" and msg.get("name") == "read_file":
                read_contents.append(str(msg.get("content", "")))
        return LLMResponse(content="all done", tool_calls=[], finish_reason="stop")

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "write then read"}],
        tools=registry,
        model="test",
        max_iterations=10,
        max_tool_result_chars=16000,
        workspace=tmp_path,
        session_key="test2",
    ))

    assert result.final_content == "all done"
    assert "write_file" in result.tools_used
    assert "read_file" in result.tools_used
    assert any("written_data" in c for c in read_contents)


@pytest.mark.asyncio
async def test_runner_loop_executes_exec_tool(tmp_path: Path):
    """Runner executes a shell command and returns output to LLM."""
    from nanobot.agent.runner import AgentRunSpec, AgentRunner
    from nanobot.agent.tools.shell import ExecTool

    registry = _make_registry(ExecTool(timeout=10))

    exec_results: list[str] = []
    call_count = {"n": 0}

    cmd = "echo runner_exec_test" if sys.platform != "win32" else "echo runner_exec_test"

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(
                    id="tc_exec",
                    name="exec",
                    arguments={"command": cmd},
                )],
                finish_reason="tool_calls",
            )
        for msg in messages:
            if msg.get("role") == "tool":
                exec_results.append(str(msg.get("content", "")))
        return LLMResponse(content="exec done", tool_calls=[], finish_reason="stop")

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "run echo"}],
        tools=registry,
        model="test",
        max_iterations=5,
        max_tool_result_chars=16000,
    ))

    assert result.final_content == "exec done"
    assert any("runner_exec_test" in r for r in exec_results)


@pytest.mark.asyncio
async def test_runner_tool_error_does_not_crash_loop():
    """Tool errors return error strings but the runner continues."""
    from nanobot.agent.runner import AgentRunSpec, AgentRunner

    registry = _make_registry(_FailTool())

    call_count = {"n": 0}

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="tc1", name="fail", arguments={})],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="recovered", tool_calls=[], finish_reason="stop")

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "try"}],
        tools=registry,
        model="test",
        max_iterations=5,
        max_tool_result_chars=16000,
    ))

    # Runner should continue and return final response
    assert result.final_content == "recovered"
    assert "fail" in result.tools_used


@pytest.mark.asyncio
async def test_runner_concurrent_tools(tmp_path: Path):
    """Concurrent-safe tools run in parallel without issues."""
    from nanobot.agent.runner import AgentRunSpec, AgentRunner
    from nanobot.agent.tools.filesystem import ReadFileTool

    (tmp_path / "f1.txt").write_text("content1", encoding="utf-8")
    (tmp_path / "f2.txt").write_text("content2", encoding="utf-8")

    registry = _make_registry(ReadFileTool(workspace=tmp_path))

    call_count = {"n": 0}

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="tc1", name="read_file", arguments={"path": "f1.txt"}),
                    ToolCallRequest(id="tc2", name="read_file", arguments={"path": "f2.txt"}),
                ],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="both read", tool_calls=[], finish_reason="stop")

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "read both"}],
        tools=registry,
        model="test",
        max_iterations=5,
        max_tool_result_chars=16000,
        concurrent_tools=True,
        workspace=tmp_path,
    ))

    assert result.final_content == "both read"
    assert result.tools_used.count("read_file") == 2


# ===========================================================================
# 12. Tool Parameter Casting and Validation
# ===========================================================================

def test_tool_cast_string_to_int():
    """Tool.cast_params coerces string "5" to int for integer params."""
    from nanobot.agent.tools.filesystem import ReadFileTool

    tool = ReadFileTool()
    params = tool.cast_params({"path": "foo.txt", "offset": "3", "limit": "10"})
    assert params["offset"] == 3
    assert params["limit"] == 10


def test_tool_cast_string_bool():
    """Tool.cast_params coerces "true"/"false" to bool."""
    from nanobot.agent.tools.filesystem import ListDirTool

    tool = ListDirTool()
    params = tool.cast_params({"path": "/tmp", "recursive": "true"})
    assert params["recursive"] is True

    params2 = tool.cast_params({"path": "/tmp", "recursive": "false"})
    assert params2["recursive"] is False


def test_tool_validate_missing_required():
    from nanobot.agent.tools.filesystem import WriteFileTool

    tool = WriteFileTool()
    errors = tool.validate_params({"content": "data"})  # path missing
    assert any("path" in e for e in errors)


def test_tool_schema_exported_correctly():
    from nanobot.agent.tools.filesystem import ReadFileTool

    schema = ReadFileTool().to_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "read_file"
    assert "path" in fn["parameters"]["properties"]


# ===========================================================================
# 13. Tool Call Message Format (Custom Parsing)
# ===========================================================================

def test_tool_call_request_to_openai_format():
    """ToolCallRequest serializes to the correct OpenAI wire format."""
    tc = ToolCallRequest(id="abc123", name="read_file", arguments={"path": "foo.txt"})
    d = tc.to_openai_tool_call()
    assert d["id"] == "abc123"
    assert d["type"] == "function"
    assert d["function"]["name"] == "read_file"
    import json
    args = json.loads(d["function"]["arguments"])
    assert args["path"] == "foo.txt"


def test_tool_call_arguments_are_json_string():
    """Arguments in OpenAI format must be a JSON string, not a dict."""
    tc = ToolCallRequest(id="x", name="exec", arguments={"command": "ls"})
    d = tc.to_openai_tool_call()
    assert isinstance(d["function"]["arguments"], str)


@pytest.mark.asyncio
async def test_runner_handles_string_arguments_in_tool_call(tmp_path: Path):
    """Runner correctly handles tool calls where arguments come as a JSON string."""
    from nanobot.agent.runner import AgentRunSpec, AgentRunner
    from nanobot.agent.tools.filesystem import WriteFileTool

    registry = _make_registry(WriteFileTool(workspace=tmp_path))
    call_count = {"n": 0}

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Simulate LLM returning arguments as already-parsed dict (normal case)
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(
                    id="tc",
                    name="write_file",
                    arguments={"path": "out.txt", "content": "test_data"},
                )],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="written", tool_calls=[], finish_reason="stop")

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "write it"}],
        tools=registry,
        model="test",
        max_iterations=5,
        max_tool_result_chars=16000,
        workspace=tmp_path,
    ))

    assert result.final_content == "written"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "test_data"


# ===========================================================================
# 14. GrepTool - output format and count
# ===========================================================================

@pytest.mark.asyncio
async def test_grep_returns_line_numbers(tmp_path: Path):
    from nanobot.agent.tools.search import GrepTool

    fp = tmp_path / "src.py"
    fp.write_text("line1\ntarget_line\nline3\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path)
    # Use content mode to get matching line text (default is files_with_matches)
    result = await tool.execute(pattern="target_line", path=str(tmp_path), output_mode="content")
    assert "target_line" in result


@pytest.mark.asyncio
async def test_grep_context_lines(tmp_path: Path):
    from nanobot.agent.tools.search import GrepTool

    fp = tmp_path / "ctx.txt"
    fp.write_text("before\nmatch_me\nafter\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path)
    result = await tool.execute(
        pattern="match_me", path=str(tmp_path),
        output_mode="content", context_before=1, context_after=1,
    )
    assert "match_me" in result


# ===========================================================================
# 15. Custom Tool via tool_parameters decorator
# ===========================================================================

def test_custom_tool_via_decorator():
    """Verify the @tool_parameters decorator properly wires up schema."""
    from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

    @tool_parameters(
        tool_parameters_schema(
            name=StringSchema("Name to greet"),
            required=["name"],
        )
    )
    class GreetTool(Tool):
        @property
        def name(self) -> str:
            return "greet"

        @property
        def description(self) -> str:
            return "Greet someone"

        async def execute(self, name: str = "", **kwargs: Any) -> str:
            return f"Hello, {name}!"

    t = GreetTool()
    assert t.name == "greet"
    params = t.parameters
    assert "name" in params["properties"]
    schema = t.to_schema()
    assert schema["function"]["name"] == "greet"


@pytest.mark.asyncio
async def test_custom_tool_executes_correctly():
    from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

    @tool_parameters(
        tool_parameters_schema(
            name=StringSchema("Name to greet"),
            required=["name"],
        )
    )
    class GreetTool(Tool):
        @property
        def name(self) -> str:
            return "greet"

        @property
        def description(self) -> str:
            return "Greet someone"

        async def execute(self, name: str = "", **kwargs: Any) -> str:
            return f"Hello, {name}!"

    tool = GreetTool()
    result = await tool.execute(name="world")
    assert result == "Hello, world!"


@pytest.mark.asyncio
async def test_runner_with_custom_tool():
    """Full loop: custom tool via decorator integrates with runner."""
    from nanobot.agent.runner import AgentRunSpec, AgentRunner
    from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

    @tool_parameters(
        tool_parameters_schema(
            name=StringSchema("Name to greet"),
            required=["name"],
        )
    )
    class GreetTool(Tool):
        @property
        def name(self) -> str:
            return "greet"

        @property
        def description(self) -> str:
            return "Greet someone"

        async def execute(self, name: str = "", **kwargs: Any) -> str:
            return f"Hello, {name}!"

    registry = _make_registry(GreetTool())
    call_count = {"n": 0}
    greet_results: list[str] = []

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="tc_g", name="greet", arguments={"name": "Alice"})],
                finish_reason="tool_calls",
            )
        for msg in messages:
            if msg.get("role") == "tool":
                greet_results.append(str(msg.get("content", "")))
        return LLMResponse(content="greeted", tool_calls=[], finish_reason="stop")

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "greet alice"}],
        tools=registry,
        model="test",
        max_iterations=5,
        max_tool_result_chars=16000,
    ))

    assert result.final_content == "greeted"
    assert any("Hello, Alice!" in r for r in greet_results)


# ===========================================================================
# 16. Text-based tool call parsing (extract_text_tool_calls)
# ===========================================================================

def test_text_tc_simple_string_arg():
    from nanobot.agent.runner import extract_text_tool_calls
    calls = extract_text_tool_calls(
        'read_file(path="/tmp/foo.txt")',
        frozenset(["read_file"]),
    )
    assert len(calls) == 1
    assert calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "/tmp/foo.txt"}


def test_text_tc_multiple_calls_in_order():
    from nanobot.agent.runner import extract_text_tool_calls
    text = (
        "I will read the skill first.\n"
        'read_file(path="skill.md")\n'
        "Then execute:\n"
        'exec(command="curl http://api/lights")\n'
    )
    calls = extract_text_tool_calls(text, frozenset(["read_file", "exec"]))
    assert len(calls) == 2
    assert calls[0].name == "read_file"
    assert calls[1].name == "exec"
    assert calls[1].arguments["command"] == "curl http://api/lights"


def test_text_tc_ignores_unknown_tools():
    from nanobot.agent.runner import extract_text_tool_calls
    calls = extract_text_tool_calls(
        'unknown_func(x=1)\nread_file(path="ok.txt")',
        frozenset(["read_file"]),
    )
    assert len(calls) == 1
    assert calls[0].name == "read_file"


def test_text_tc_no_calls_returns_empty():
    from nanobot.agent.runner import extract_text_tool_calls
    calls = extract_text_tool_calls(
        "Here is some plain text with no tool calls.",
        frozenset(["exec", "read_file"]),
    )
    assert calls == []


def test_text_tc_multiple_kwargs():
    from nanobot.agent.runner import extract_text_tool_calls
    calls = extract_text_tool_calls(
        'write_file(path="out.txt", content="hello world")',
        frozenset(["write_file"]),
    )
    assert len(calls) == 1
    assert calls[0].arguments == {"path": "out.txt", "content": "hello world"}


def test_text_tc_command_with_spaces_and_quotes():
    from nanobot.agent.runner import extract_text_tool_calls
    calls = extract_text_tool_calls(
        """exec(command="curl -s 'http://hub.home:5010/api/lights'")""",
        frozenset(["exec"]),
    )
    assert len(calls) == 1
    assert "hub.home" in calls[0].arguments["command"]


def test_text_tc_in_markdown_code_block():
    from nanobot.agent.runner import extract_text_tool_calls
    text = '\nI will do this:\n\n```\nexec(command="echo hi")\n```\n'
    calls = extract_text_tool_calls(text, frozenset(["exec"]))
    assert len(calls) == 1
    assert calls[0].arguments["command"] == "echo hi"


def test_text_tc_each_has_unique_id():
    from nanobot.agent.runner import extract_text_tool_calls
    text = 'exec(command="ls")\nexec(command="pwd")'
    calls = extract_text_tool_calls(text, frozenset(["exec"]))
    assert len(calls) == 2
    assert calls[0].id != calls[1].id


def test_text_tc_boolean_and_int_args():
    from nanobot.agent.runner import extract_text_tool_calls
    calls = extract_text_tool_calls(
        "list_dir(path=\"/tmp\", recursive=True, max_entries=50)",
        frozenset(["list_dir"]),
    )
    assert len(calls) == 1
    args = calls[0].arguments
    assert args["recursive"] is True
    assert args["max_entries"] == 50


# ===========================================================================
# 17. Runner loop: text tool calls trigger real execution
# ===========================================================================

@pytest.mark.asyncio
async def test_runner_text_tool_call_parsed_and_executed(tmp_path: Path):
    """Runner parses text-format tool calls and executes them."""
    from nanobot.agent.runner import AgentRunSpec, AgentRunner
    from nanobot.agent.tools.filesystem import ReadFileTool

    (tmp_path / "data.txt").write_text("text_content_99", encoding="utf-8")
    registry = _make_registry(ReadFileTool(workspace=tmp_path))

    call_count = {"n": 0}
    tool_results: list[str] = []

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content='Let me read that file.\nread_file(path="data.txt")',
                tool_calls=[],
                finish_reason="stop",
            )
        for msg in messages:
            if msg.get("role") == "tool":
                tool_results.append(str(msg.get("content", "")))
        return LLMResponse(content="file read successfully", tool_calls=[], finish_reason="stop")

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "read data.txt"}],
        tools=registry,
        model="test",
        max_iterations=5,
        max_tool_result_chars=16000,
        workspace=tmp_path,
    ))

    assert result.final_content == "file read successfully"
    assert "read_file" in result.tools_used
    assert any("text_content_99" in r for r in tool_results)


@pytest.mark.asyncio
async def test_runner_text_tool_calls_chained(tmp_path: Path):
    """Multi-step chain: model writes write_file then read_file as text calls."""
    from nanobot.agent.runner import AgentRunSpec, AgentRunner
    from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool

    registry = _make_registry(
        WriteFileTool(workspace=tmp_path),
        ReadFileTool(workspace=tmp_path),
    )

    call_count = {"n": 0}
    seen_tool_names: list[str] = []

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content='write_file(path="chain.txt", content="chain_value")',
                tool_calls=[],
                finish_reason="stop",
            )
        if call_count["n"] == 2:
            for msg in messages:
                if msg.get("role") == "tool":
                    seen_tool_names.append(msg.get("name", ""))
            return LLMResponse(
                content='read_file(path="chain.txt")',
                tool_calls=[],
                finish_reason="stop",
            )
        for msg in messages:
            if msg.get("role") == "tool":
                seen_tool_names.append(msg.get("name", ""))
        return LLMResponse(content="chain complete", tool_calls=[], finish_reason="stop")

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "write then read"}],
        tools=registry,
        model="test",
        max_iterations=10,
        max_tool_result_chars=16000,
        workspace=tmp_path,
    ))

    assert result.final_content == "chain complete"
    assert "write_file" in seen_tool_names
    assert "read_file" in seen_tool_names


@pytest.mark.asyncio
async def test_runner_text_tool_calls_multiple_in_one_response(tmp_path: Path):
    """Text tool calls execute one per iteration ([:1] limit prevents spurious bulk calls).

    When a model emits multiple text-style calls in one response, only the first
    is executed; the model can emit the second in the next iteration.
    """
    from nanobot.agent.runner import AgentRunSpec, AgentRunner
    from nanobot.agent.tools.filesystem import ReadFileTool

    (tmp_path / "f1.txt").write_text("aaa", encoding="utf-8")
    (tmp_path / "f2.txt").write_text("bbb", encoding="utf-8")
    registry = _make_registry(ReadFileTool(workspace=tmp_path))

    call_count = {"n": 0}
    tool_results: list[str] = []

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        # Collect any tool results seen so far
        for msg in messages:
            if msg.get("role") == "tool":
                content = str(msg.get("content", ""))
                if content not in tool_results:
                    tool_results.append(content)
        if call_count["n"] == 1:
            # First response: two calls in one response — only first is taken
            return LLMResponse(
                content='read_file(path="f1.txt")\nread_file(path="f2.txt")',
                tool_calls=[],
                finish_reason="stop",
            )
        if call_count["n"] == 2:
            # After f1 was read, emit f2 (the model "continues" in next iteration)
            return LLMResponse(
                content='read_file(path="f2.txt")',
                tool_calls=[],
                finish_reason="stop",
            )
        return LLMResponse(content="both read", tool_calls=[], finish_reason="stop")

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "read both"}],
        tools=registry,
        model="test",
        max_iterations=5,
        max_tool_result_chars=16000,
        workspace=tmp_path,
    ))

    assert result.final_content == "both read"
    assert result.tools_used.count("read_file") == 2
    assert any("aaa" in r for r in tool_results)
    assert any("bbb" in r for r in tool_results)


@pytest.mark.asyncio
async def test_runner_native_tool_calls_not_double_parsed(tmp_path: Path):
    """Native structured tool calls are NOT re-parsed from text."""
    from nanobot.agent.runner import AgentRunSpec, AgentRunner
    from nanobot.agent.tools.filesystem import ReadFileTool

    (tmp_path / "test.txt").write_text("native", encoding="utf-8")
    registry = _make_registry(ReadFileTool(workspace=tmp_path))

    call_count = {"n": 0}

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Proper structured tool call AND text that looks like one
            return LLMResponse(
                content='I will read: read_file(path="ignored.txt")',
                tool_calls=[ToolCallRequest(
                    id="native01",
                    name="read_file",
                    arguments={"path": "test.txt"},
                )],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="native done", tool_calls=[], finish_reason="stop")

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "test"}],
        tools=registry,
        model="test",
        max_iterations=5,
        max_tool_result_chars=16000,
        workspace=tmp_path,
    ))

    assert result.final_content == "native done"
    # Only ONE read_file call (the native one), not two
    assert result.tools_used == ["read_file"]
