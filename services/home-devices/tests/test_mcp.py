"""The MCP endpoint, end to end against a fake Home Assistant."""
import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from home_devices.registry import Candidate, Registry
from home_devices.web import App

TOKEN = "mcp-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeHA:
    status = "connected"

    def __init__(self):
        self.states = {"light.oficina_tomi": {"state": "off", "attributes": {}, "last_updated": "t0"},
                       "binary_sensor.puerta": {"state": "off", "attributes": {}, "last_updated": "t0"}}
        self.calls = []

    async def call_service(self, domain, service, entity_id, data):
        self.calls.append((domain, service, entity_id, data))
        self.states[entity_id] = {"state": "on" if service == "turn_on" else "off",
                                  "attributes": {"brightness": 255 * data.get("brightness_pct", 100) // 100},
                                  "last_updated": f"t{len(self.calls)}"}

    async def wait_for_change(self, entity_id, before, timeout=2.0):
        return

    def state_of(self, entity_id):
        s = self.states.get(entity_id)
        return {"state": s["state"], "attributes": s["attributes"]} if s else None


def setup(tmp_path):
    reg = Registry(tmp_path / "devices.json")
    reg.observe("ha", [
        Candidate(key="ha:device:a", backend="ha", name="Oficina Tomi", kind="light", area="Oficina",
                  entities=[{"entity_id": "light.oficina_tomi", "domain": "light", "name": "x"}]),
        Candidate(key="ha:entity:binary_sensor.puerta", backend="ha", name="Puerta", kind="binary_sensor",
                  entities=[{"entity_id": "binary_sensor.puerta", "domain": "binary_sensor", "name": "Puerta"}]),
        Candidate(key="ha:device:c", backend="ha", name="Luz Paula", kind="light", area="Living",
                  entities=[{"entity_id": "light.luz_paula", "domain": "light", "name": "x"}]),
    ])
    reg.update("oficina-tomi", {"status": "active"})
    reg.update("puerta", {"status": "active"})
    ha = FakeHA()
    return reg, ha, App(reg, ha, "secret", mcp_token=TOKEN).build()


def rpc(method, params=None, msg_id=1):
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}


def run(tmp_path, scenario):
    reg, ha, app = setup(tmp_path)

    async def go():
        async with TestClient(TestServer(app)) as client:
            await scenario(client)
    asyncio.run(go())
    return ha


async def call(c, name, args):
    r = await c.post("/mcp", json=rpc("tools/call", {"name": name, "arguments": args}), headers=AUTH)
    res = (await r.json())["result"]
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def test_without_the_token_nothing_answers(tmp_path):
    async def scenario(c):
        assert (await c.post("/mcp", json=rpc("tools/list"))).status == 401
        assert (await c.post("/mcp", json=rpc("tools/list"), headers={"Authorization": "Bearer no"})).status == 401
    run(tmp_path, scenario)


def test_handshake_and_two_tools(tmp_path):
    async def scenario(c):
        init = await (await c.post("/mcp", json=rpc("initialize", {"protocolVersion": "2025-03-26"}),
                                   headers=AUTH)).json()
        assert init["result"]["serverInfo"]["name"] == "home-devices"
        note = await c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=AUTH)
        assert note.status == 202
        tools = (await (await c.post("/mcp", json=rpc("tools/list"), headers=AUTH)).json())["result"]["tools"]
        assert [t["name"] for t in tools] == ["find", "set"]
    run(tmp_path, scenario)


def test_find_lists_accepted_devices_and_mentions_new_ones(tmp_path):
    async def scenario(c):
        out, err = await call(c, "find", {})
        assert not err
        assert {d["name"] for d in out["devices"]} == {"Oficina Tomi", "Puerta"}
        lamp = next(d for d in out["devices"] if d["name"] == "Oficina Tomi")
        assert lamp["state"] == "off" and "brightness" in lamp["actions"]
        assert out["new_devices"]["names"] == ["Luz Paula"]
        out, _ = await call(c, "find", {"room": "oficina"})
        assert [d["name"] for d in out["devices"]] == ["Oficina Tomi"]
    run(tmp_path, scenario)


def test_set_switches_a_device_and_reports_where_it_ended(tmp_path):
    async def scenario(c):
        out, err = await call(c, "set", {"device": "oficina tomi", "action": "brightness", "value": 40})
        assert not err and out["done"] == [{"name": "Oficina Tomi", "room": "Oficina", "state": "on"}]
    ha = run(tmp_path, scenario)
    assert ha.calls == [("light", "turn_on", "light.oficina_tomi", {"brightness_pct": 40})]


