"""Thread-safe circular buffer of recent shell command executions for /v1/debug/shell-log."""

from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from typing import Any

_MAX_ENTRIES = 50
_MAX_OUTPUT = 2000
_entries: deque[dict[str, Any]] = deque(maxlen=_MAX_ENTRIES)
_lock = threading.Lock()

# Environment variables whose *values* must never appear in this buffer.
# Matched on the name, so a credential added later is covered without anyone
# remembering to come back here.
_SECRET_NAME_RE = re.compile(
    r"TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL|APIKEY", re.IGNORECASE
)
# Short values are not credentials and scrubbing them would corrupt ordinary
# output — a PORT of "80" or a flag of "1" would turn every 80 into [redacted].
_MIN_SECRET_LEN = 12


def _secrets() -> list[tuple[str, str]]:
    """(name, value) for every environment variable that looks like a secret.

    Read on each call rather than cached: the exec tool runs with a filtered
    environment that can change, and a stale snapshot would silently stop
    redacting the one credential that was added most recently.
    """
    found = []
    for name, value in os.environ.items():
        if not value or len(value) < _MIN_SECRET_LEN:
            continue
        if _SECRET_NAME_RE.search(name):
            found.append((name, value))
    # Longest first, so a token that contains another as a prefix is replaced
    # whole instead of being half-scrubbed into something still recognisable.
    found.sort(key=lambda pair: len(pair[1]), reverse=True)
    return found


def redact(text: str) -> str:
    """Replace any known credential value with a marker naming its variable.

    This buffer is served by /v1/debug/shell-log, which has no authentication,
    and it stores both the command and its output. A Paperless API token
    reached it once and every token in the house had to be rotated. Redacting
    here covers each way a secret can arrive — a value inlined into a command,
    an API response that echoes a token back, `env` printed by a confused turn —
    rather than only the one that happened to leak first.
    """
    if not text:
        return text
    for name, value in _secrets():
        if value in text:
            text = text.replace(value, f"[redacted:{name}]")
    return text


def log_exec(command: str, exit_code: int, stdout: str, stderr: str) -> None:
    output = stdout
    if stderr.strip():
        output += f"\nSTDERR:\n{stderr}"
    if len(output) > _MAX_OUTPUT:
        half = _MAX_OUTPUT // 2
        output = output[:half] + "\n... (truncated) ...\n" + output[-half:]
    # Redact after truncating: the cheaper order, and the marker is short enough
    # that it cannot push the entry back over the limit.
    record: dict[str, Any] = {
        "timestamp": time.time(),
        "command": redact(command[:1000]),
        "exit_code": exit_code,
        "output": redact(output),
    }
    with _lock:
        _entries.append(record)


def get_execs(*, limit: int = 20) -> list[dict[str, Any]]:
    with _lock:
        items = list(_entries)
    items.reverse()
    return items[:limit]


def clear_execs() -> int:
    with _lock:
        count = len(_entries)
        _entries.clear()
    return count
