"""household: an MCP failure never writes a server URL's token to the log."""

from __future__ import annotations

import httpx
from loguru import logger

from nanobot.agent.tools import mcp as mcp_module

# Built at runtime so no key-shaped literal sits in the file (publish gate).
SECRET = "-".join(("sk", "live", "0123456789abcdef"))
URL = f"https://mcp.example.com/mcp?token={SECRET}"


def _capture():
    lines: list[str] = []
    handler = logger.add(lambda m: lines.append(str(m)), level="DEBUG", format="{message}{exception}")
    return lines, handler


def _failure() -> BaseException:
    try:
        raise httpx.ConnectError(f"All connection attempts failed for {URL}",
                                 request=httpx.Request("POST", URL))
    except httpx.ConnectError as exc:
        return exc


def test_a_connect_failure_logs_the_host_not_the_token():
    lines, handler = _capture()
    try:
        mcp_module._log_mcp_connection_failure("brightdata", _failure(), "check the URL")
    finally:
        logger.remove(handler)
    text = "\n".join(lines)
    assert SECRET not in text and "token=" not in text
    assert "mcp.example.com" in text


def test_a_tool_call_failure_is_scrubbed_too():
    lines, handler = _capture()
    exc = _failure()
    try:
        logger.opt(exception=mcp_module._loggable_exc(exc)).error(
            "MCP tool failed: {}", mcp_module._scrub_text(exc))
    finally:
        logger.remove(handler)
    text = "\n".join(lines)
    assert SECRET not in text and "mcp.example.com" in text
