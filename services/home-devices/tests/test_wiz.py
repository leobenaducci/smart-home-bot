"""WiZ: the protocol against a fake bulb on loopback, and the pure parts."""
import asyncio
import json

import pytest

from home_devices import control, wiz
from home_devices.registry import Candidate, Registry

MAC = "a0b1c2d3e4f5"


class FakeBulb(asyncio.DatagramProtocol):
    def __init__(self, module="ESP01_SHRGB_03"):
        self.pilot = {"mac": MAC, "state": False, "dimming": 60, "temp": 2700, "sceneId": 0}
        self.module = module
        self.sets = []

    def connection_made(self, transport):
        self.t = transport

    def datagram_received(self, data, addr):
        msg = json.loads(data)
        if msg["method"] == "getPilot":
            out = dict(self.pilot)
        elif msg["method"] == "getSystemConfig":
            out = {"mac": MAC, "moduleName": self.module}
        elif msg["method"] == "setPilot":
            self.sets.append(msg["params"])
            self.pilot.update(msg["params"])
            if "r" in msg["params"]:
                self.pilot.pop("temp", None)
            out = {"success": True}
        else:
            return
        self.t.sendto(json.dumps({"method": msg["method"], "result": out}).encode(), addr)


def with_bulb(scenario, module="ESP01_SHRGB_03"):
    async def go():
        loop = asyncio.get_running_loop()
        bulb = FakeBulb(module)
        transport, _ = await loop.create_datagram_endpoint(lambda: bulb, local_addr=("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        old, wiz.PORT = wiz.PORT, port
        found = []
        w = wiz.Wiz("127.0.0.1/32", on_scan=found.extend, broadcast="127.0.0.1")
        task = asyncio.create_task(w.run())
        try:
            for _ in range(100):
                if found:
                    break
                await asyncio.sleep(0.1)
            await scenario(w, bulb, found)
        finally:
            task.cancel()
            wiz.PORT = old
            transport.close()
    asyncio.run(go())


def test_a_bulb_is_found_by_mac_and_its_state_read():
    async def scenario(w, bulb, found):
        assert [c.key for c in found] == [f"wiz:{MAC}"]
        assert found[0].kind == "light" and found[0].model == "ESP01_SHRGB_03"
        st = w.state_of(MAC)
        assert st["state"] == "off" and st["attributes"]["brightness"] == 153
        assert st["attributes"]["color_temp_kelvin"] == 2700
    with_bulb(scenario)


def test_a_socket_is_a_plug():
    async def scenario(w, bulb, found):
        assert found[0].kind == "plug"
    with_bulb(scenario, module="ESP10_SOCKET_06")


def test_set_pilot_reaches_the_bulb_and_answers_with_its_new_state():
    async def scenario(w, bulb, found):
        st = await w.set_pilot(MAC, {"state": True, "dimming": 40})
        assert bulb.sets == [{"state": True, "dimming": 40}]
        assert st["state"] == "on" and st["attributes"]["brightness"] == 102
    with_bulb(scenario)


def test_an_unknown_device_is_a_connection_error():
    async def scenario(w, bulb, found):
        with pytest.raises(ConnectionError):
            await w.set_pilot("ffffffffffff", {"state": True})
    with_bulb(scenario)


def test_subnets_too_big_or_wrong_are_ignored():
    assert [str(n) for n in wiz.parse_subnets("192.168.100.0/24, nonsense, 10.0.0.0/8")] == ["192.168.100.0/24"]


# --- control over a WiZ route ----------------------------------------------

def merged_device():
    return {"id": "luz-paula", "name": "Luz Paula", "kind": "light", "status": "active", "room": "Living",
            "routes": [{"backend": "ha", "key": "ha:device:x",
                        "entities": [{"entity_id": "light.luz_paula", "domain": "light"}]},
                       {"backend": "wiz", "key": f"wiz:{MAC}", "entities": [{"mac": MAC, "domain": "light"}]}]}


def test_the_lan_route_is_preferred_and_the_cloud_one_is_the_fallback():
    dev = merged_device()
    assert control.preferred_route(dev)["backend"] == "wiz"
    dev["routes"][1]["missing"] = True
    assert control.preferred_route(dev)["backend"] == "ha"


def test_wiz_plans():
    dev = merged_device()
    assert control.plan_wiz(dev, "brightness", 5) == {"state": True, "dimming": 10}
    assert control.plan_wiz(dev, "brightness", 0) == {"state": False}
    assert control.plan_wiz(dev, "toggle", is_on=True) == {"state": False}
    assert control.plan_wiz(dev, "temperature", 9000) == {"state": True, "temp": 6500}
    assert control.plan_wiz(dev, "color", rgb=[0, 0, 255]) == {"state": True, "r": 0, "g": 0, "b": 255}


def test_restore_puts_back_what_was_there():
    assert control.wiz_restore({"state": False, "dimming": 50}) == {"state": False}
    assert control.wiz_restore({"state": True, "dimming": 50, "temp": 3000, "sceneId": 0}) == \
        {"state": True, "dimming": 50, "temp": 3000}
    assert control.wiz_restore({"state": True, "dimming": 80, "r": 255, "g": 0, "b": 0, "sceneId": 0}) == \
        {"state": True, "dimming": 80, "r": 255, "g": 0, "b": 0}
    assert control.wiz_restore({"state": True, "dimming": 70, "sceneId": 6, "speed": 100}) == \
        {"state": True, "dimming": 70, "sceneId": 6, "speed": 100}


# --- merging two records of one bulb -----------------------------------------

def test_merge_keeps_the_households_name_and_takes_the_lan_route(tmp_path):
    reg = Registry(tmp_path / "devices.json")
    reg.observe("ha", [Candidate(key="ha:device:x", backend="ha", name="Luz Paula", kind="light",
                                 entities=[{"entity_id": "light.luz_paula", "domain": "light"}])])
    reg.observe("wiz", [Candidate(key=f"wiz:{MAC}", backend="wiz", name="WiZ bulb E4F5", kind="light",
                                  entities=[{"mac": MAC, "domain": "light"}])])
    reg.update("luz-paula", {"room": "Living", "status": "active"})
    dev = reg.merge("wiz-bulb-e4f5", "luz-paula")
    assert [r["backend"] for r in dev["routes"]] == ["ha", "wiz"]
    assert set(reg.devices) == {"luz-paula"} and dev["room"] == "Living"
    # A later scan finds the route already owned: nothing new in the inbox.
    assert reg.observe("wiz", [Candidate(key=f"wiz:{MAC}", backend="wiz", name="WiZ bulb E4F5",
                                         kind="light", entities=[])]) == []
    with pytest.raises(ValueError):
        reg.merge("luz-paula", "luz-paula")
