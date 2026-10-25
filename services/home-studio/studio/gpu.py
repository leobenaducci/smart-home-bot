"""Reservations of the Studio's card: who has it after the Studio, and until when.

The card is the Studio's whenever nobody has asked for it. Anything else that
wants it -- the Programmer's local model, the coding benchmark, a program of
the household's own on the LAN -- reserves it here and waits its turn: one
holder at a time, first come first served, and the Studio's running job is
finished first (stopped and queued again only when a parent's tool asks for
the card *now*). The manager grants a reservation once its own worker has
gone and the card is free, and takes no jobs while any reservation exists.

Every reservation says roughly how long it needs the card (`minutes`), so the
ones behind it and the Studio's page can say when the card comes back. That
is an estimate, not a limit: running over is shown, never enforced, and a
heartbeat may correct it.

A reservation is a lease, not a lock. Its holder sends a heartbeat every ten
minutes (HEARTBEAT_EVERY), and fifteen minutes without one (LEASE) ends it --
so a borrower that dies gives the card back on its own instead of keeping the
Studio off it until somebody notices, and a heartbeat that is a few minutes
late costs nothing. Any request about a reservation counts as one.

Before this, lending was the parent's pause with a reason: one flag for every
borrower, each checking the others by hand -- the Programmer's script looked
for the benchmark's container, the benchmark remembered whether the pause was
its own -- and the flag lived in memory, so a Studio restart ended every loan
under the borrower (2026-10-01). Reservations are kept on disk; after a
restart each gets a fresh LEASE, because the time the Studio was down is not
time its holder failed to show up.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

HEARTBEAT_EVERY = 600
LEASE = 900
# The estimate a reservation must give, in minutes: half a day at most, which
# is longer than anything in this house has wanted the card.
MAX_MINUTES = 720
# How long a request may wait for its turn before it is answered "waiting".
MAX_WAIT = 60


class ReservationError(ValueError):
    pass


def _clean(text, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def minutes_of(value) -> int:
    """The estimate as whole minutes, or ReservationError saying what is wanted."""
    try:
        m = int(round(float(value)))
    except (TypeError, ValueError):
        raise ReservationError("say roughly how long you need the card: minutes") from None
    if not 1 <= m <= MAX_MINUTES:
        raise ReservationError(f"minutes is between 1 and {MAX_MINUTES}")
    return m


class Reservations:
    def __init__(self, path: Path | None = None, clock=time.time):
        self.path, self.clock = (Path(path) if path else None), clock
        self._cv = threading.Condition(threading.RLock())
        self._items: list[dict] = self._load()

    # -- disk ------------------------------------------------------------------
    def _load(self) -> list[dict]:
        if not self.path or not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        now, out = self.clock(), []
        for r in raw if isinstance(raw, list) else []:
            if isinstance(r, dict) and r.get("id") and r.get("state") in ("waiting", "granted"):
                r["heartbeat"] = now
                out.append(r)
        return out

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._items), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- asking ----------------------------------------------------------------
    def reserve(self, holder: str, minutes, purpose: str = "", preempt: bool = False, by: str = "") -> dict:
        """A new reservation at the back of the line. Its id is the only
        handle on it: whoever holds the id holds the reservation."""
        m = minutes_of(minutes)
        now = self.clock()
        r = {"id": secrets.token_urlsafe(18), "holder": _clean(holder, 40) or "unnamed",
             "purpose": _clean(purpose, 120), "by": _clean(by, 40), "state": "waiting",
             "preempt": bool(preempt), "minutes": m, "created": now, "granted_at": None, "heartbeat": now}
        with self._cv:
            self.expire()
            self._items.append(r)
            self._save()
            return dict(r)

    def heartbeat(self, rid: str, minutes=None) -> dict | None:
        """The holder is still there. With `minutes`, how much longer it now
        expects to need the card -- from now, granted or not."""
        m = None if minutes in (None, "") else minutes_of(minutes)
        with self._cv:
            self.expire()
            r = self._find(rid)
            if r is None:
                return None
            now = self.clock()
            r["heartbeat"] = now
            if m is not None:
                r["minutes"] = m if r["state"] == "waiting" else round((now - r["granted_at"]) / 60) + m
            self._save()
            return dict(r)

    def wait(self, rid: str, seconds: float) -> dict | None:
        """A heartbeat, then up to `seconds` for the card to be granted."""
        deadline = time.monotonic() + max(0.0, min(float(seconds or 0), MAX_WAIT))
        with self._cv:
            r = self.heartbeat(rid)
            while r is not None and r["state"] == "waiting":
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cv.wait(left)
                r = self.heartbeat(rid)
            return r

    def release(self, rid: str) -> bool:
        with self._cv:
            before = len(self._items)
            self._items = [r for r in self._items if r["id"] != rid]
            if len(self._items) == before:
                return False
            self._save()
            self._cv.notify_all()
            return True

    # -- the manager's side ----------------------------------------------------
    def expire(self) -> list[dict]:
        """Drop the reservations whose holder stopped sending heartbeats."""
        with self._cv:
            cut = self.clock() - LEASE
            gone = [r for r in self._items if r["heartbeat"] <= cut]
            if gone:
                self._items = [r for r in self._items if r["heartbeat"] > cut]
                self._save()
                self._cv.notify_all()
            return gone

    def active(self) -> bool:
        with self._cv:
            self.expire()
            return bool(self._items)

    def granted(self) -> dict | None:
        with self._cv:
            return next((dict(r) for r in self._items if r["state"] == "granted"), None)

    def grant_next(self) -> dict | None:
        """The first in line gets the card, when nobody has it."""
        with self._cv:
            self.expire()
            if any(r["state"] == "granted" for r in self._items):
                return None
            r = next((r for r in self._items if r["state"] == "waiting"), None)
            if r is None:
                return None
            r["state"], r["granted_at"] = "granted", self.clock()
            self._save()
            self._cv.notify_all()
            return dict(r)

    # -- telling ---------------------------------------------------------------
    def all(self) -> list[dict]:
        with self._cv:
            self.expire()
            return [dict(r) for r in self._items]

    def _find(self, rid: str) -> dict | None:
        return next((r for r in self._items if rid and r["id"] == rid), None)

    def _left(self, r: dict, now: float) -> float:
        """Seconds of its estimate still to go; the whole estimate if it is waiting."""
        if r["state"] == "granted":
            return max(0.0, r["granted_at"] + r["minutes"] * 60 - now)
        return r["minutes"] * 60

    def public(self, r: dict, with_id: bool = False) -> dict:
        """What may be said about a reservation: to its holder (`with_id`),
        everything; to anybody else, who has it or waits, what for, and when.
        `starts_in` counts only the estimates ahead of it -- a Studio job still
        on the card comes first and is not in it."""
        now = self.clock()
        with self._cv:
            ahead, line = 0.0, []
            for x in self._items:
                if x["id"] == r["id"]:
                    break
                ahead += self._left(x, now)
                line.append(x)
        out = {"holder": r["holder"], "purpose": r["purpose"], "state": r["state"], "minutes": r["minutes"],
               "created": r["created"], "granted_at": r["granted_at"]}
        if r["state"] == "granted":
            out.update(ends_in=round(self._left(r, now)),
                       overdue=now > r["granted_at"] + r["minutes"] * 60)
        else:
            out.update(position=sum(x["state"] == "waiting" for x in line) + 1, starts_in=round(ahead))
        if with_id:
            out.update(id=r["id"], heartbeat_every=HEARTBEAT_EVERY,
                       expires_in=max(0, round(r["heartbeat"] + LEASE - now)))
        return out
