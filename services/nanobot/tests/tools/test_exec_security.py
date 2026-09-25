"""Where the SSRF boundary is, and where it deliberately is not.

This file used to assert that `exec` refuses a command containing a private
address. It no longer does, and the reason is worth keeping next to the tests
that replaced it, because "we removed a security check" is the kind of change
that gets re-added by whoever reads only the diff.

The check could not hold. `_guard_command` sees one string — the command line —
on a tool whose entire job is running arbitrary code. The same request written
to a file and executed went through untouched; so did every skill, because the
skill layer base64-encodes its own generated code before handing it to exec. It
never selected on where a request was going, only on whether the destination
happened to be spelled in the argv.

And it selected against the readable spelling, which is the part that cost
something. This stack shows the command in the turn — the `$ …` hint line a
person watching sees. A refusal on the one-liner and silence on the file means
the same call still happens and nobody can see where to. Measured: asked
whether a Home Assistant automation was wired correctly, the agent hit the
refusal, moved the request into a numbered probe script, and kept doing that;
seventeen of them accumulated in one workspace.

So the boundary is where the connection is actually opened, and only there:
`validate_url_target` in agent/tools/web.py, in the channels' media fetch and
in the document skill. Those cannot be walked around by writing a file, because
nanobot itself is the one making the request. Those are tested here too — the
point of removing the ineffective copy is that the effective ones matter more,
not less.
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from nanobot.agent.tools.shell import ExecTool
from nanobot.security.network import contains_internal_url, validate_url_target


def _fake_resolve_private(hostname, port, family=0, type_=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))]


def _fake_resolve_localhost(hostname, port, family=0, type_=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]


def _fake_resolve_public(hostname, port, family=0, type_=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]


# --- the boundary that holds --------------------------------------------------

def test_the_fetch_path_still_refuses_cloud_metadata():
    """169.254.169.254 is the credential endpoint every cloud exposes."""
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_private):
        ok, err = validate_url_target("http://metadata.internal/computeMetadata/v1/")
    assert not ok
    assert "169.254.169.254" in err


def test_the_fetch_path_still_refuses_loopback():
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_localhost):
        ok, _ = validate_url_target("http://localhost:8080/secret")
    assert not ok


def test_the_fetch_path_allows_the_public_internet():
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_public):
        ok, err = validate_url_target("https://example.com/api/data")
    assert ok and err == ""


# --- the guard that does not, and why -----------------------------------------

def test_exec_does_not_block_on_a_url_in_the_command():
    """Deliberate. See the module docstring.

    Kept as a test rather than left implicit: re-adding the check is a one-line
    change that looks like tightening security and is not, and this is what
    says so at the moment somebody makes it.
    """
    cmd = "curl -s http://169.254.169.254/computeMetadata/v1/"
    tool = ExecTool()
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_private):
        # Not vacuous: the condition the removed check tested is still true for
        # this input, so the `is None` below is the removal and nothing else.
        assert contains_internal_url(cmd)
        assert tool._guard_command(cmd, "/tmp") is None


def test_the_command_and_the_file_are_treated_alike(tmp_path):
    """The inconsistency this removed, pinned from both sides.

    A guard that refuses the first of these and permits the second is not
    protecting anything; it is choosing which of two identical requests is
    allowed to be legible.
    """
    script = tmp_path / "probe.py"
    script.write_text("import requests\nrequests.get('http://169.254.169.254/')\n")
    tool = ExecTool()
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_private):
        inline = tool._guard_command(
            "python3 -c \"import requests; requests.get('http://169.254.169.254/')\"", "/tmp")
        from_file = tool._guard_command(f"python3 {script}", "/tmp")
    assert inline == from_file is None


def test_a_household_service_is_reachable_from_exec():
    """What the removal is actually for.

    Every skill in this repo reaches a service on the household's own network,
    and the ones that do it in the open — rather than through the base64 the
    skill layer happens to apply — were the ones being refused.
    """
    tool = ExecTool()
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_private):
        assert tool._guard_command("curl -s http://portal:21001/chat/health", "/tmp") is None


# --- the guards that are unchanged --------------------------------------------

def test_dangerous_patterns_are_still_refused():
    tool = ExecTool(deny_patterns=[r"rm\s+-rf\s+/"])
    assert tool._guard_command("rm -rf /", "/tmp") == \
        "Error: Command blocked by safety guard (dangerous pattern detected)"


def test_an_allowlist_still_excludes_everything_else():
    tool = ExecTool(allow_patterns=[r"^git\b"])
    assert tool._guard_command("git status", "/tmp") is None
    assert tool._guard_command("curl https://example.com", "/tmp") == \
        "Error: Command blocked by safety guard (not in allowlist)"


@pytest.mark.asyncio
async def test_ordinary_commands_still_run():
    tool = ExecTool(timeout=5)
    result = await tool.execute(command="echo hello")
    assert "hello" in result
    assert "Error" not in result.split("\n")[0]
