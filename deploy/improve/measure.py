"""Numbers, so that a proposal about how the assistant behaves starts from what it
does rather than from what it says it does.

    improve measure usage [--days N]    turns, tokens and latency by scope and model
    improve measure stops [--days N]    how turns ended: stop reasons, escalations
    improve measure events [--days N]   the runner's codes: parse failures, retries, refusals
    improve measure tools [--days N]    which tools and skills turns call, and how often

Read only, from usage.db (docs/self-improvement.md, phase 1). Counts and
metadata, never what anybody wrote. On 2026-09-29 the Programmer built a plan
on the everyday model's description of itself -- "I read the whole
SKILL_PYTHON.md for every call" -- which nine days of sessions showed was not
so; these are the queries that settle such a claim.
"""
from __future__ import annotations

import sqlite3
import statistics
from collections import Counter
from pathlib import Path


def _db(state: Path) -> sqlite3.Connection:
    p = state / "home-core" / "data" / "usage.db"
    if not p.exists():
        raise FileNotFoundError(f"no usage.db at {p}")
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def _since(days: int) -> str:
    return f"date('now', '-{int(days)} days')"


def usage(state: Path, days: int = 7) -> str:
    con = _db(state)
    rows = con.execute(
        f"SELECT scope, model, COUNT(*), AVG(prompt_tokens), AVG(cached_tokens), "
        f"AVG(completion_tokens) FROM token_usage WHERE day >= {_since(days)} "
        f"GROUP BY scope, model ORDER BY COUNT(*) DESC").fetchall()
    lat = {}
    for scope, ms in con.execute(
            f"SELECT scope, latency_ms FROM token_usage WHERE day >= {_since(days)} "
            f"AND latency_ms IS NOT NULL"):
        lat.setdefault(scope, []).append(ms)
    con.close()
    out = [f"last {days} day(s): scope / model: turns, mean prompt (cached), mean out, latency p50/p90"]
    for scope, model, n, p, c, o in rows:
        ls = sorted(lat.get(scope) or [])
        l50 = f"{ls[len(ls) // 2] / 1000:.1f}s" if ls else "-"
        l90 = f"{ls[int(len(ls) * 0.9)] / 1000:.1f}s" if ls else "-"
        out.append(f"  {scope or 'chat':<13} {model[:34]:<34} {n:>5}  {p or 0:>8.0f} "
                   f"({c or 0:>7.0f})  {o or 0:>6.0f}  {l50}/{l90}")
    return "\n".join(out)


def stops(state: Path, days: int = 7) -> str:
    con = _db(state)
    rows = con.execute(
        f"SELECT scope, stop_reason, COUNT(*), SUM(escalated) FROM token_usage "
        f"WHERE day >= {_since(days)} AND turn_id != '' GROUP BY scope, stop_reason "
        f"ORDER BY COUNT(*) DESC").fetchall()
    con.close()
    return "\n".join([f"last {days} day(s): scope / stop reason: rows, escalated"] + [
        f"  {s or 'chat':<13} {r or '-':<24} {n:>5}  {e or 0}" for s, r, n, e in rows])


def events(state: Path, days: int = 7) -> str:
    con = _db(state)
    rows = con.execute(
        f"SELECT code, SUM(n), COUNT(DISTINCT turn_id) FROM turn_events "
        f"WHERE day >= {_since(days)} GROUP BY code ORDER BY SUM(n) DESC").fetchall()
    con.close()
    return "\n".join([f"last {days} day(s): code: times, turns"] + [
        f"  {c:<40} {n:>5}  {t}" for c, n, t in rows]) if rows else "no runner events recorded"


def tools(state: Path, days: int = 7) -> str:
    con = _db(state)
    counts: Counter = Counter()
    per_turn = []
    for (names,) in con.execute(f"SELECT tool_names FROM token_usage WHERE day >= {_since(days)} "
                                f"AND tool_names != ''"):
        ns = [n for n in names.split(",") if n]
        counts.update(ns)
        per_turn.append(len(ns))
    con.close()
    head = (f"last {days} day(s): {len(per_turn)} turns called tools, median "
            f"{statistics.median(per_turn) if per_turn else 0} calls a turn")
    return "\n".join([head] + [f"  {n:<32} {c}" for n, c in counts.most_common(30)])


KINDS = {"usage": usage, "stops": stops, "events": events, "tools": tools}
