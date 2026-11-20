"""The page and its API, behind the portal's mount.

The portal carries ``/home-devices/`` to this server signed in as the member,
adding the trusted-proxy pair: ``X-Proxy-User`` (the login id) and
``X-Proxy-Secret`` (sha256 of ``PROXY_SHARED_SECRET:<login>``). Everything but
``/healthz`` requires that pair, so the port answering on the LAN gives nothing
to a caller that is not the portal.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
from pathlib import Path

from aiohttp import web

from . import control
from .ha import HomeAssistant
from .mcp import McpServer
from .registry import KINDS, STATUSES, Registry

log = logging.getLogger("home-devices")
STATIC = Path(__file__).parent / "static"


def proxy_user(request: web.Request, secret: str) -> str | None:
    user = request.headers.get("X-Proxy-User", "")
    given = request.headers.get("X-Proxy-Secret", "")
    if not (secret and user and given):
        return None
    want = hashlib.sha256(f"{secret}:{user}".encode()).hexdigest()
    return user if hmac.compare_digest(want, given) else None


def member_locales(raw: str) -> dict[str, str]:
    out = {}
    for pair in raw.split(","):
        login, sep, loc = pair.partition(":")
        if sep and login.strip() and loc.strip():
            out[login.strip()] = loc.strip()
    return out


class App:
    def __init__(self, registry: Registry, ha: HomeAssistant | None, secret: str,
                 default_locale: str = "en", locales: dict | None = None, mcp_token: str = ""):
        self.registry = registry
        self.ha = ha
        self.secret = secret
        self.default_locale = default_locale
        self.locales = locales or {}
        self.started = time.time()
        self.mcp = McpServer(self, mcp_token)

    # --- helpers ----------------------------------------------------------

    def device_view(self, dev: dict) -> dict:
        view = Registry.display(dev)
        states = {}
        if self.ha:
            for route in dev.get("routes", []):
                if route["backend"] == "ha":
                    for e in route.get("entities", []):
                        st = self.ha.state_of(e["entity_id"])
                        if st:
                            states[e["entity_id"]] = st
        view["states"] = states
        primary = control.control_entity(view)
        view["primary_state"] = states.get(primary[0]) if primary else next(iter(states.values()), None)
        view["actions"] = control.capabilities(view)
        return view

    async def act(self, view: dict, action: str, value=None, rgb=None) -> dict:
        """Do one action on one device and answer with the state it ended in."""
        domain, service, entity, data = control.plan(view, action, value, rgb)
        if not self.ha:
            raise ConnectionError("Home Assistant is off")
        before = (self.ha.states.get(entity) or {}).get("last_updated")
        await self.ha.call_service(domain, service, entity, data)
        await self.ha.wait_for_change(entity, before)
        log.info("%s %s -> %s.%s %s", view["id"], action, domain, service, data or "")
        after = self.device_view(self.registry.devices[view["id"]])
        return {"name": after["name"], "room": after["room"],
                "state": (after.get("primary_state") or {}).get("state")}

    # --- handlers ---------------------------------------------------------

    @web.middleware
    async def auth(self, request: web.Request, handler):
        if request.path in ("/healthz", "/mcp"):  # /mcp checks its own token
            return await handler(request)
        user = proxy_user(request, self.secret)
        if not user:
            return web.json_response({"error": "sign in through the portal"}, status=401)
        request["user"] = user
        return await handler(request)

    async def healthz(self, request):
        ha = {"status": self.ha.status, "last_scan": self.ha.last_scan,
              "error": self.ha.error} if self.ha else {"status": "off"}
        return web.json_response({"status": "ok", "service": "home-devices",
                                  "devices": len(self.registry.devices), "backends": {"ha": ha}})

    async def index(self, request):
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def config(self, request):
        return web.json_response({
            "locale": self.locales.get(request["user"], self.default_locale),
            "statuses": STATUSES, "kinds": KINDS,
            "backends": {"ha": self.ha.status if self.ha else "off"}})

    async def list_devices(self, request):
        devs = [self.device_view(d) for d in self.registry.devices.values()]
        devs.sort(key=lambda d: (d["room"].lower() or "~", d["name"].lower()))
        return web.json_response({"devices": devs})

    async def update_device(self, request):
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": "JSON body expected"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "JSON object expected"}, status=400)
        try:
            dev = self.registry.update(request.match_info["dev_id"], body)
        except KeyError:
            return web.json_response({"error": "no such device"}, status=404)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"device": self.device_view(dev)})

    async def device_action(self, request):
        dev = self.registry.devices.get(request.match_info["dev_id"])
        if dev is None:
            return web.json_response({"error": "no such device"}, status=404)
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": "JSON body expected"}, status=400)
        view = self.device_view(dev)
        if view["status"] != "active":
            return web.json_response({"error": "accept the device first"}, status=409)
        try:
            await self.act(view, body.get("action"), body.get("value"), body.get("rgb"))
        except control.ActionError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except ConnectionError as exc:
            return web.json_response({"error": str(exc)}, status=503)
        return web.json_response({"device": self.device_view(self.registry.devices[dev["id"]])})

    def build(self) -> web.Application:
        app = web.Application(middlewares=[self.auth])
        app.add_routes([
            web.get("/healthz", self.healthz),
            web.get("/", self.index),
            web.get("/api/config", self.config),
            web.get("/api/devices", self.list_devices),
            web.patch("/api/devices/{dev_id}", self.update_device),
            web.post("/api/devices/{dev_id}/action", self.device_action),
            web.route("*", "/mcp", self.mcp.handle),
            web.static("/static", STATIC),
        ])
        return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    registry = Registry(Path(os.environ.get("HOME_DEVICES_DATA", "/data")) / "devices.json")
    ha = HomeAssistant(os.environ.get("HOMEASSISTANT_URL", ""), os.environ.get("HOMEASSISTANT_TOKEN", ""),
                       on_scan=lambda cands: _announce(registry.observe("ha", cands)))
    app = App(registry, ha, os.environ.get("PROXY_SHARED_SECRET", ""),
              os.environ.get("HOME_STACK_DEFAULT_LOCALE", "en"),
              member_locales(os.environ.get("MEMBER_LOCALES", "")),
              os.environ.get("HOME_DEVICES_MCP_TOKEN", ""))

    async def start_backends(_app):
        _app["ha_task"] = asyncio.create_task(ha.run())

    async def stop_backends(_app):
        _app["ha_task"].cancel()

    webapp = app.build()
    webapp.on_startup.append(start_backends)
    webapp.on_cleanup.append(stop_backends)
    web.run_app(webapp, host="0.0.0.0", port=int(os.environ.get("HOME_DEVICES_PORT", "8080")),
                access_log=None)


def _announce(created: list[str]):
    if created:
        log.info("new devices: %s", ", ".join(created))
