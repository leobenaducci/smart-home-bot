"""Thread-safe circular buffer of recent errors for the /v1/debug/errors endpoint."""

from __future__ import annotations

import threading
import traceback
import time
from collections import deque
from typing import Any

_MAX_ERRORS = 200
_errors: deque[dict[str, Any]] = deque(maxlen=_MAX_ERRORS)
_lock = threading.Lock()


def log_error(
    source: str,
    error: BaseException | str,
    *,
    session_key: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append an error record to the debug log."""
    if isinstance(error, BaseException):
        exc_type = type(error).__name__
        exc_msg = str(error)
        exc_tb = traceback.format_exception(type(error), error, error.__traceback__)
    else:
        exc_type = "Message"
        exc_msg = str(error)
        exc_tb = None

    record: dict[str, Any] = {
        "timestamp": time.time(),
        "source": source,
        "error_type": exc_type,
        "error_message": exc_msg,
    }
    if session_key:
        record["session_key"] = session_key
    if exc_tb:
        record["traceback"] = "".join(exc_tb)
    if extra:
        record.update(extra)

    with _lock:
        _errors.append(record)


def get_errors(
    *, limit: int = 50, source: str | None = None
) -> list[dict[str, Any]]:
    """Return the most recent errors, newest first."""
    with _lock:
        items = list(_errors)
    if source:
        items = [e for e in items if e.get("source") == source]
    items.reverse()
    return items[:limit]


def clear_errors() -> int:
    """Clear all stored errors. Returns the count of cleared records."""
    with _lock:
        count = len(_errors)
        _errors.clear()
    return count