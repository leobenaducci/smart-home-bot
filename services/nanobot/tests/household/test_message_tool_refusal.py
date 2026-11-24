"""household: the message tool refuses a skill block instead of sending it (C2)."""

import asyncio

from nanobot.agent.tools.message import MessageTool


def _tool(sent):
    async def send(msg):
        sent.append(msg)

    return MessageTool(send_callback=send, default_channel="websocket",
                       default_chat_id="homeweb:999000111:2026-11-17:1")


def test_a_skill_block_is_refused_not_sent():
    sent = []
    out = asyncio.run(_tool(sent).execute(content='{"skill":"geo","action":"list_places"}'))
    assert sent == []
    assert "message tool" in str(out)


def test_ordinary_messages_still_go_out():
    sent = []
    asyncio.run(_tool(sent).execute(content="Ya está la cena"))
    assert len(sent) == 1 and sent[0].content == "Ya está la cena"
