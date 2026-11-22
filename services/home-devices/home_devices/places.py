"""Where things are: floors, landmarks and distances, for the assistant.

The plans give three kinds of answer the room alone cannot:

* **floor** -- "everything upstairs". A floor is a plan, named by the
  household ("Planta alta"). A device placed on a plan is on that floor; one
  with a room and no position is on the floor whose plan has that room, when
  exactly one does.
* **landmarks** -- "the lamp by the window". Any label written on a plan
  ("Ventana", "Cama", "Sofá") is a landmark, and so is every room's label. A
  placed device is described by the nearest one within ``NEAR_M``.
* **distance** -- "the light closest to the TV". Between two points on the
  same plan, in metres, whatever unit the drawing is in.
"""
from __future__ import annotations

import math

from .control import ActionError, fold
from .floorplan import _room_name

NEAR_M = 1.5
UNIT_M = {"mm": 0.001, "cm": 0.01, "m": 1.0, "in": 0.0254, "ft": 0.3048}


def metres_per_unit(plan: dict) -> float:
    if plan.get("units") in UNIT_M:
        return UNIT_M[plan["units"]]
    # Unitless: a house is 5-40 m across. Thousands of units is millimetres.
    x0, y0, x1, y1 = plan.get("bounds") or (0, 0, 1, 1)
    span = max(x1 - x0, y1 - y0)
    return 0.001 if span > 500 else 0.01 if span > 60 else 1.0


class Places:
    """One snapshot of every plan, built per request (plans are cached on
    disk reads by the store, so this is cheap)."""

    def __init__(self, plans: list[dict]):
        self.plans = {p["id"]: p for p in plans}
        self.landmarks: dict[str, list[dict]] = {}
        for p in plans:
            rooms = {fold(r["name"]) for r in p.get("rooms", [])}
            marks = []
            for t in p.get("texts", []):
                name = _room_name(t["text"])
                if name:
                    marks.append({"name": name, "x": t["x"], "y": t["y"], "room": fold(name) in rooms})
            self.landmarks[p["id"]] = marks

    def floors(self) -> list[dict]:
        return [{"name": p["name"], "rooms": [r["name"] for r in p.get("rooms", [])]}
                for p in sorted(self.plans.values(), key=lambda p: (p.get("order") or 0, p["name"]))]

    def plan_of(self, dev: dict) -> dict | None:
        pos = dev.get("position") or {}
        if pos.get("plan") in self.plans:
            return self.plans[pos["plan"]]
        room = fold(dev.get("room"))
        if room:
            having = [p for p in self.plans.values() if any(fold(r["name"]) == room for r in p.get("rooms", []))]
            if len(having) == 1:
                return having[0]
        return None

    def floor_of(self, dev: dict) -> str | None:
        plan = self.plan_of(dev)
        return plan["name"] if plan else None

    def find_floor(self, floor: str) -> dict:
        want = fold(floor)
        for p in self.plans.values():
            if want in (fold(p["name"]), fold(p["id"])):
                return p
        loose = [p for p in self.plans.values() if want and want in fold(p["name"])]
        if len(loose) == 1:
            return loose[0]
        names = ", ".join(p["name"] for p in self.plans.values()) or "none: no plan has been added"
        raise ActionError(f"no floor called '{floor}'. Floors: {names}")

    def _point(self, dev: dict):
        pos = dev.get("position") or {}
        if pos.get("plan") in self.plans:
            return pos["plan"], pos["x"], pos["y"]
        return None

    def distance_m(self, a, b) -> float | None:
        """Between two (plan, x, y) points; None across floors."""
        if not a or not b or a[0] != b[0]:
            return None
        return math.hypot(a[1] - b[1], a[2] - b[2]) * metres_per_unit(self.plans[a[0]])

    def landmark_near(self, dev: dict) -> str | None:
        """The nearest label that is not the room's own, within NEAR_M."""
        pt = self._point(dev)
        if not pt:
            return None
        best = None
        for m in self.landmarks.get(pt[0], []):
            if m["room"]:
                continue
            d = self.distance_m(pt, (pt[0], m["x"], m["y"]))
            if d is not None and d <= NEAR_M and (best is None or d < best[0]):
                best = (d, m["name"])
        return best[1] if best else None

    def references(self, views: list[dict], near: str) -> tuple[str, list]:
        """The points `near` names: a placed device, or every landmark whose
        label contains it. Returns (what it matched, points)."""
        want = fold(near)
        devs = [d for d in views if d.get("status") == "active" and self._point(d) and
                (want == fold(d["name"]) or want in map(fold, d.get("aliases", [])))]
        if not devs:
            devs = [d for d in views if d.get("status") == "active" and self._point(d) and want in fold(d["name"])]
        if len(devs) == 1:
            return devs[0]["name"], [self._point(devs[0])]
        marks = [(pid, m) for pid, ms in self.landmarks.items() for m in ms if want and want in fold(m["name"])]
        if marks:
            return ", ".join(sorted({m["name"] for _p, m in marks})), [(pid, m["x"], m["y"]) for pid, m in marks]
        if len(devs) > 1:
            raise ActionError(f"'{near}' matches several devices: {', '.join(d['name'] for d in devs[:10])}")
        raise ActionError(f"nothing on the plans is called '{near}': name a placed device or a label on a plan")

    def nearest(self, dev: dict, points: list) -> float | None:
        pt = self._point(dev)
        ds = [d for p in points if (d := self.distance_m(pt, p)) is not None]
        return min(ds) if ds else None
