"""WiZ bulbs and sockets, spoken to directly over the house network.

Why this exists beside Home Assistant: a WiZ bulb HA reaches through a cloud
integration reports state that can be days old, and a command takes seconds
to land. On the LAN the bulb answers in milliseconds and its answer is the
truth. So when a device has both routes, this one drives it and this one's
state is shown.

The protocol is JSON over UDP port 38899, one datagram each way:
``getPilot`` (state, dimming, colour), ``setPilot`` (change them) and
``getSystemConfig`` (MAC, module name). A bulb is identified by its MAC --
addresses move with DHCP, so every scan re-learns where each MAC is.

Discovery runs on a timer: a broadcast, plus -- for subnets a broadcast
cannot reach (a second network behind a router) -- a unicast sweep of every
host in ``HOME_DEVICES_WIZ_SUBNETS``. Known bulbs are polled every few
seconds, so state stays live without anyone asking.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time

from .registry import Candidate

log = logging.getLogger("home-devices.wiz")

BACKEND = "wiz"
PORT = 38899
SCAN_SECONDS = 120
POLL_SECONDS = 5
# A bulb not heard from in this long is missing from the next complete scan.
FORGET_SECONDS = 600
MAX_SWEEP_HOSTS = 1024


def parse_subnets(raw: str) -> list[ipaddress.IPv4Network]:
    out = []
    for part in (raw or "").replace(" ", ",").split(","):
        if not part:
            continue
        try:
            net = ipaddress.ip_network(part, strict=False)
        except ValueError:
            log.warning("ignoring WiZ subnet %r: not a network", part)
            continue
        if net.num_addresses > MAX_SWEEP_HOSTS:
            log.warning("ignoring WiZ subnet %s: larger than %d hosts", net, MAX_SWEEP_HOSTS)
            continue
        out.append(net)
    return out


def pilot_state(pilot: dict) -> dict:
    """A bulb's getPilot result, in the shape the rest of the service reads
    (Home Assistant's: brightness 0-255, rgb_color, color_temp_kelvin)."""
    attrs = {}
    if pilot.get("dimming") is not None:
        attrs["brightness"] = round(int(pilot["dimming"]) * 255 / 100)
    if pilot.get("temp"):
        attrs["color_temp_kelvin"] = int(pilot["temp"])
    elif all(pilot.get(c) is not None for c in ("r", "g", "b")) and any(pilot.get(c) for c in ("r", "g", "b")):
        attrs["rgb_color"] = [int(pilot["r"]), int(pilot["g"]), int(pilot["b"])]
    return {"state": "on" if pilot.get("state") else "off", "attributes": attrs}


def candidate_for(mac: str, info: dict) -> Candidate:
    module = (info.get("module") or "").upper()
    kind = "plug" if "SOCKET" in module else "light"
    label = "WiZ socket" if kind == "plug" else "WiZ bulb"
    return Candidate(key=f"wiz:{mac}", backend=BACKEND, name=f"{label} {mac[-4:].upper()}",
                     kind=kind, manufacturer="WiZ", model=info.get("module") or "",
                     entities=[{"mac": mac, "ip": info.get("ip", ""), "domain": kind}])


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, owner: "Wiz"):
        self.owner = owner

    def datagram_received(self, data, addr):
        try:
            msg = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            return
        if isinstance(msg, dict):
            self.owner._reply(addr[0], msg)


