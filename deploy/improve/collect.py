"""Turns into episodes: what was asked, what came back, and what went wrong.

Reads, never writes, the house's own records:

* `usage.db` -- one row per billed part of a turn, with the stop reason,
  latency, failures and routing (the columns added on 2026-09-29), and
  `turn_events`, the runner's codes;
* the portal's history -- what the person wrote and what Alfred answered, a 👍
  or 👎 and its note, a Stop.

An episode is one answer in the chat, joined to the usage rows its turn
billed, or -- for a turn nobody reads in the chat, an event or a background
task -- the usage rows alone. It carries **signals**, the reasons it might be
worth a look; `signals_of` is the whole list. Every episode with one is kept;
of the rest, a small sample, so the evaluator can say how often "no signal"
still means "wrong".

Nothing here leaves the house. `redact.py` runs over what this produces
before anything is written where a model outside can read it.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

# The profession folders, as the portal names them, and the scope usage.db
# files their turns under (CHAT_SPACES in the portal). A folder not listed is
# read with its own name as the scope.
SPACE_SCOPES = {"programmer": "dev", "teacher": "edu", "designer": "dsg",
                "doctor": "sal", "legal": "ley"}
# The folders from before the professions were renamed, which the portal still
# writes to when that is what exists (`_space_history_dirname`, and
# CHAT_SPACE_ALIASES beside CHAT_SPACES). Read as their own name they filed a
# Programmer turn under scope "programador", joined to no usage row at all.
# `finanzas` is a profession that no longer exists, billed as `fin`.
LEGACY_SPACES = {"programador": "programmer", "profesor": "teacher", "disenador": "designer",
                 "medico": "doctor", "salud": "doctor"}
RETIRED_SCOPES = {"finanzas": "fin"}


def scope_of_folder(name: str) -> str:
    """The usage scope of a profession's history folder, old names included."""
    space = LEGACY_SPACES.get(name, name)
    return SPACE_SCOPES.get(space) or RETIRED_SCOPES.get(name) or name
# A usage row belongs to the answer filed within this long after it.
JOIN_BEFORE_S = 30
JOIN_AFTER_S = 120
# How soon a second message reads as the first one not having worked.
FOLLOWUP_S = 120
SLOW_MS = 90_000
TEXT_MAX = 2000
# The runner's stop reasons that are an outcome, not a failure.
FINE_STOPS = {"", "completed"}
_CORRECTION = re.compile(
    r"^\s*(no\b|nop\b|eso no|otra vez|de nuevo|no (anda|funcion|funcionó|sirve|era)|"
    r"mal\b|incorrect|wrong|that'?s not|try again|again\b|not what)", re.I)
_WORD = re.compile(r"\w{3,}", re.U)


@dataclass
class Row:
    ts: int
    scope: str
    model: str
    stop_reason: str
    turn_id: str
    latency_ms: int | None
    call_errors: int
    tool_errors: int
    escalated: int
    escalated_from: str
    tier: str
    tools: str


def _usage(db: Path, login: str | None, since: int, until: int) -> list[tuple[str, Row]]:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(token_usage)")}
        if "turn_id" not in cols:
            return []  # a usage.db from before the telemetry: nothing to join
        sql = ("SELECT username, ts, scope, model, stop_reason, turn_id, latency_ms, "
               "call_errors, tool_errors, escalated, escalated_from, tier, tool_names "
               "FROM token_usage WHERE ts >= ? AND ts < ?")
        args: list = [since, until]
        if login:
            sql += " AND username = ?"
            args.append(login)
        return [(r[0], Row(*r[1:])) for r in con.execute(sql + " ORDER BY ts", args)]
    finally:
        con.close()


