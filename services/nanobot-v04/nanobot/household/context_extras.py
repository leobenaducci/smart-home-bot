"""household: memory and context the fork added around each turn (D2).

Three pieces, each on an upstream seam rather than inside upstream's
ContextBuilder:

- **standing context** (``household/standing_context.py``): a persona HomeCore
  re-sends every turn inside a marked region. The model reads it every turn;
  the session keeps none of it. Upstream persists the user message verbatim
  and replays runtime-context blocks with it, so the region is cut from the
  message before upstream persists it and the text rides the **system
  prompt** for that turn instead (system prompts are rebuilt, never stored).
  A turn that brings none (a finished background task, a cron turn) reuses
  the last copy remembered on the session, as the fork did.
- **Recent History**: this session's not-yet-dreamt history summaries, put in
  front of the current user message only (never persisted; volatile text
  after the stable prefix, the fork's cache rule). Entries match on upstream's
  ``session_key`` or the fork's older ``session`` tag.
- **contextBlockLimit**: the most conversation history (tokens, system
  prompt excluded) a turn may carry; older messages are dropped at a legal
  boundary. Upstream removed the setting in favour of ContextGovernor and
  compaction; ``null`` (the default) leaves upstream's behaviour alone.
"""

from __future__ import annotations

import contextvars
from typing import Any

from loguru import logger

from nanobot.household import standing_context

STANDING_KEY = "household_standing_context"
MAX_RECENT_HISTORY = 50
MAX_HISTORY_CHARS = 32_000

TURN_STANDING: contextvars.ContextVar[str] = contextvars.ContextVar("household_standing", default="")
TURN_RECENT: contextvars.ContextVar[str] = contextvars.ContextVar("household_recent", default="")
# A per-turn instruction for the model (plans: "this request takes several
# steps"), in front of this turn's message only, never persisted.
TURN_NOTE: contextvars.ContextVar[str] = contextvars.ContextVar("household_turn_note", default="")


def take_standing(session: Any, content: Any) -> tuple[Any, str]:
    """(content to persist and route, standing text for the system prompt)."""
    if isinstance(content, str) and (block := standing_context.extract(content)):
        if session.metadata.get(STANDING_KEY) != block:
            session.metadata[STANDING_KEY] = block
        return standing_context.for_history(content), standing_context.for_prompt(block)
    return content, standing_context.for_prompt(session.metadata.get(STANDING_KEY, "") or "")


def recent_history(memory: Any, session_key: str | None) -> str:
    if memory is None or not session_key:
        return ""
    try:
        entries = memory.read_unprocessed_history(since_cursor=memory.get_last_dream_cursor())
    except Exception:
        logger.debug("recent history unavailable", exc_info=True)
        return ""
    own = [e for e in entries if (e.get("session_key") or e.get("session")) == session_key]
    if not own:
        return ""
    from nanobot.utils.helpers import truncate_text

    text = "\n".join(f"- [{e.get('timestamp', '')}] {e.get('content', '')}"
                     for e in own[-MAX_RECENT_HISTORY:])
    return "# Recent History\n\n" + truncate_text(text, MAX_HISTORY_CHARS)


def limit_history(history: list[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    if not limit or not history:
        return history
    from nanobot.utils.helpers import estimate_message_tokens, find_legal_message_start

    total, start = 0, len(history)
    for i in range(len(history) - 1, -1, -1):
        total += estimate_message_tokens(history[i])
        if total > limit:
            break
        start = i
    if start == 0:
        return history
    kept = history[start:]
    kept = kept[find_legal_message_start(kept):]
    logger.info("contextBlockLimit {}: history {} -> {} messages", limit, len(history), len(kept))
    return kept


def install(loop: Any) -> None:
    """Wrap the loop's ContextBuilder so the per-turn extras reach the prompt."""
    context = loop.context
    build_system_prompt = context.build_system_prompt
    build_current_message = context.build_current_message

    def system_prompt(*args: Any, **kwargs: Any) -> str:
        prompt = build_system_prompt(*args, **kwargs)
        standing = TURN_STANDING.get()
        return f"{prompt}\n\n{standing}" if standing else prompt

    def current_message(current: str, **kwargs: Any) -> dict[str, Any]:
        message = build_current_message(current, **kwargs)
        recent = "\n\n".join(p for p in (TURN_RECENT.get(), TURN_NOTE.get()) if p)
        if recent and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = f"{recent}\n\n{content}"
            elif isinstance(content, list):
                message["content"] = [{"type": "text", "text": recent}, *content]
        return message

    context.build_system_prompt = system_prompt
    context.build_current_message = current_message


__all__ = ["STANDING_KEY", "TURN_NOTE", "TURN_RECENT", "TURN_STANDING", "install", "limit_history",
           "recent_history", "take_standing"]
