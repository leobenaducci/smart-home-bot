"""household: MCP failures are logged with every URL in them reduced to its host.

Server URLs carry secrets (Bright Data's has ``?token=``) and an httpx error
repeats the whole URL in its message; the fork scrubbed every URL out of the
message and the traceback (fork tools/mcp.py ``_scrub``). v0.4.1 redacts only
the proxy URL on connect failures and logs tool-call failures with the live
exception.
"""

from __future__ import annotations

import re
import traceback
import urllib.parse

_URL_IN_TEXT = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+")


def redact_url(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url)
        hostname = parts.hostname or ""
        netloc = f"[{hostname}]" if ":" in hostname else hostname
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        path = "/..." if parts.path and parts.path != "/" else parts.path
        return urllib.parse.urlunsplit((parts.scheme, netloc, path, "", ""))
    except Exception:  # noqa: BLE001
        return "<redacted-url>"


def scrub(text: object) -> str:
    return _URL_IN_TEXT.sub(lambda m: redact_url(m.group(0)), str(text))


def loggable(exc: BaseException) -> Exception:
    """A stand-in carrying the scrubbed traceback text, never live frames."""
    details = "".join(traceback.TracebackException.from_exception(exc, capture_locals=False).format())
    return Exception("\n" + scrub(details).rstrip())
