"""household: branch a conversation at one message (fork /v1/sessions/fork, item 7).

The fork copied a prefix of one JSONL session into a new one. v0.4.1 keeps
sessions in SQLite behind ``SessionManager.state``; its only writers are turns,
children and history facts. A fresh session written through the same owner
(``state.get`` creates it, ``state.finish_turn`` commits the transcript) is the
store-level copy: no turn runs, no model is called, and the target is refused
if it already holds messages.
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from loguru import logger


class ForkError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def find_legal_message_end(messages: list[dict[str, Any]], upto: int) -> int:
    """Trim `upto` back until no assistant tool call is left without its result.

    The mirror of `find_legal_message_start`, and needed for the same reason at
    the other end: a cut through the middle of a turn leaves an assistant
    message that announced tool calls and no `tool` messages answering them.
    Every provider rejects that outright — it is not a soft inconsistency, it is
    a 400 — so a fork that copies a prefix has to land on a boundary rather than
    wherever the anchor happened to be.

    Walks backwards, because dropping the offending assistant message can expose
    another one behind it: a turn that called a tool, read the result and called
    another is two assistant messages deep, and cutting between them is illegal
    twice.
    """
    end = max(0, min(int(upto), len(messages)))
    while end:
        declared: dict[str, int] = {}
        answered: set[str] = set()
        for i, msg in enumerate(messages[:end]):
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared[str(tc["id"])] = i
            elif msg.get("role") == "tool":
                tid = msg.get("tool_call_id")
                if tid:
                    answered.add(str(tid))
        orphans = [i for tid, i in declared.items() if tid not in answered]
        if not orphans:
            return end
        # Cut to just before the earliest assistant left holding an unanswered
        # call. Not `end - 1`: that would walk back one message at a time
        # through a long tool turn, re-scanning the whole prefix each step.
        end = min(orphans)
    return 0


async def fork_session(sessions: Any, source_key: str, target_key: str, anchor: str, role: str,
                       message_text: Callable[[dict[str, Any]], str]) -> dict[str, Any]:
    """Copy *source_key* up to the anchor into the new *target_key*."""
    if source_key == target_key:
        raise ForkError(400, "a session cannot fork into itself")
    source = await sessions.state.read(source_key)
    if source is None or not source.messages:
        raise ForkError(404, f"nothing to fork: {source_key} has no messages")
    needle = " ".join(anchor.split())[:400]
    cut = None
    for i in range(len(source.messages) - 1, -1, -1):
        msg = source.messages[i]
        if msg.get("role") != role:
            continue
        if needle and needle in " ".join(message_text(msg).split()):
            cut = i if role == "user" else i + 1
            break
    if cut is None:
        raise ForkError(404, "that message is not in this conversation any more")
    kept = find_legal_message_end(source.messages, cut)
    target = await sessions.state.get(target_key)
    if target.messages:
        raise ForkError(409, f"{target_key} already has a conversation in it")
    target.messages = copy.deepcopy(source.messages[:kept])
    target.last_consolidated = 0
    target.metadata.update({"forked_from": source_key, "forked_messages": kept})
    await sessions.state.finish_turn(target)
    logger.info("api: forked {} -> {} at {} message(s) of {}", source_key, target_key, kept,
                len(source.messages))
    return {"ok": True, "source_key": source_key, "target_key": target_key, "kept": kept,
            "source_messages": len(source.messages), "trimmed": max(0, cut - kept)}


__all__ = ["ForkError", "find_legal_message_end", "fork_session"]
