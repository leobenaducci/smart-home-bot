import re

_HHMM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


def parse(text):
    m = _HHMM.fullmatch(text) if isinstance(text, str) else None
    if not m:
        raise ValueError(f"not a time: {text!r}")
    return int(m.group(1)) * 60 + int(m.group(2))


def fmt(minutes):
    if not 0 <= minutes < 1440:
        raise ValueError(f"not a time of day: {minutes}")
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
