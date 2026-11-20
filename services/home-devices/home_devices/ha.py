"""Home Assistant as a backend: what it knows exists, and the state it reports.

Over HA's WebSocket API, because that is the only interface that *pushes*.
The registries (areas, devices, entities) are read on connect and re-read
whenever HA announces a change to one of them -- which is how a device paired
in HA, or added by an integration such as Tuya's, shows up here within
seconds instead of at the next poll. A full re-read also runs every few
minutes regardless, so an event lost across a reconnect costs minutes, not a
device nobody ever sees.

The grouping is by HA *device*: a Zigbee plug is one device with a switch, a
power sensor and a link-quality sensor, and the household means the plug.
Entities with no device become candidates of their own. Configuration and
diagnostic entities, hidden ones and disabled ones are left out -- they are
HA's plumbing, not things in the house.
"""
from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from .registry import Candidate

log = logging.getLogger("home-devices.ha")

BACKEND = "ha"

# Domains that are things in a house, most specific first: a device's kind is
# the first of these any of its entities has. A device with only diagnostic
# sensors never reaches this list, because those entities are filtered first.
DOMAIN_KIND = (
    ("light", "light"), ("climate", "climate"), ("cover", "cover"), ("fan", "fan"),
    ("lock", "lock"), ("vacuum", "vacuum"), ("media_player", "media"),
    ("humidifier", "climate"), ("water_heater", "climate"), ("valve", "switch"),
    ("switch", "switch"), ("siren", "switch"), ("camera", "camera"),
    ("button", "button"), ("event", "button"),
    ("binary_sensor", "binary_sensor"), ("sensor", "sensor"),
)
DOMAINS = {d for d, _ in DOMAIN_KIND}

# Re-read everything this often even without an event.
RESCAN_SECONDS = 300
# Registry events arrive in bursts (pairing one device creates a dozen
# entities); wait for the burst to finish before re-reading.
DEBOUNCE_SECONDS = 3


def candidates_from(areas: list, devices: list, entities: list, states: list) -> list[Candidate]:
    """Pure: HA's three registries plus its states, as candidates."""
    area_name = {a["area_id"]: a.get("name") or a["area_id"] for a in areas}
    friendly = {s["entity_id"]: (s.get("attributes") or {}).get("friendly_name") for s in states}
    dev_by_id = {d["id"]: d for d in devices}

    grouped: dict[str, list[dict]] = {}
    loose: list[dict] = []
    for e in entities:
        eid = e.get("entity_id") or ""
        domain = eid.split(".", 1)[0]
        if domain not in DOMAINS:
            continue
        if e.get("disabled_by") or e.get("hidden_by") or e.get("entity_category"):
            continue
        if e.get("device_id") and e["device_id"] in dev_by_id:
            grouped.setdefault(e["device_id"], []).append(e)
        else:
            loose.append(e)

    def entity_view(e):
        eid = e["entity_id"]
        return {"entity_id": eid, "domain": eid.split(".", 1)[0],
                "name": e.get("name") or e.get("original_name") or friendly.get(eid) or eid}

    def kind_of(ents):
        domains = {e["entity_id"].split(".", 1)[0] for e in ents}
        return next((k for d, k in DOMAIN_KIND if d in domains), "other")

    out: list[Candidate] = []
    for dev_id, ents in grouped.items():
        d = dev_by_id[dev_id]
        # Integrations' own "service" devices (the weather, the sun, HA itself)
        # are not things in the house.
        if d.get("entry_type") == "service":
            continue
        area = d.get("area_id") or next((e.get("area_id") for e in ents if e.get("area_id")), None)
        name = d.get("name_by_user") or d.get("name") or entity_view(ents[0])["name"]
        out.append(Candidate(
            key=f"ha:device:{dev_id}", backend=BACKEND, name=name, kind=kind_of(ents),
            area=area_name.get(area, "") if area else "",
            manufacturer=d.get("manufacturer") or "", model=d.get("model") or "",
            entities=sorted((entity_view(e) for e in ents), key=lambda v: v["entity_id"])))
    for e in loose:
        view = entity_view(e)
        out.append(Candidate(
            key=f"ha:entity:{e['entity_id']}", backend=BACKEND, name=view["name"],
            kind=kind_of([e]), area=area_name.get(e.get("area_id"), "") if e.get("area_id") else "",
            entities=[view]))
    return out


