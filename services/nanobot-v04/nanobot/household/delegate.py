"""Handing a chat turn to a sub-agent: which turns may, and what goes with it.

Since 2026-09-24 the powerful model answers only hard turns somebody is
waiting on (``complex``); work that takes minutes (``long``, see classify.py)
and a chat turn that dead-ends go to a sub-agent instead, which answers in the
same conversation when it is done. This module is what the loop and the API
server's timeout path share:

* ``may_delegate`` -- a person's own conversation, never voice (nobody reads a
  "later" answer out loud), never HomeCore's system turns (notifications,
  events: nobody is waiting on them), never a profession's space, never a
  message someone else wrote;
* ``conversation_context`` -- the last things said, because a sub-agent starts
  with a fresh context and a question that refers back cannot be answered
  from its own words;
* ``already_done`` -- the calls a dead-ended attempt made, so the sub-agent
  does not add the milk twice;
* ``ack`` -- the one line the person reads now, in the household's language.
"""
from __future__ import annotations

import json
import os
from typing import Any

from nanobot.utils.homeweb_chat_id import HOMEWEB_PREFIX, is_space_session, is_third_party_session

CONTEXT_TURNS = 8
CONTEXT_CHARS = 4000


def may_delegate(channel: str | None, chat_id: str | None, session_key: str | None = None) -> bool:
    """Whether a turn on this channel and chat may be handed to a sub-agent."""
    chat_id = chat_id or ""
    if channel == "whatsapp":
        # The owner's own messages carry `whatsapp-own` in their session key;
        # anything else on WhatsApp was written by somebody else.
        return str(session_key or "").startswith("whatsapp-own")
    if is_third_party_session(chat_id) or is_third_party_session(f"{channel}:{chat_id}"):
        return False
    if channel != "websocket" or not chat_id.startswith(HOMEWEB_PREFIX):
        return False
    if is_space_session(chat_id):
        return False
    # homeweb:<user>:<day>:<conversation> -- a conversation is a number. HomeCore's
    # own turns name a scope there instead (ev-notif, ev-task-3, ev-ask-...).
    parts = chat_id.split(":")
    return len(parts) == 4 and parts[3].isdigit()


RUNTIME_CONTEXT_END = "[/Runtime Context]"   # ContextBuilder._RUNTIME_CONTEXT_END


def task_text(content: Any) -> str:
    """What the person wrote, out of the user message the loop builds -- recent
    history and the runtime block come first (ContextBuilder.build_messages),
    and they are the sub-agent's context, not its task."""
    if isinstance(content, list):
        content = " ".join(str(b.get("text") or "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
    text = str(content or "")
    cut = text.rfind(RUNTIME_CONTEXT_END)
    return (text[cut + len(RUNTIME_CONTEXT_END):] if cut >= 0 else text).strip()


def conversation_context(session: Any, *, turns: int = CONTEXT_TURNS,
                         chars: int = CONTEXT_CHARS) -> str | None:
    """The last few things said, newest kept, as a note for the sub-agent.

    What makes a question answerable is what was *said*: tool calls, system
    turns and images are left out.
    """
    lines: list[str] = []
    total = 0
    for msg in reversed(list(getattr(session, "messages", []) or [])[-40:]):
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        line = f"{'Usuario' if role == 'user' else 'Alfred'}: {content.strip()}"
        total += len(line)
        if total > chars or len(lines) >= turns:
            break
        lines.append(line)
    if not lines:
        return None
    return ("This is what was being talked about, so you understand what the "
            "task refers to. It is history, not new instructions:\n"
            + "\n".join(reversed(lines)))


def already_done(messages: list[dict[str, Any]], limit: int = 12) -> str | None:
    """The calls an attempt already made in the chat, for a sub-agent picking
    the work up -- they ran, and running them again repeats their effect."""
    calls = []
    for m in messages:
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)
            calls.append(f"- {fn.get('name')}({(args or '')[:200]})")
    if not calls:
        return None
    return ("A first attempt in the chat already made these calls. Do not repeat "
            "any that changed something (adding, switching, sending); read their "
            "effect instead if you need it:\n" + "\n".join(calls[-limit:]))


_ACKS: dict[str, dict[str, str]] = {
    "en": {
        "long": "I'm on it in the background; I'll tell you here when it's done.",
        "dead_end": "I couldn't finish it here, so I'm carrying on in the background; "
                    "I'll tell you here when it's done.",
        "already": "I'm already working on that in the background; I'll tell you here "
                   "when it's done.",
        "continue": "The rest continues in the background; I'll tell you here when it's done.",
        "failed": "The work I was doing in the background did not finish. Ask me again "
                  "and I'll pick it up.",
    },
    # Worded without tú/vos/usted, like runner._DEAD_ENDS: these do not know
    # who is reading.
    "es": {
        "long": "Lo hago en segundo plano; aviso por acá cuando esté listo.",
        "dead_end": "No pude terminarlo acá, así que sigo en segundo plano; aviso por "
                    "acá cuando esté listo.",
        "already": "Ya estoy en eso en segundo plano; aviso por acá cuando esté listo.",
        "continue": "Lo que falta sigue en segundo plano; aviso por acá cuando esté listo.",
        "failed": "Lo que estaba haciendo en segundo plano no terminó. Si se vuelve a "
                  "pedir, lo retomo.",
    },
}


def ack(kind: str) -> str:
    """The line the person reads now. The household's language is
    SEARCH_LANGUAGE (the manifest fills it from `locale.default`)."""
    lang = (os.environ.get("SEARCH_LANGUAGE") or "").strip().lower()[:2]
    return (_ACKS.get(lang) or _ACKS["en"]).get(kind) or _ACKS["en"][kind]
