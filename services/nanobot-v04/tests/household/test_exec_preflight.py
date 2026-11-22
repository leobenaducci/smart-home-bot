"""Don't run a command that cannot parse — and say why.

Both cases are taken from production: the model reimplemented the camera skill
by hand and produced an unbalanced quote, then on a later turn a typo'd
re.sub call. Each cost a subprocess and came back as noise it had to guess at.
"""
import base64
import sys

import pytest

from nanobot.agent.tools.shell import ExecTool

# The bot only ever runs on Linux; these exercise the real bash gate.
requires_bash = pytest.mark.skipif(
    sys.platform == "win32", reason="production is Linux; bash -n is the gate"
)

# verbatim from /v1/debug/shell-log — note the stray " before the closing '
BROKEN_QUOTING = (
    'python -c "import base64; exec(base64.b64decode(b\'aW1wb3J0IG9z"\')'
    '.decode(\'utf-8\'))"'
)


def _tool(tmp_path):
    return ExecTool(working_dir=str(tmp_path), timeout=10)


# --- pure helpers: run everywhere, including the Windows dev box -----------


def test_extracts_inline_python_payload():
    assert ExecTool._python_payloads("python3 -c 'print(1)'") == ["print(1)"]


def test_extracts_base64_wrapped_payload():
    b64 = base64.b64encode(b"print(2)").decode()
    cmd = f"python3 -c \"import base64; exec(base64.b64decode(b'{b64}').decode('utf-8'))\""
    assert "print(2)" in ExecTool._python_payloads(cmd)


def test_no_payload_for_ordinary_commands():
    assert ExecTool._python_payloads("ls -la /tmp") == []
    assert ExecTool._python_payloads('echo "python -c not really"') == []


def test_python_checker_flags_the_observed_typo():
    bad = 'python3 -c \'slug = re.subrr["^[a-z0-9]+"], "-", label)\''
    err = ExecTool._python_syntax_error(bad)
    assert err and "syntax error" in err
    assert "line 1" in err


def test_python_checker_flags_a_bad_base64_payload():
    b64 = base64.b64encode(b"def broken(:\n    pass\n").decode()
    cmd = f"python3 -c \"import base64; exec(base64.b64decode(b'{b64}').decode('utf-8'))\""
    assert "syntax error" in (ExecTool._python_syntax_error(cmd) or "")


def test_python_checker_passes_valid_source():
    assert ExecTool._python_syntax_error("python3 -c 'print(1)'") is None


def test_python_checker_ignores_unparseable_shell():
    """bash -n owns that diagnosis; shlex failing here must not raise."""
    assert ExecTool._python_syntax_error("echo 'unterminated") is None


def test_error_points_at_the_skill_path():
    """The model hand-writes these instead of invoking the skill, so the error
    is the one place we can say so at the moment it matters."""
    bad = 'python3 -c \'print(1\''
    assert "JSON invocation block" in (ExecTool._python_syntax_error(bad) or "")


# --- end to end through execute(): needs the shell the bot actually uses ---


@requires_bash
@pytest.mark.asyncio
async def test_unbalanced_quote_is_rejected_before_running(tmp_path):
    result = await _tool(tmp_path).execute(command=BROKEN_QUOTING)

    assert result.startswith("Error: this is not valid shell")
    assert "Exit code" not in result  # never reached a subprocess


@requires_bash
@pytest.mark.asyncio
async def test_valid_command_still_runs(tmp_path):
    result = await _tool(tmp_path).execute(command="echo hola")

    assert "hola" in result
    assert "Exit code: 0" in result


@requires_bash
@pytest.mark.asyncio
async def test_valid_base64_wrapper_still_runs(tmp_path):
    """The skill translator's real output shape must pass untouched."""
    payload = base64.b64encode(b"print('ok from skill')\n").decode()
    cmd = f"python3 -c \"import base64; exec(base64.b64decode(b'{payload}').decode('utf-8'))\""

    result = await _tool(tmp_path).execute(command=cmd)

    assert "ok from skill" in result


@requires_bash
@pytest.mark.asyncio
async def test_shell_constructs_are_not_false_positives(tmp_path):
    """Pipes, subshells and awk braces are valid shell; rejecting them would be
    worse than the bug this guards."""
    cmd = """printf 'a b\\nc d\\n' | awk '{print $2}' | sort | head -1"""

    result = await _tool(tmp_path).execute(command=cmd)

    assert "Error: this is not valid shell" not in result
    assert "b" in result
