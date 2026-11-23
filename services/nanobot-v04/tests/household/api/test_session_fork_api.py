"""household: /v1/sessions/fork writes the branch through upstream's SQLite owner."""

from __future__ import annotations

import pytest
import pytest_asyncio
from agent.conftest import make_loop, make_provider
from aiohttp.test_utils import TestClient, TestServer

from nanobot.household.api_server import create_app

SRC = "homeweb:999000111:2026-11-17:1"
DST = "homeweb:999000111:2026-11-17:2"


@pytest_asyncio.fixture
async def client(tmp_path):
    loop = make_loop(tmp_path, provider=make_provider(default_model="m"))
    session = await loop.sessions.state.get(f"websocket:{SRC}")
    session.messages = [
        {"role": "user", "content": "What do we buy for tea?"},
        {"role": "assistant", "content": "Bread, avocado and tea."},
        {"role": "user", "content": "And for lunch?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "pasta"},
        {"role": "assistant", "content": "Pasta, there is sauce in the pantry."},
    ]
    session.last_consolidated = 2
    await loop.sessions.state.finish_turn(session)
    c = TestClient(TestServer(create_app(loop, model_name="m")))
    await c.start_server()
    yield c, loop
    await c.close()


def _body(anchor, role="user", target=DST):
    return {"channel": "websocket", "source_chat_id": SRC, "target_chat_id": target,
            "anchor_text": anchor, "anchor_role": role}


@pytest.mark.asyncio
async def test_a_question_forks_before_it_and_is_stored(client):
    c, loop = client
    resp = await c.post("/v1/sessions/fork", json=_body("And for lunch?"))
    assert resp.status == 200 and (await resp.json())["kept"] == 2
    target = await loop.sessions.state.read(f"websocket:{DST}")
    assert [m["content"] for m in target.messages] == ["What do we buy for tea?", "Bread, avocado and tea."]
    assert target.metadata["forked_from"] == f"websocket:{SRC}" and target.last_consolidated == 0
    source = await loop.sessions.state.read(f"websocket:{SRC}")
    assert len(source.messages) == 6  # untouched
    # Durable: a second manager on the same store reads the branch from SQLite.
    from nanobot.session.manager import SessionManager

    fresh = SessionManager(loop.sessions.workspace, sessions_root=loop.sessions.location.sessions_root
                           if hasattr(loop.sessions.location, "sessions_root") else None)
    reread = await fresh.state.read(f"websocket:{DST}")
    assert reread is not None and len(reread.messages) == 2
    await fresh.state.aclose()


@pytest.mark.asyncio
async def test_an_answer_forks_after_it(client):
    c, loop = client
    resp = await c.post("/v1/sessions/fork", json=_body("Pasta, there is sauce", role="assistant"))
    assert (await resp.json())["kept"] == 6


@pytest.mark.asyncio
async def test_a_live_target_and_a_missing_anchor_are_refused(client):
    c, _ = client
    assert (await c.post("/v1/sessions/fork", json=_body("And for lunch?"))).status == 200
    assert (await c.post("/v1/sessions/fork", json=_body("And for lunch?"))).status == 409
    assert (await c.post("/v1/sessions/fork", json=_body("never said", target=DST + "9"))).status == 404
    assert (await c.post("/v1/sessions/fork", json=_body("x", target=SRC))).status == 400
