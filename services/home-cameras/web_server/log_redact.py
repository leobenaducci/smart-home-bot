"""Keep camera passwords out of the log.

A camera's stream address carries its login -- user and password, before the @ --
and it reached the log two ways: the connect line printed it, and PyAV's error
for a camera that does not answer quotes it back. On 2026-09-26 the wall's log
held one camera password 73 times in a day, for two cameras that were simply
offline.

Redacted where a record is *made*, not in a handler: several modules here call
logging.basicConfig and the first one imported wins, so a filter on "the"
handler would miss whichever records reached another -- and a third-party
logger quoting the address would be missed too. The message and any traceback
are rewritten; the user name stays, because "which login" is useful and not
secret.
"""
import logging
import re

_CREDENTIALS = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@'\"]+):[^/\s@'\"]+@")


def redact(text: str) -> str:
    """`<scheme>://admin:secret@10.0.0.5/x` -> `<scheme>://admin:***@10.0.0.5/x`."""
    return _CREDENTIALS.sub(r"\1:***@", text) if text else text


def install() -> None:
    """Redact every log record made in this process from now on. Idempotent."""
    base = logging.getLogRecordFactory()
    if getattr(base, "_redacts", False):
        return

    def factory(*args, **kwargs):
        record = base(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:                     # a malformed record is not ours to fix
            return record
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, None
        if record.exc_info:
            text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact(text)
        return record

    factory._redacts = True
    logging.setLogRecordFactory(factory)