def _events(db: Path, turn_ids: set[str]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    if not turn_ids:
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        ids = sorted(turn_ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for tid, code, n in con.execute(
                    f"SELECT turn_id, code, n FROM turn_events WHERE turn_id IN "
                    f"({','.join('?' * len(chunk))})", chunk):
                out.setdefault(tid, {})
                out[tid][code] = out[tid].get(code, 0) + n
        con.close()
    except sqlite3.Error:
        pass
    return out


def _history(root: Path, login: str, day: str) -> Iterator[tuple[str, list[dict]]]:
    """(scope, messages) for one person's day: the ordinary chat, then each
    profession's folder."""
    base = root / login
    for path, scope in [(base / f"{day}.json", "")] + [
            (d / f"{day}.json", scope_of_folder(d.name))
            for d in sorted(base.iterdir()) if d.is_dir()] if base.is_dir() else []:
        try:
            msgs = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(msgs, list):
            yield scope, [m for m in msgs if isinstance(m, dict)]


def _similar(a: str, b: str) -> float:
    wa, wb = set(_WORD.findall(a.lower())), set(_WORD.findall(b.lower()))
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def signals_of(ep: dict) -> list[str]:
    """Why this episode might be worth a look. Order is severity, roughly."""
    s = []
    fb = ep.get("feedback") or {}
    if fb.get("rating") == "down":
        s.append("thumbs_down")
    if ep.get("interrupted"):
        s.append("stopped")
    if ep.get("followup"):
        s.append(ep["followup"])  # correction | rephrase
    for stop in ep.get("stop_reasons") or []:
        if stop not in FINE_STOPS:
            s.append(f"stop:{stop}")
    if ep.get("call_errors"):
        s.append("llm_error")
    if ep.get("tool_errors"):
        s.append("tool_error")
    if ep.get("escalated"):
        s.append("escalated")
    if (ep.get("latency_ms") or 0) > SLOW_MS and ep.get("scope") in ("",) + tuple(SPACE_SCOPES.values()):
        s.append("slow")
    for code in ep.get("events") or {}:
        if not code.startswith("llm:truncated"):
            s.append(f"event:{code.split(':')[0]}")
    if fb.get("rating") == "up":
        s.append("thumbs_up")  # a signal too: what good looks like
    out: list[str] = []
    for x in s:
        if x not in out:
            out.append(x)
    return out


def _fold_rows(ep: dict, rows: list[Row], events: dict[str, dict[str, int]]) -> None:
    ep["models"] = sorted({r.model for r in rows if r.model})
    ep["stop_reasons"] = [r.stop_reason for r in rows]
    ep["latency_ms"] = sum(r.latency_ms or 0 for r in rows) or None
    ep["call_errors"] = sum(r.call_errors for r in rows)
    ep["tool_errors"] = sum(r.tool_errors for r in rows)
    ep["escalated"] = any(r.escalated for r in rows)
    ep["tiers"] = sorted({r.tier for r in rows if r.tier})
    ep["tools"] = [t for r in rows for t in (r.tools.split(",") if r.tools else [])][:40]
    ev: dict[str, int] = {}
    for tid in {r.turn_id for r in rows if r.turn_id}:
        for code, n in (events.get(tid) or {}).items():
            ev[code] = ev.get(code, 0) + n
    ep["events"] = ev


def _clip(text: str) -> str:
    text = text or ""
    return text if len(text) <= TEXT_MAX else text[:TEXT_MAX] + " […]"


def _sampled(key: str, rate: float) -> bool:
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < rate


def collect(state: Path, day: str, tz: timezone | None = None, sample: float = 0.08,
            logins: list[str] | None = None) -> list[dict]:
    """The episodes of one day (the house's local day), newest last.

    Every episode with a signal, and *sample* of those without. A heartbeat is
    never sampled: 230 a day of "nothing to report" would be most of any
    sample and teach nothing."""
    tz = tz or timezone.utc
    d0 = datetime.combine(date.fromisoformat(day), datetime.min.time(), tz)
    since, until = int(d0.timestamp()), int((d0 + timedelta(days=1)).timestamp())
    usage = state / "home-core" / "data" / "usage.db"
    hist = state / "home-core" / "history"
    rows = _usage(usage, None, since - JOIN_AFTER_S, until + JOIN_AFTER_S)
    events = _events(usage, {r.turn_id for _, r in rows if r.turn_id})
    by_login: dict[str, list[Row]] = {}
    for login, r in rows:
        by_login.setdefault(login, []).append(r)
    people = sorted(set(by_login) | ({p.name for p in hist.iterdir() if p.is_dir()}
                                     if hist.is_dir() else set()))
    if logins:
        people = [p for p in people if p in logins]

    episodes: list[dict] = []
    for login in people:
        mine = by_login.get(login, [])
        used: set[int] = set()
        for scope, msgs in _history(hist, login, day):
            for i, m in enumerate(msgs):
                if m.get("role") != "bot":
                    continue
                ts = int((m.get("ts") or 0) / 1000)
                ask = next((x for x in reversed(msgs[:i]) if x.get("role") == "user"), None)
                start = int((ask or {}).get("ts", 0) / 1000) or ts - JOIN_AFTER_S
                joined = [k for k, r in enumerate(mine) if k not in used and r.scope == scope
                          and start - JOIN_BEFORE_S <= r.ts <= ts + JOIN_AFTER_S]
                used.update(joined)
                nxt = next((x for x in msgs[i + 1:] if x.get("role") == "user"), None)
                followup = ""
                if nxt and 0 <= (nxt.get("ts", 0) - m.get("ts", 0)) / 1000 <= FOLLOWUP_S:
                    if _CORRECTION.search(nxt.get("text") or ""):
                        followup = "correction"
                    elif ask and _similar(nxt.get("text") or "", ask.get("text") or "") >= 0.5:
                        followup = "rephrase"
                ep = {"day": day, "login": login, "scope": scope, "ts": ts,
                      "request": _clip((ask or {}).get("text", "")),
                      "answer": _clip(m.get("text", "")),
                      "next": _clip((nxt or {}).get("text", "")) if followup else "",
                      "followup": followup, "interrupted": bool(m.get("interrupted")),
                      "feedback": m.get("feedback") or None,
                      "plan": bool(m.get("plan"))}
                _fold_rows(ep, [mine[k] for k in joined], events)
                if since <= ts < until:
                    episodes.append(ep)
        # The rest: turns nobody reads in the chat. Grouped by turn id, so an
        # escalation's two rows are one episode.
        turns: dict[str, list[Row]] = {}
        for k, r in enumerate(mine):
            if k in used or not (since <= r.ts < until):
                continue
            turns.setdefault(r.turn_id or f"row{k}", []).append(r)
        for tid, rs in turns.items():
            ep = {"day": day, "login": login, "scope": rs[0].scope, "ts": rs[-1].ts,
                  "request": "", "answer": "", "next": "", "followup": "",
                  "interrupted": False, "feedback": None, "plan": False}
            _fold_rows(ep, rs, events)
            episodes.append(ep)

    kept = []
    for ep in sorted(episodes, key=lambda e: e["ts"]):
        ep["id"] = hashlib.sha256(f"{ep['login']}:{ep['scope']}:{ep['ts']}".encode()).hexdigest()[:12]
        ep["signals"] = signals_of(ep)
        if ep["signals"] or (ep["scope"] != "ev-heartbeat" and _sampled(ep["id"], sample)):
            ep["sampled"] = not ep["signals"]
            kept.append(ep)
    return kept
