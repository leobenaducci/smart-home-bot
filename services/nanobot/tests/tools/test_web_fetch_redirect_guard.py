"""web_fetch must refuse a redirect to an internal address *before* requesting it.

The old code followed redirects and validated the final URL afterwards, so a
public page redirecting to http://127.0.0.1/ (or a container address) had
already been requested by the time it was refused. Upstream HKUDS/nanobot#4086.
"""

import json

import httpx
import pytest

import nanobot.security.network as net
from nanobot.agent.tools.web import WebFetchTool


@pytest.mark.asyncio
async def test_redirect_to_loopback_is_never_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:8080/admin"})
        return httpx.Response(200, text="internal secret")

    real_transport, real_resolve = net.PinnedDNSAsyncTransport, net.resolve_url_target

    class Fake(real_transport):
        def __init__(self, **kw):
            super().__init__(inner=httpx.MockTransport(handler), **kw)

    def resolve(url, **kw):
        host = httpx.URL(url).host
        if host == "public.example":
            return True, "", ("93.184.216.34",)
        return real_resolve(url, **kw)  # 127.0.0.1 is refused by the real check

    monkeypatch.setattr(net, "resolve_url_target", resolve)
    monkeypatch.setattr(net, "PinnedDNSAsyncTransport", Fake)

    tool = WebFetchTool()
    out = await tool._fetch_readability("http://public.example/", "markdown", 1000)

    assert requested == ["http://public.example/"], requested
    assert "internal secret" not in json.dumps(out)
