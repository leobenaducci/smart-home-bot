"""Trial port: a homeweb answer is handed to HomeCore whether or not anyone is attached."""

from pathlib import Path

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.outbound_events import ProgressEvent, StreamedResponseEvent
from nanobot.channels.homeweb_durable import homeweb_record

CHAT = "homeweb:999000111:2026-08-15:1786840580988"


def _msg(content="Done.", chat_id=CHAT, **kw):
    return OutboundMessage(channel="websocket", chat_id=chat_id, content=content, **kw)


def test_plain_answer_is_kept():
    assert homeweb_record(_msg(), None) == {"type": "message", "chat_id": CHAT, "text": "Done."}


def test_streamed_final_text_is_kept():
    rec = homeweb_record(_msg(event=StreamedResponseEvent()), None)
    assert rec and rec["text"] == "Done."


@pytest.mark.parametrize("ev", [ProgressEvent(content="x", tool_hint=True), ProgressEvent(content="thinking")])
def test_trace_is_never_kept(ev):
    assert homeweb_record(_msg(event=ev), None) is None


def test_other_chats_and_empty_are_left_alone():
    assert homeweb_record(_msg(chat_id="abc123"), None) is None
    assert homeweb_record(_msg(content="  "), None) is None


def test_reminder_keeps_its_buttons():
    rec = homeweb_record(_msg(metadata={"reminder_job": "j1", "reminder_name": "Water plants",
                                        "reminder_recurring": True}), None)
    assert rec["reminder"] == {"job": "j1", "name": "Water plants", "recurring": True}


def test_files_arrive_as_pictures_in_the_text(tmp_path: Path):
    pic = tmp_path / "out" / "drawing_1.png"
    pic.parent.mkdir()
    pic.write_bytes(b"png")
    rec = homeweb_record(_msg(content="Here", media=[str(pic)]), tmp_path)
    assert rec["text"].startswith("Here\n\n![drawing 1](media/")
    assert list((tmp_path / "media").iterdir())


def test_homeweb_chat_id_fits_upstream_webui_chat_id_rule():
    from nanobot.webui.session_identity import is_valid_webui_chat_id
    assert is_valid_webui_chat_id(CHAT)
    # a space-scoped id with a long scope can exceed upstream's 64-char cap
    long_scope = "homeweb:999000111:2026-08-15:programmer-long-scope-name:1786840580988"
    assert len(long_scope) > 64 and not is_valid_webui_chat_id(long_scope)