class Wiz:
    def __init__(self, subnets: str, on_scan, broadcast: str = "255.255.255.255"):
        self.subnets = parse_subnets(subnets)
        self.broadcast = broadcast
        self.on_scan = on_scan            # callable(list[Candidate]) -> None
        self.bulbs: dict[str, dict] = {}  # mac -> {ip, module, pilot, seen}
        self.status = "starting"
        self.last_scan: float | None = None
        self.error = ""
        self._transport = None
        self._waiters: dict[tuple[str, str], list[asyncio.Future]] = {}

    # --- the socket -------------------------------------------------------

    def _reply(self, ip: str, msg: dict):
        method, result = msg.get("method"), msg.get("result") or {}
        mac = (result.get("mac") or "").lower()
        if mac:
            bulb = self.bulbs.setdefault(mac, {})
            bulb["ip"], bulb["seen"] = ip, time.time()
            if method == "getPilot":
                bulb["pilot"] = result
            elif method == "getSystemConfig":
                bulb["module"] = result.get("moduleName") or bulb.get("module", "")
        for fut in self._waiters.pop((ip, method), []):
            if not fut.done():
                fut.set_result(msg)

    def _send(self, ip: str, method: str, params: dict | None = None):
        payload = json.dumps({"method": method, "params": params or {}}).encode()
        self._transport.sendto(payload, (ip, PORT))

    async def request(self, ip: str, method: str, params: dict | None = None,
                      tries: int = 3, timeout: float = 0.7) -> dict:
        """One request, retried: UDP drops, and a bulb busy fading drops more."""
        for _ in range(tries):
            fut = asyncio.get_running_loop().create_future()
            self._waiters.setdefault((ip, method), []).append(fut)
            self._send(ip, method, params)
            try:
                msg = await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                continue
            if "error" in msg:
                raise ValueError(f"bulb {ip} refused {method}: {msg['error']}")
            return msg
        raise ConnectionError(f"WiZ device at {ip} did not answer")

    # --- discovery and polling -------------------------------------------

    async def scan(self):
        self._send(self.broadcast, "getPilot")
        for net in self.subnets:
            for host in net.hosts():
                self._send(str(host), "getPilot")
            await asyncio.sleep(0)
        await asyncio.sleep(3)
        # Module names, once per bulb: a socket is a plug, not a light.
        for mac, bulb in list(self.bulbs.items()):
            if "module" not in bulb and bulb.get("ip"):
                try:
                    await self.request(bulb["ip"], "getSystemConfig", tries=2)
                except (ConnectionError, ValueError):
                    pass
        now = time.time()
        live = {m: b for m, b in self.bulbs.items() if now - b.get("seen", 0) < FORGET_SECONDS}
        self.on_scan([candidate_for(m, b) for m, b in live.items()])
        self.last_scan = now
        log.info("scanned WiZ: %d device(s)", len(live))

    async def poll(self):
        async def one(bulb):
            try:
                await self.request(bulb["ip"], "getPilot", tries=1, timeout=1.0)
            except (ConnectionError, ValueError):
                pass
        await asyncio.gather(*(one(b) for b in list(self.bulbs.values()) if b.get("ip")))

    async def run(self):
        loop = asyncio.get_running_loop()
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _Protocol(self), local_addr=("0.0.0.0", 0), allow_broadcast=True)
        except OSError as exc:
            self.status, self.error = "off", f"no UDP socket: {exc}"
            log.warning("WiZ backend off: %s", self.error)
            return
        self.status = "running"
        next_scan = 0.0
        while True:
            try:
                if time.monotonic() >= next_scan:
                    await self.scan()
                    next_scan = time.monotonic() + SCAN_SECONDS
                await self.poll()
                self.error = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - report and keep scanning
                self.error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("WiZ: %s", self.error)
            await asyncio.sleep(POLL_SECONDS)

    # --- for the rest of the service -------------------------------------

    def state_of(self, mac: str) -> dict | None:
        bulb = self.bulbs.get(mac)
        if not bulb or "pilot" not in bulb or time.time() - bulb.get("seen", 0) > FORGET_SECONDS:
            return None
        return {**pilot_state(bulb["pilot"]), "changed": bulb.get("seen")}

    async def set_pilot(self, mac: str, params: dict) -> dict | None:
        bulb = self.bulbs.get(mac)
        if not bulb or not bulb.get("ip"):
            raise ConnectionError("that WiZ device has not been seen on the network")
        await self.request(bulb["ip"], "setPilot", params)
        try:
            await self.request(bulb["ip"], "getPilot", tries=2)
        except (ConnectionError, ValueError):
            pass
        return self.state_of(mac)