def test_set_refusals_are_tool_errors_not_crashes(tmp_path):
    async def scenario(c):
        out, err = await call(c, "set", {"device": "puerta", "action": "on"})
        assert err and "only reports" in out["error"]
        out, err = await call(c, "set", {"device": "Luz Paula", "action": "on"})
        assert err and "no device" in out["error"]
    ha = run(tmp_path, scenario)
    assert ha.calls == []


def test_a_room_action_drives_its_lights(tmp_path):
    async def scenario(c):
        out, err = await call(c, "set", {"room": "Oficina", "action": "off"})
        assert not err and [r["name"] for r in out["done"]] == ["Oficina Tomi"]
    ha = run(tmp_path, scenario)
    assert ha.calls == [("light", "turn_off", "light.oficina_tomi", {})]


def run_with_plans(tmp_path, scenario):
    """Two floors: the flat downstairs (Living, Dormitorio, a window labelled
    on the plan) and a study upstairs. Oficina Tomi sits by the window, Luz
    Paula in the bedroom, Lámpara alta upstairs; Puerta is on no plan."""
    from home_devices.floorplan import PlanStore
    from test_floorplan import DRAWN, dxf, label, rect

    reg, ha, app = setup(tmp_path)
    store = PlanStore(tmp_path / "plans")
    store.save("Planta baja", dxf(DRAWN + "\n" + label(0.5, 3, "Ventana", kind="TEXT", h=0.15)))
    store.save("Planta alta", dxf(rect(0, 0, 4, 4) + "\n" + label(2, 2, "Estudio")))
    reg.observe("ha", [Candidate(key="ha:device:d", backend="ha", name="Lámpara alta", kind="light",
                                 entities=[{"entity_id": "light.lampara_alta", "domain": "light", "name": "x"}])],
                complete=False)
    reg.update("oficina-tomi", {"position": {"plan": "planta-baja", "x": 1, "y": 3}})
    reg.update("luz-paula", {"status": "active", "position": {"plan": "planta-baja", "x": 9, "y": 5}})
    reg.update("lampara-alta", {"status": "active", "room": "Estudio"})      # no position: floor from its room

    async def go():
        async with TestClient(TestServer(app)) as client:
            await scenario(client)
    asyncio.run(go())
    return ha


def test_find_says_which_floor_and_what_a_device_is_beside(tmp_path):
    async def scenario(c):
        out, err = await call(c, "find", {})
        assert not err
        assert out["floors"] == [{"name": "Planta baja", "rooms": ["Living", "Dormitorio"]},
                                 {"name": "Planta alta", "rooms": ["Estudio"]}]
        by = {d["name"]: d for d in out["devices"]}
        assert by["Oficina Tomi"]["floor"] == "Planta baja" and by["Oficina Tomi"]["near"] == "Ventana"
        assert by["Lámpara alta"]["floor"] == "Planta alta"
        assert "floor" not in by["Puerta"]
        out, _ = await call(c, "find", {"floor": "planta alta"})
        assert [d["name"] for d in out["devices"]] == ["Lámpara alta"]
        out, err = await call(c, "find", {"floor": "sótano"})
        assert err and "Planta baja" in out["error"]
    run_with_plans(tmp_path, scenario)


def test_near_ranks_by_distance_from_a_label_or_a_device(tmp_path):
    async def scenario(c):
        out, err = await call(c, "find", {"near": "ventana", "kind": "light"})
        assert not err and out["near"] == "Ventana"
        assert [(d["name"], d["distance_m"]) for d in out["devices"]] == [("Oficina Tomi", 0.5), ("Luz Paula", 8.7)]
        assert out["not_on_a_plan"] == 1                    # Lámpara alta has a room, not a spot
        out, _ = await call(c, "find", {"near": "Oficina Tomi"})
        assert [d["name"] for d in out["devices"]] == ["Luz Paula"]
        out, err = await call(c, "find", {"near": "piano"})
        assert err and "nothing on the plans" in out["error"]
    run_with_plans(tmp_path, scenario)


def test_set_on_a_floor_drives_that_floors_lights_only(tmp_path):
    async def scenario(c):
        out, err = await call(c, "set", {"floor": "Planta baja", "action": "off"})
        assert not err and sorted(r["name"] for r in out["done"]) == ["Luz Paula", "Oficina Tomi"]
    ha = run_with_plans(tmp_path, scenario)
    assert sorted(e for _d, _s, e, _x in ha.calls) == ["light.luz_paula", "light.oficina_tomi"]
