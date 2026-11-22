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


def test_a_plan_upload_a_drop_into_a_room_and_its_removal(tmp_path):
    from test_floorplan import FLAT, dxf

    async def scenario(c):
        h = {**headers(), "Content-Type": "application/octet-stream"}
        bad = await c.post("/api/plans?name=Casa", data=b"not a drawing", headers=h)
        assert bad.status == 400 and (await bad.json())["code"] == "plan_unreadable"
        r = await c.post("/api/plans?name=Planta%20baja&source=casa.dxf", data=dxf(FLAT), headers=h)
        assert r.status == 201
        plan = await r.json()
        assert plan["id"] == "planta-baja" and sorted(r["name"] for r in plan["rooms"]) == ["Cocina", "Living"]
        listed = (await (await c.get("/api/plans", headers=headers())).json())["plans"]
        assert listed[0]["rooms"] and "lines" not in listed[0]
        # Dropped inside the kitchen: the device is in the kitchen now.
        r = await c.patch("/api/devices/oficina-tomi", json={"position": {"plan": "planta-baja", "x": 1, "y": 1}},
                          headers=headers())
        dev = (await r.json())["device"]
        assert dev["room"] == "Cocina" and dev["position"] == {"plan": "planta-baja", "x": 1.0, "y": 1.0}
        # A plan that does not exist is refused, not stored.
        r = await c.patch("/api/devices/oficina-tomi", json={"position": {"plan": "nope", "x": 1, "y": 1}},
                          headers=headers())
        assert r.status == 400
        assert (await c.patch("/api/plans/planta-baja", json={"name": "PB"}, headers=headers())).status == 200
        r = await c.delete("/api/plans/planta-baja", headers=headers())
        assert (await r.json())["unplaced"] == 1
    reg = run(tmp_path, scenario)
    dev = reg.devices["oficina-tomi"]
    assert dev["position"] is None and dev["room"] == "Cocina"
