"""The queue: every job the studio's card will run, persisted.

One SQLite file in the studio's state, so a restart -- a deploy, a crash, the
machine rebooting -- resumes the queue instead of losing it. A job that was
running when the process died is put back in the queue: it produced nothing
the project recorded, so running it again is the only honest outcome.

Order is fair rather than first-come: each person's oldest ready job is a
candidate, and the person served least recently goes first. A long video is
many shot jobs from one person, so somebody else's picture waits for one
shot, not for the film. Among equals the job for the model already loaded
wins, because a swap costs minutes.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

STATES = ("queued", "running", "done", "failed", "cancelled")
ACTIVE = ("queued", "running")

# Seconds, before anything was measured on this machine -- from the prototype
# (docs/home-studio.md). Replaced by the median of what each kind really took.
DEFAULT_SECONDS = {"image": 60, "song": 330, "instrumental": 300, "voice": 90,
                   "video_shot": 1800, "edit": 1800, "analyze": 60, "repaint": 120, "board": 60, "portrait": 60,
                   "score": 120}
# Kinds whose time grows with the seconds they make: estimated per second of
# video, so a 20-second shot is not promised in the time of a 5-second one --
# a music video queues dozens of them.
PER_SECOND = ("video_shot", "edit")
# A model load that is not needed when the same model ran last.
LOAD_SECONDS = {"image": 10, "song": 60, "instrumental": 60, "voice": 30,
                "video_shot": 240, "edit": 240, "analyze": 0, "repaint": 30, "board": 10, "portrait": 10,
                "score": 0}


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    owner_name TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    model TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    project TEXT NOT NULL DEFAULT '',
                    target TEXT NOT NULL DEFAULT '',
                    params TEXT NOT NULL,
                    after TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'queued',
                    priority INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL,
                    started REAL,
                    finished REAL,
                    progress REAL NOT NULL DEFAULT 0,
                    phase TEXT NOT NULL DEFAULT '',
                    files TEXT NOT NULL DEFAULT '[]',
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
            """)
            # Whatever was running when the process stopped runs again.
            c.execute("UPDATE jobs SET state='queued', started=NULL, progress=0, phase='' "
                      "WHERE state='running'")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        d["params"] = json.loads(d["params"])
        d["files"] = json.loads(d["files"])
        return d

    # -- writing ------------------------------------------------------------
    def add(self, *, owner: str, owner_name: str, kind: str, model: str, params: dict,
            title: str = "", project: str = "", target: str = "", after: str = "") -> dict:
        job_id = uuid.uuid4().hex[:12]
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO jobs (id, owner, owner_name, kind, model, title, project, target, "
                      "params, after, created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (job_id, owner, owner_name, kind, model, title[:120], project, target,
                       json.dumps(params, ensure_ascii=False), after, time.time()))
        return self.get(job_id)

    def update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        for key in ("params", "files"):
            if key in fields:
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def cancel(self, job_id: str) -> str | None:
        """Cancel a queued job, and every queued job that waits on it. The
        state it was in, or None when there is no such job."""
        job = self.get(job_id)
        if not job:
            return None
        if job["state"] == "queued":
            with self._lock, self._conn() as c:
                c.execute("UPDATE jobs SET state='cancelled', finished=? WHERE id=?", (time.time(), job_id))
            for dep in self.dependents(job_id):
                self.cancel(dep["id"])
        return job["state"]

    # -- reading ------------------------------------------------------------
    def requeue(self, job_id: str) -> None:
        """A running job back in the queue, to start over: a pause that stopped
        it mid-way. Its place is kept -- it was first in line once already."""
        with self._lock, self._conn() as c:
            c.execute("UPDATE jobs SET state='queued', started=NULL, progress=0, phase='' "
                      "WHERE id=? AND state='running'", (job_id,))

    def get(self, job_id: str) -> dict | None:
        with self._conn() as c:
            return self._row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def active(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM jobs WHERE state IN ('queued','running') "
                             "ORDER BY created").fetchall()
        return [self._row(r) for r in rows]

    def recent(self, owner: str | None = None, limit: int = 50) -> list[dict]:
        q = "SELECT * FROM jobs WHERE state NOT IN ('queued','running')"
        args: tuple = ()
        if owner:
            q += " AND owner=?"
            args = (owner,)
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY finished DESC LIMIT ?", (*args, limit)).fetchall()
        return [self._row(r) for r in rows]

    def dependents(self, job_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM jobs WHERE after=? AND state='queued'", (job_id,)).fetchall()
        return [self._row(r) for r in rows]

    def running(self) -> dict | None:
        with self._conn() as c:
            return self._row(c.execute("SELECT * FROM jobs WHERE state='running'").fetchone())

    # -- ordering -----------------------------------------------------------
    def _ready(self, job: dict) -> bool:
        if not job["after"]:
            return True
        dep = self.get(job["after"])
        return dep is None or dep["state"] == "done"

    def blocked_forever(self) -> list[dict]:
        """Queued jobs whose prerequisite failed or was cancelled: they can
        never start (a shot needs the frame the one before it did not make)."""
        out = []
        for job in self.active():
            if job["state"] == "queued" and job["after"]:
                dep = self.get(job["after"])
                if dep and dep["state"] in ("failed", "cancelled"):
                    out.append(job)
        return out

    def last_served(self) -> dict[str, float]:
        with self._conn() as c:
            rows = c.execute("SELECT owner, MAX(started) FROM jobs WHERE started IS NOT NULL "
                             "GROUP BY owner").fetchall()
        return {r[0]: r[1] or 0.0 for r in rows}

    def order(self, loaded_model: str = "") -> list[dict]:
        """The queued jobs in the order they will run.

        Repeatedly: each person's oldest ready job is a candidate; a raised
        priority first, then the person served least recently, then the job
        for the model already loaded, then the oldest. What is chosen is
        taken out and the choice is made again -- so the whole order is the
        one the card will actually follow, and positions are real.
        """
        queued = [j for j in self.active() if j["state"] == "queued"]
        served = dict(self.last_served())
        running = self.running()
        model = running["model"] if running else loaded_model
        if running:
            served[running["owner"]] = time.time()
        done_ids = set()
        out: list[dict] = []
        pending = list(queued)
        while pending:
            candidates: dict[str, dict] = {}
            for job in pending:
                ready = (not job["after"]) or job["after"] in done_ids or self._ready(job)
                if ready and job["owner"] not in candidates:
                    candidates[job["owner"]] = job
            if not candidates:
                out.extend(pending)                     # waiting on something that will not come
                break
            pick = min(candidates.values(), key=lambda j: (
                -j["priority"], served.get(j["owner"], 0.0), j["model"] != model, j["created"]))
            out.append(pick)
            pending.remove(pick)
            done_ids.add(pick["id"])
            served[pick["owner"]] = time.time() + len(out)
            model = pick["model"]
        return out

    def next_job(self, loaded_model: str = "") -> dict | None:
        for job in self.order(loaded_model):
            if self._ready(job):
                return job
        return None

    # -- estimates ----------------------------------------------------------
    def seconds_for(self, kind: str) -> float:
        """The median of what this kind took here lately, else the default."""
        with self._conn() as c:
            rows = [r[0] for r in c.execute(
                "SELECT finished - started FROM jobs WHERE kind=? AND state='done' "
                "AND started IS NOT NULL ORDER BY finished DESC LIMIT 15", (kind,))]
        if not rows:
            return float(DEFAULT_SECONDS.get(kind, 300))
        rows.sort()
        return float(rows[len(rows) // 2])

    def rate(self, kind: str) -> float:
        """Card seconds per second of video for *kind*: the median of what it
        took here lately, else the default shot's time over five seconds."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT finished - started, params FROM jobs WHERE kind=? AND state='done' "
                "AND started IS NOT NULL ORDER BY finished DESC LIMIT 15", (kind,)).fetchall()
        rates = []
        for took, params in rows:
            try:
                secs = float(json.loads(params).get("seconds") or 0)
            except (ValueError, TypeError, AttributeError):
                continue
            if secs > 0 and took and took > 0:
                rates.append(took / secs)
        if not rates:
            return DEFAULT_SECONDS.get(kind, 1800) / 5.0
        rates.sort()
        return rates[len(rates) // 2]

    def seconds_for_job(self, job: dict) -> float:
        if job["kind"] in PER_SECOND:
            try:
                secs = float((job.get("params") or {}).get("seconds") or 5)
            except (TypeError, ValueError):
                secs = 5.0
            return self.rate(job["kind"]) * max(1.0, secs)
        return self.seconds_for(job["kind"])

    def schedule(self, loaded_model: str = "") -> list[dict]:
        """The order with a start estimate for each job, from now."""
        running = self.running()
        now = time.time()
        clock = now
        model = loaded_model
        if running:
            # What is left of the running job: its typical length times the
            # share not done yet, never less than half a minute.
            left = max(30.0, self.seconds_for_job(running) * (1 - running["progress"]))
            clock = now + left
            model = running["model"]
        out = []
        for i, job in enumerate(self.order(loaded_model), 1):
            swap = LOAD_SECONDS.get(job["kind"], 60) if job["model"] != model else 0
            took = self.seconds_for_job(job)
            out.append({**job, "position": i, "starts_in": round(clock - now),
                        "takes": round(took + swap)})
            clock += took + swap
            model = job["model"]
        return out
