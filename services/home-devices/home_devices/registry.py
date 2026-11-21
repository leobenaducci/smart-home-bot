"""The registry: every device the house has, and which backend reaches it.

A *device* is the household's: its name, its room, whether anybody has looked
at it yet. A *candidate* is a backend's: what Home Assistant (and, later, the
WiZ scan and MQTT) says exists right now. Discovery is the merge between the
two, and it runs on every scan:

* a candidate no device claims becomes a new device with status ``new`` --
  the page's inbox, and what Alfred is told about. Nothing is silently made
  ``active``: a scan that finds forty diagnostic entities must not hand the
  assistant forty switches.
* a candidate a device claims refreshes what the backend says about it
  (``discovered``) and its ``last_seen``. What the household typed -- name,
  room, aliases -- is never overwritten by a scan.
* a device whose route is missing from a *complete* scan of its backend is
  marked ``missing`` rather than deleted. A bulb unplugged for the summer
  keeps its name, room and place on the plan.

Stored as one JSON file, written atomically. The whole house is a few hundred
devices at most; a database would buy nothing but a migration story.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

STATUSES = ("new", "active", "ignored")

# What a device *is*, as the page and the assistant read it. A backend maps its
# own vocabulary onto these; anything it cannot place is `other`.
KINDS = ("light", "switch", "plug", "fan", "cover", "climate", "lock", "media",
         "vacuum", "sensor", "binary_sensor", "button", "camera", "other")


@dataclass
class Candidate:
    """One thing a backend says exists. ``key`` is stable across scans and
    unique across backends (``ha:device:<id>``, ``wiz:<mac>``)."""
    key: str
    backend: str
    name: str
    kind: str = "other"
    area: str = ""
    manufacturer: str = ""
    model: str = ""
    # The backend's own handles, e.g. Home Assistant entity ids.
    entities: list[dict] = field(default_factory=list)


def slugify(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    return slug[:48] or "device"


class Registry:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.doc = self._load()

    # --- storage ----------------------------------------------------------

    def _load(self) -> dict:
        try:
            doc = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"version": 1, "devices": {}}
        if not isinstance(doc, dict) or not isinstance(doc.get("devices"), dict):
            # Refuse rather than start empty: an empty registry saved over a
            # damaged one is every name and room the household typed, gone.
            raise ValueError(f"{self.path} is not a devices registry")
        return doc

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".devices-")
            with os.fdopen(fd, "w") as fh:
                json.dump(self.doc, fh, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(tmp, self.path)

    # --- reading ----------------------------------------------------------

    @property
    def devices(self) -> dict[str, dict]:
        return self.doc["devices"]

    def by_route(self) -> dict[str, str]:
        """Candidate key -> device id."""
        return {r["key"]: dev_id for dev_id, dev in self.devices.items()
                for r in dev.get("routes", [])}

    @staticmethod
    def display(dev: dict) -> dict:
        """What the page and the assistant show: the household's name and room
        where they typed one, the backend's otherwise."""
        found = dev.get("discovered") or {}
        return {**dev, "name": dev.get("name") or found.get("name") or dev["id"],
                "room": dev.get("room") or found.get("area") or ""}

    # --- discovery --------------------------------------------------------

    def _new_id(self, name: str) -> str:
        base = slugify(name)
        candidate, n = base, 2
        while candidate in self.devices:
            candidate, n = f"{base}-{n}", n + 1
        return candidate

    def observe(self, backend: str, candidates: list[Candidate], complete: bool = True,
                now: float | None = None) -> list[str]:
        """Merge one scan of ``backend``. Returns the ids of devices it created.

        ``complete`` says the scan listed everything the backend has; only then
        can absence mean missing. An incremental event says nothing about the
        devices it does not mention.
        """
        now = now or time.time()
        created: list[str] = []
        with self._lock:
            owner = self.by_route()
            seen: set[str] = set()
            for cand in candidates:
                seen.add(cand.key)
                found = {"name": cand.name, "kind": cand.kind, "area": cand.area,
                         "manufacturer": cand.manufacturer, "model": cand.model}
                dev_id = owner.get(cand.key)
                if dev_id is None:
                    dev_id = self._new_id(cand.name)
                    self.devices[dev_id] = {
                        "id": dev_id, "status": "new", "name": "", "room": "",
                        "kind": cand.kind, "aliases": [], "position": None,
                        "first_seen": now, "routes": [],
                    }
                    owner[cand.key] = dev_id
                    created.append(dev_id)
                dev = self.devices[dev_id]
                route = next((r for r in dev["routes"] if r["key"] == cand.key), None)
                if route is None:
                    route = {"key": cand.key, "backend": backend}
                    dev["routes"].append(route)
                route["entities"] = cand.entities
                route["last_seen"] = now
                route["missing"] = False
                dev["discovered"] = found
                dev["last_seen"] = now
                dev["missing"] = False
            if complete:
                for dev in self.devices.values():
                    routes = dev.get("routes", [])
                    for r in routes:
                        if r["backend"] == backend and r["key"] not in seen:
                            r["missing"] = True
                    dev["missing"] = bool(routes) and all(r.get("missing") for r in routes)
            self.save()
        return created

    # --- the household's edits -------------------------------------------

    EDITABLE = {"name", "room", "status", "kind", "aliases", "position"}

    def update(self, dev_id: str, changes: dict) -> dict:
        with self._lock:
            dev = self.devices.get(dev_id)
            if dev is None:
                raise KeyError(dev_id)
            unknown = set(changes) - self.EDITABLE
            if unknown:
                raise ValueError(f"not editable: {', '.join(sorted(unknown))}")
            if "status" in changes and changes["status"] not in STATUSES:
                raise ValueError(f"status is one of {', '.join(STATUSES)}")
            if "kind" in changes and changes["kind"] not in KINDS:
                raise ValueError(f"kind is one of {', '.join(KINDS)}")
            for key in ("name", "room"):
                if key in changes:
                    changes[key] = str(changes[key] or "").strip()[:80]
            if "aliases" in changes:
                raw = changes["aliases"]
                if not isinstance(raw, list):
                    raise ValueError("aliases is a list")
                changes["aliases"] = [str(a).strip()[:80] for a in raw if str(a).strip()][:20]
            if "position" in changes:
                pos = changes["position"]
                if pos is not None:
                    if not (isinstance(pos, dict) and isinstance(pos.get("plan"), str) and
                            all(isinstance(pos.get(k), (int, float)) and not isinstance(pos.get(k), bool)
                                for k in ("x", "y"))):
                        raise ValueError("position is null or {plan, x, y}")
                    changes["position"] = {"plan": pos["plan"], "x": float(pos["x"]), "y": float(pos["y"])}
            dev.update(changes)
            self.save()
            return dev

    def unplace(self, plan_id: str) -> int:
        """Take every device off a plan that is gone. They keep their room."""
        with self._lock:
            moved = [d for d in self.devices.values() if (d.get("position") or {}).get("plan") == plan_id]
            for dev in moved:
                dev["position"] = None
            if moved:
                self.save()
            return len(moved)

    def merge(self, src_id: str, dst_id: str) -> dict:
        """One device, two records: a WiZ bulb found on the LAN and the same
        bulb as Home Assistant knows it. ``src``'s routes move to ``dst`` and
        ``src`` is gone. What the household typed on ``dst`` stays; a blank
        there takes ``src``'s, and aliases and a position are kept from either.
        Later scans find each route already owned and leave it where it is."""
        with self._lock:
            if src_id == dst_id:
                raise ValueError("a device cannot be merged into itself")
            src, dst = self.devices.get(src_id), self.devices.get(dst_id)
            if src is None or dst is None:
                raise KeyError(src_id if src is None else dst_id)
            have = {r["key"] for r in dst["routes"]}
            dst["routes"] += [r for r in src["routes"] if r["key"] not in have]
            for key in ("name", "room"):
                if not dst.get(key) and src.get(key):
                    dst[key] = src[key]
            dst["aliases"] = list(dict.fromkeys(dst.get("aliases", []) + src.get("aliases", [])))
            if dst.get("position") is None:
                dst["position"] = src.get("position")
            if dst.get("status") != "active" and src.get("status") == "active":
                dst["status"] = "active"
            dst["missing"] = all(r.get("missing") for r in dst["routes"])
            del self.devices[src_id]
            self.save()
            return dst
