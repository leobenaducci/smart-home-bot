"""The house's own public address, learned from the house.

Opt-in (`cloud.vps.home_address_is_home`). When it is on, a request reaching the
public copy of this proxy from the address the house itself goes out on counts
as being at home -- which is what lets the Android app show the house-only apps
on the home wifi, where it still arrives here because it dials the public name.

The household chose this knowing what it means: anybody on the home wifi who
can sign in sees the house-only apps without the VPN. The two ways the idea
went wrong before are what this module is shaped around:

  * **The address changes.** Nothing here is configured. The copy at home
    reports in every few minutes, over the public internet, and the address it
    arrives from is the one recorded -- so an ISP renumbering the house is
    picked up on the next report. A recorded address that has not been
    confirmed for `TTL_S` stops counting, so a house that went quiet does not
    leave its old address trusted for whoever gets it next.
  * **Anybody could claim it.** A report is signed with the proxy secret both
    copies already hold, over a timestamp, and a timestamp is accepted once.
    The address comes from the last X-Forwarded-For hop (Caddy's), never from
    the report's body, and only a public address is recorded: a report that
    arrived from loopback or a private range is a misrouted one, and recording
    it would make that range the house.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import time

import httpx

logger = logging.getLogger("home-chat")

REPORT_PATH = "/_house/address"
# How often the house reports, and how long a report counts for. Three missed
# reports before the address stops counting: a redeploy of either side, or a
# minute of lost internet, should not blank the menu.
EVERY_S = 300
TTL_S = 20 * 60
# How far a report's clock may be from ours.
SKEW_S = 300


def enabled() -> bool:
    return os.environ.get("HOUSE_ADDRESS_IS_HOME", "0").strip().lower() not in ("", "0", "false", "no")


def _secret() -> bytes:
    return os.environ.get("PROXY_SHARED_SECRET", "").encode()


def sign(ts: int, secret: bytes | None = None) -> str:
    key = _secret() if secret is None else secret
    return hmac.new(key, f"house-address:{ts}".encode(), hashlib.sha256).hexdigest()


class Record:
    """The last address the house reported from, kept across restarts."""

    def __init__(self, path: str):
        self.path = path
        self.address = ""
        self.seen = 0.0
        self.last_ts = 0
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            self.address = str(doc.get("address") or "")
            self.seen = float(doc.get("seen") or 0)
            self.last_ts = int(doc.get("last_ts") or 0)
        except (OSError, ValueError, TypeError):
            pass

    def accept(self, ts: int, sig: str, address: str, now: float | None = None) -> str | None:
        """Record `address` if the report is genuine. Returns why not, or None."""
        now = time.time() if now is None else now
        if not _secret():
            return "no secret"
        if not hmac.compare_digest(sign(ts), str(sig or "")):
            return "bad signature"
        if abs(now - ts) > SKEW_S:
            return "stale"
        if ts <= self.last_ts:
            return "replayed"
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return "no address"
        if not ip.is_global:
            return "not a public address"
        changed = str(ip) != self.address
        self.address, self.seen, self.last_ts = str(ip), now, ts
        self._save()
        if changed:
            logger.info("house address is now %s", self.address)
        return None

    def matches(self, address: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if not self.address or now - self.seen > TTL_S:
            return False
        try:
            return ipaddress.ip_address(address) == ipaddress.ip_address(self.address)
        except ValueError:
            return False

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"address": self.address, "seen": self.seen,
                           "last_ts": self.last_ts}, f)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("could not keep the house address: %s", e)


async def report_forever(address: str, name: str) -> None:
    """The house's half: tell the public copy where the house is, every few
    minutes, for as long as this copy runs.

    Dials the VPS by its address rather than by name, because inside the house
    the name answers *this* proxy -- that is the point of the split horizon --
    and a report that reaches home says nothing about the internet. TLS is still
    checked, against the public name, sent as SNI and Host.
    """
    url = f"https://{address}{REPORT_PATH}"
    last_error = None
    async with httpx.AsyncClient(timeout=20.0) as client:
        while True:
            ts = int(time.time())
            try:
                r = await client.post(url, json={"ts": ts, "sig": sign(ts)},
                                      headers={"Host": name},
                                      extensions={"sni_hostname": name})
                error = None if r.status_code == 200 else f"HTTP {r.status_code}: {r.text[:120]}"
            except httpx.HTTPError as e:
                error = str(e) or e.__class__.__name__
            # Once per change, not every five minutes.
            if error != last_error:
                if error:
                    logger.warning("house address report failed: %s", error)
                else:
                    logger.info("house address reported to %s", name)
                last_error = error
            await asyncio.sleep(EVERY_S)
