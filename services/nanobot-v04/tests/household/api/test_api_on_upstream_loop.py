"""household: the HomeCore API server drives upstream's real AgentLoop."""

from __future__ import annotations

import asyncio

import json

import pytest
import pytest_asyncio
from agent.conftest import make_loop, make_provider
from aiohttp.test_utils import TestClient, TestServer

from nanobot.config.schema import Config
from nanobot.household.api_server import create_app
from nanobot.household.runtime import RUNTIME
from nanobot.providers.base import LLMResponse

@pytest_asyncio.fixture
async def aiohttp_client():
    clients: list[TestClient] = []

    async def _make(app):
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    yield _make
    for client in clients:
        await client.close()


LONG_CHAT_ID = "homeweb:999000111:2026-11-17:dsg:" + "1" * 40  # past upstream's 64-char rule


def _provider(text: str):
    provider = make_provider(default_model="test-model")
    provider.provider_name = "custom"
    seen: list[dict] = []

    async def chat(**kwargs):
        seen.append(kwargs)
        delta = kwargs.get("on_content_delta")
        if delta is not None:
            await delta(text)
        return LLMResponse(content=text, tool_calls=[])

    provider.chat_with_retry = chat
    provider.chat_stream_with_retry = chat
    return provider, seen


@pytest.mark.asyncio
async def test_streamed_turn_with_a_long_homeweb_chat_id(tmp_path, aiohttp_client):
    provider, seen = _provider("Hola Tomi")
    loop = make_loop(tmp_path, provider=provider)
    client = await aiohttp_client(create_app(loop, model_name="test-model"))
    resp = await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hola"}],
        "stream": True, "channel": "websocket", "chat_id": LONG_CHAT_ID,
    })
    assert resp.status == 200
    body = await resp.text()
    chunks = [json.loads(line[6:]) for line in body.splitlines()
              if line.startswith("data: {")]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c.get("choices"))
    assert text == "Hola Tomi"
    assert body.rstrip().endswith("data: [DONE]")
    # The terminator follows the final usage, not the end of the turn (the
    # answer is not held for work after it), so the save may land just after.
    for _ in range(100):
        session = loop.sessions.get_existing(f"websocket:{LONG_CHAT_ID}")
        if session is not None and session.messages and session.messages[-1]["content"] == "Hola Tomi":
            break
        await asyncio.sleep(0.02)
    assert session is not None and session.messages[-1]["content"] == "Hola Tomi"


@pytest.mark.asyncio
async def test_profile_routes_the_turn_to_the_role_model(tmp_path, aiohttp_client):
    provider, seen = _provider("ok")
    loop = make_loop(tmp_path, provider=provider)
    RUNTIME.config = Config.model_validate({"agents": {"defaults": {
        "model": "test-model", "provider": "custom",
        "modelProfiles": {"designer": "design-model"},
        "providerProfiles": {"designer": "custom"},
    }}})
    client = await aiohttp_client(create_app(loop, model_name="test-model"))
    resp = await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "un afiche"}], "profile": "designer",
    })
    assert resp.status == 200
    assert seen[-1]["model"] == "design-model"


@pytest.mark.asyncio
async def test_bearer_auth(tmp_path, aiohttp_client, monkeypatch):
    provider, _ = _provider("ok")
    loop = make_loop(tmp_path, provider=provider)
    monkeypatch.setenv("NANOBOT_API_SECRET", "s3cret")
    client = await aiohttp_client(create_app(loop, model_name="test-model"))
    denied = await client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]})
    assert denied.status == 401
    ok = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer s3cret"},
                           json={"messages": [{"role": "user", "content": "x"}]})
    assert ok.status == 200
