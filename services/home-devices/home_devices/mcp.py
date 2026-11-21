"""The assistant's way to the devices: a Model Context Protocol server.

Streamable HTTP, answered statelessly with plain JSON -- every request carries
its own credentials, so a restart of this container costs the assistants
nothing, and there is no session to lose. Only what nanobot's MCP client uses:
``initialize``, notifications, ``ping``, ``tools/list`` and ``tools/call``.

Two tools, because every tool listed is in every prompt the assistant sends:
``find`` (what there is, where, in what state, able to do what) and ``set``
(do something to one device or to a room). Names the assistant passes back are
the ones ``find`` returned.

Who may call: a bearer token derived from the shared secret for this service
(``derive_service_token(secret, "home-devices")`` in the deployer), the same
in every assistant container -- the devices are the household's, not one
member's.
"""
from __future__ import annotations

import hmac
import json
import logging

from aiohttp import web

from . import control
from .places import Places

log = logging.getLogger("home-devices.mcp")

PROTOCOL = "2025-03-26"

TOOLS = [
    {
        "name": "find",
        "description": (
            "The house's devices: lights, plugs, switches, fans, covers, climate, sensors, TVs. "
            "Returns each device's name, room, kind, current state and the actions it accepts. "
            "Filter by room, kind, floor or text; with no filter, everything. Call this before `set` "
            "and use the exact names it returns. Where the house has floor plans, each device also "
            "says its `floor` and, when it is beside something labelled on the plan, `near` (e.g. "
            "'Ventana'), and the answer lists the `floors` and their rooms. For 'the lamp by the "
            "window' or 'the light closest to the TV', pass `near` (a label on the plan or a device "
            "name): devices come back nearest first, with `distance_m`. Also reports devices a scan "
            "found that nobody has accepted yet (`new_devices`): those cannot be controlled until "
            "someone accepts them on the Devices page."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text in the name, e.g. 'lamp'"},
                "room": {"type": "string"},
                "floor": {"type": "string", "description": "A floor's name, as `floors` lists them"},
                "near": {"type": "string", "description": "A label on the plan or a device name; sorts by distance"},
                "kind": {"type": "string", "enum": list(("light", "switch", "plug", "fan", "cover",
                                                         "climate", "lock", "media", "vacuum", "sensor",
                                                         "binary_sensor", "button", "camera", "other"))},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "set",
        "description": (
            "Do something to one device (`device`), or to every light in a room (`room`) or on a "
            "floor (`floor`, e.g. 'everything upstairs'; both together narrow it); add `kind` for "
            "something other than lights. Actions: on, off, toggle; brightness (value 0-100); "
            "color (rgb [r,g,b]); temperature (value in kelvin, 2000 warm - 6500 cold); position "
            "(covers, value 0-100); target_temperature (climate, value in degrees); identify (blinks "
            "it and leaves it as it was, to tell which lamp is which). Answers with the state "
            "each device ended in."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "device": {"type": "string", "description": "A name from `find`"},
                "room": {"type": "string"},
                "floor": {"type": "string"},
                "kind": {"type": "string"},
                "action": {"type": "string", "enum": list(control.ACTIONS)},
                "value": {"type": "number"},
                "rgb": {"type": "array", "items": {"type": "integer"}, "minItems": 3, "maxItems": 3},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
]


class McpServer:
    def __init__(self, app, token: str):
        self.app = app          # web.App: registry, ha, device_view
        self.token = token

    def authorized(self, request: web.Request) -> bool:
        given = request.headers.get("Authorization", "")
        return bool(self.token) and hmac.compare_digest(given, f"Bearer {self.token}")

    async def handle(self, request: web.Request) -> web.Response:
        if request.method != "POST":
            return web.Response(status=405, headers={"Allow": "POST"})
        if not self.authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            msg = await request.json()
        except ValueError:
            return web.json_response(_error(None, -32700, "parse error"), status=400)
        if not isinstance(msg, dict):
            return web.json_response(_error(None, -32600, "one request per POST"), status=400)
        method, msg_id = msg.get("method"), msg.get("id")
        if msg_id is None:                      # a notification: nothing to answer
            return web.Response(status=202)
        if method == "initialize":
            asked = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
            return web.json_response(_result(msg_id, {
                "protocolVersion": asked,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "home-devices", "version": "2"}}))
        if method == "ping":
            return web.json_response(_result(msg_id, {}))
        if method == "tools/list":
            return web.json_response(_result(msg_id, {"tools": TOOLS}))
        if method == "tools/call":
            params = msg.get("params") or {}
            try:
                out = await self.call(params.get("name"), params.get("arguments") or {})
                return web.json_response(_result(msg_id, _text(out)))
            except control.ActionError as exc:
                return web.json_response(_result(msg_id, {**_text({"error": str(exc)}), "isError": True}))
            except ConnectionError as exc:
                return web.json_response(_result(msg_id, {**_text({"error": str(exc)}), "isError": True}))
        return web.json_response(_error(msg_id, -32601, f"method not found: {method}"))

    # --- the tools --------------------------------------------------------

    def _views(self) -> list[dict]:
        return [self.app.device_view(d) for d in self.app.registry.devices.values()]

    def _places(self) -> Places:
        return Places(self.app.plans.docs())

    @staticmethod
    def _brief(d: dict, places: Places | None = None) -> dict:
        out = {"name": d["name"], "room": d["room"], "kind": d["kind"]}
        if places and places.plans:
            floor = places.floor_of(d)
            if floor:
                out["floor"] = floor
            mark = places.landmark_near(d)
            if mark:
                out["near"] = mark
        st = d.get("primary_state")
        if st:
            out["state"] = st["state"]
            attrs = st.get("attributes") or {}
            if "brightness" in attrs and attrs["brightness"] is not None:
                out["brightness"] = round(attrs["brightness"] * 100 / 255)
            for k in ("rgb_color", "color_temp_kelvin", "current_position", "temperature",
                      "current_temperature", "unit_of_measurement"):
                if attrs.get(k) is not None:
                    out[k] = attrs[k]
        if d.get("missing"):
            out["missing"] = True
        caps = control.capabilities(d)
        if caps:
            out["actions"] = caps
        if d.get("aliases"):
            out["aliases"] = d["aliases"]
        return out

    async def call(self, name: str, args: dict) -> dict:
        if name == "find":
            return self.find(**{k: args.get(k) for k in ("query", "room", "kind", "floor", "near")})
        if name == "set":
            return await self.set(**{k: args.get(k) for k in ("device", "room", "floor", "kind", "action",
                                                                "value", "rgb")})
        raise control.ActionError(f"no tool called {name}")

    def find(self, query=None, room=None, kind=None, floor=None, near=None) -> dict:
        views = self._views()
        places = self._places()
        active = [d for d in views if d["status"] == "active"]
        if floor:
            plan = places.find_floor(floor)
            active = [d for d in active if (places.plan_of(d) or {}).get("id") == plan["id"]]
        if room:
            active = [d for d in active if control.fold(d["room"]) == control.fold(room)]
        if kind:
            active = [d for d in active if d["kind"] == kind]
        if query:
            q = control.fold(query)
            active = [d for d in active if q in control.fold(d["name"]) or
                      any(q in control.fold(a) for a in d.get("aliases", []))]
        if near:
            matched, points = places.references(views, near)
            ranked = [(dist, d) for d in active if (dist := places.nearest(d, points)) is not None
                      and fold_name(d) != control.fold(matched)]
            ranked.sort(key=lambda p: p[0])
            out = {"near": matched,
                   "devices": [{**self._brief(d, places), "distance_m": round(dist, 1)} for dist, d in ranked]}
            unplaced = len(active) - len(ranked)
            if unplaced:
                out["not_on_a_plan"] = unplaced
        else:
            out = {"devices": [self._brief(d, places) for d in active]}
        if places.plans:
            out["floors"] = places.floors()
        new = [d["name"] for d in views if d["status"] == "new"]
        if new:
            out["new_devices"] = {"count": len(new), "names": new[:20],
                                  "note": "found by a scan; accept them on the Devices page to use them"}
        if not active and not (query or room or kind or floor or near):
            out["note"] = "No accepted devices yet: someone has to accept them on the Devices page."
        return out

    async def set(self, device=None, room=None, floor=None, kind=None, action=None, value=None, rgb=None) -> dict:
        views = self._views()
        if device:
            targets = [control.resolve(views, device)]
        elif room or floor:
            if room:
                targets = control.in_room(views, room, kind)
            else:
                targets = [d for d in views if d.get("status") == "active" and not d.get("missing")
                           and d.get("kind") in ((kind,) if kind else control.ROOM_DEFAULT_KINDS)]
            if floor:
                places = self._places()
                plan = places.find_floor(floor)
                targets = [d for d in targets if (places.plan_of(d) or {}).get("id") == plan["id"]]
            if not targets:
                where = " ".join(filter(None, [f"in {room}" if room else "", f"on {floor}" if floor else ""]))
                raise control.ActionError(f"no {kind or 'light'}s {where}")
        else:
            raise control.ActionError("name a device, a room or a floor")
        results = []
        for d in targets:
            try:
                results.append(await self.app.act(d, action, value, rgb))
            except control.ActionError as exc:
                results.append({"name": d["name"], "error": str(exc)})
        if all("error" in r for r in results):
            raise control.ActionError("; ".join(r["error"] for r in results))
        return {"done": results}


def fold_name(d: dict) -> str:
    return control.fold(d["name"])


def _result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _text(obj) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}]}
