"""The API behind the portal: the proxy pair is required, edits round-trip."""
import asyncio
import hashlib

from aiohttp.test_utils import TestClient, TestServer

from home_devices.registry import Candidate, Registry
from home_devices.web import App

SECRET = "test-secret"


def headers(user="999000111", secret=SECRET):
    return {"X-Proxy-User": user, "X-Proxy-Secret": hashlib.sha256(f"{secret}:{user}".encode()).hexdigest()}


def run(tmp_path, scenario):
    reg = Registry(tmp_path / "devices.json")
    reg.observe("ha", [Candidate(key="ha:device:a1", backend="ha", name="Oficina Tomi", kind="light")])
    app = App(reg, None, SECRET, "en", {"999000111": "es"}).build()

    async def go():
        async with TestClient(TestServer(app)) as client:
            await scenario(client)
    asyncio.run(go())
    return reg


def test_healthz_is_open_and_names_the_service(tmp_path):
    async def scenario(c):
        r = await c.get("/healthz")
        assert (await r.json())["service"] == "home-devices"
    run(tmp_path, scenario)


def test_everything_else_needs_the_portals_pair(tmp_path):
    async def scenario(c):
        assert (await c.get("/api/devices")).status == 401
        assert (await c.get("/api/devices", headers=headers(secret="wrong"))).status == 401
        r = await c.get("/api/devices", headers=headers())
        assert [d["name"] for d in (await r.json())["devices"]] == ["Oficina Tomi"]
    run(tmp_path, scenario)


def test_the_members_locale_is_served(tmp_path):
    async def scenario(c):
        assert (await (await c.get("/api/config", headers=headers())).json())["locale"] == "es"
        other = await c.get("/api/config", headers=headers("999000222"))
        assert (await other.json())["locale"] == "en"
    run(tmp_path, scenario)


def test_an_edit_round_trips_and_a_bad_one_is_refused(tmp_path):
    async def scenario(c):
        r = await c.patch("/api/devices/oficina-tomi", json={"status": "active", "room": "Oficina"},
                          headers=headers())
        assert (await r.json())["device"]["room"] == "Oficina"
        assert (await c.patch("/api/devices/oficina-tomi", json={"status": "gone"}, headers=headers())).status == 400
        assert (await c.patch("/api/devices/nope", json={}, headers=headers())).status == 404
    reg = run(tmp_path, scenario)
    assert reg.devices["oficina-tomi"]["status"] == "active"