class HomeAssistant:
    """One connection to HA, kept open; reconnects on its own."""

    def __init__(self, url: str, token: str, on_scan, on_state=None):
        self.url = url.rstrip("/")
        self.token = token
        self.on_scan = on_scan          # callable(list[Candidate]) -> None
        self.on_state = on_state        # callable(entity_id, state dict | None)
        self.states: dict[str, dict] = {}
        self.status = "starting" if token and url else "not configured"
        self.last_scan: float | None = None
        self.error = ""
        self._ws = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._rescan = asyncio.Event()

    # --- the socket -------------------------------------------------------

    async def _call(self, payload: dict) -> object:
        self._next_id += 1
        msg_id = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send_json({"id": msg_id, **payload})
        reply = await asyncio.wait_for(fut, 30)
        if not reply.get("success"):
            raise RuntimeError(f"{payload['type']}: {reply.get('error')}")
        return reply.get("result")

    async def _reader(self):
        async for msg in self._ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            data = msg.json()
            if data.get("type") == "result":
                fut = self._pending.pop(data.get("id"), None)
                if fut and not fut.done():
                    fut.set_result(data)
            elif data.get("type") == "event":
                self._event(data.get("event") or {})
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("Home Assistant closed the connection"))
        self._pending.clear()

    def _event(self, event: dict):
        kind = event.get("event_type")
        if kind == "state_changed":
            data = event.get("data") or {}
            eid, new = data.get("entity_id"), data.get("new_state")
            if eid:
                if new:
                    self.states[eid] = new
                else:
                    self.states.pop(eid, None)
                if self.on_state:
                    self.on_state(eid, new)
        elif kind in ("entity_registry_updated", "device_registry_updated", "area_registry_updated"):
            self._rescan.set()

    async def scan(self):
        areas = await self._call({"type": "config/area_registry/list"})
        devices = await self._call({"type": "config/device_registry/list"})
        entities = await self._call({"type": "config/entity_registry/list"})
        states = await self._call({"type": "get_states"})
        self.states = {s["entity_id"]: s for s in states}
        cands = candidates_from(areas, devices, entities, states)
        self.on_scan(cands)
        self.last_scan = time.time()
        log.info("scanned Home Assistant: %d candidates", len(cands))

    async def _session(self, http: aiohttp.ClientSession):
        ws_url = self.url.replace("http", "ws", 1) + "/api/websocket"
        async with http.ws_connect(ws_url, heartbeat=30, max_msg_size=64 * 1024 * 1024) as ws:
            hello = await ws.receive_json()
            if hello.get("type") != "auth_required":
                raise RuntimeError(f"unexpected greeting {hello.get('type')}")
            await ws.send_json({"type": "auth", "access_token": self.token})
            auth = await ws.receive_json()
            if auth.get("type") != "auth_ok":
                self.status = "token refused"
                raise PermissionError("Home Assistant refused the token")
            self._ws = ws
            reader = asyncio.create_task(self._reader())
            try:
                for ev in ("state_changed", "entity_registry_updated",
                           "device_registry_updated", "area_registry_updated"):
                    await self._call({"type": "subscribe_events", "event_type": ev})
                await self.scan()
                self.status, self.error = "connected", ""
                while not reader.done():
                    try:
                        await asyncio.wait_for(self._rescan.wait(), RESCAN_SECONDS)
                        await asyncio.sleep(DEBOUNCE_SECONDS)
                    except asyncio.TimeoutError:
                        pass
                    self._rescan.clear()
                    if not reader.done():
                        await self.scan()
            finally:
                reader.cancel()
                self._ws = None

    async def run(self):
        if self.status == "not configured":
            log.warning("Home Assistant not configured (no URL or token); backend off")
            return
        delay = 5
        async with aiohttp.ClientSession() as http:
            while True:
                try:
                    await self._session(http)
                    delay = 5
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - any failure: report, back off, retry
                    if self.status != "token refused":
                        self.status = "disconnected"
                    self.error = f"{type(exc).__name__}: {exc}"[:300]
                    log.warning("Home Assistant: %s; retrying in %ds", self.error, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    def state_of(self, entity_id: str) -> dict | None:
        s = self.states.get(entity_id)
        if not s:
            return None
        attrs = s.get("attributes") or {}
        keep = {k: attrs[k] for k in ("brightness", "rgb_color", "color_temp_kelvin",
                                      "unit_of_measurement", "device_class", "current_position",
                                      "temperature", "current_temperature", "hvac_action")
                if k in attrs}
        return {"state": s.get("state"), "attributes": keep, "changed": s.get("last_changed")}
