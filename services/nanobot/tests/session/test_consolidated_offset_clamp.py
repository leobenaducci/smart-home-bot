"""A corrupt last_consolidated offset is reset instead of hiding history.

Ported from upstream HKUDS/nanobot#4169.
"""

import json
from pathlib import Path

from nanobot.session.manager import Session, SessionManager


def _session(count: int, last_consolidated: object) -> Session:
    msgs = [{"role": "user", "content": f"msg{i}"} for i in range(count)]
    return Session(key="chan:chat", messages=msgs, last_consolidated=last_consolidated)


def test_out_of_range_offset_is_reset():
    assert _session(10, 999).last_consolidated == 0
    assert _session(3, -5).last_consolidated == 0


def test_non_integer_offset_is_reset():
    for offset in ("999", None, 0.5, True):
        assert _session(3, offset).last_consolidated == 0


def test_valid_offset_is_preserved():
    session = _session(10, 4)
    assert session.last_consolidated == 4
    assert len(session.get_history()) == 6


def test_loaded_corrupt_offset_keeps_messages(tmp_path: Path):
    for name, offset in {"too-big": 999, "string": "999", "null": None, "bool": True}.items():
        manager = SessionManager(tmp_path / name)
        path = manager._get_session_path("chan:chat")
        path.parent.mkdir(parents=True, exist_ok=True)
        message = {"role": "user", "content": f"survived {name}"}
        path.write_text("\n".join([
            json.dumps({"_type": "metadata", "key": "chan:chat", "metadata": {},
                        "last_consolidated": offset}),
            json.dumps(message),
        ]) + "\n", encoding="utf-8")

        session = manager.get_or_create("chan:chat")
        assert session.last_consolidated == 0, name
        assert [m.get("content") for m in session.get_history(max_messages=10)] == [message["content"]], name
