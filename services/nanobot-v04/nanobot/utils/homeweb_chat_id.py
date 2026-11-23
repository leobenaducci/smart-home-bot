"""Where a long-lived job's output goes in HomeCore: today, in its own chat.

A HomeCore chat_id is ``homeweb:<user>:<YYYY-MM-DD>[:<conv>]``. The day is
**where the message gets filed** — ``/chat/agent-event`` reads it straight out
of the id — and the fourth segment names the *conversation* (the sidebar entry,
and the model session HomeCore runs that conversation's turns in).

Two things have to be rewritten when a job fires, and they are rewritten here:

**The day.** ``CronTool._add_job`` stores the chat_id that was current when the
job was created (``to=chat_id``), so a daily job created on 15 July went on
posting to 15 July for the rest of its life. The ntfy push still arrived, which
is why it looked like the message was simply missing from the chat rather than
filed under a day nobody scrolls back to.

**The conversation.** Carrying the *stored* one would tie this morning's
message to a conversation from weeks ago, so it used to be dropped — but an
unstamped message inherits whatever conversation is on screen, which is how the
6 AM greeting ended up appended to last night's chat instead of opening one of
its own. A job fires in its own model session (``session_key=f"cron:{job.id}"``)
and knows nothing of what was being discussed, so joining an existing
conversation is a lie either way. Each firing therefore names a *new*
conversation, keyed on the moment it fires.

The user segment is left exactly as it was — that is the security property
HomeCore re-checks against the authenticated proxy user, and nothing here should
be able to move a message into somebody else's history.
"""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

HOMEWEB_PREFIX = "homeweb:"


def _now(timezone: str | None, now_ms: int | None) -> datetime:
    """The moment this delivery happens, in the user's timezone.

    *now_ms* decides the day as well as the conversation id. It used to decide
    only the id, and the day came from the wall clock however explicit the
    caller had been — so passing a timestamp for determinism fixed half the
    result and left the other half moving. A test written on one day started
    failing at midnight, which is exactly the class of thing an explicit clock
    exists to prevent.
    """
    try:
        tz = ZoneInfo(timezone) if timezone else None
    except Exception:
        # An unknown or missing tzdata entry must not stop a job delivering.
        tz = None
    if now_ms is None:
        return datetime.now(tz) if tz else datetime.now()
    return datetime.fromtimestamp(now_ms / 1000, tz) if tz \
        else datetime.fromtimestamp(now_ms / 1000)


def retarget_for_delivery(
    chat_id: str | None,
    timezone: str | None = None,
    now_ms: int | None = None,
) -> str | None:
    """``homeweb:<user>:<old-day>[:<conv>]`` -> ``homeweb:<user>:<today>:<now>``.

    Anything that is not a HomeCore chat_id is returned untouched — other
    channels own their own addressing and must not be rewritten.
    """
    if not chat_id or not chat_id.startswith(HOMEWEB_PREFIX):
        return chat_id
    parts = chat_id.split(":")
    if len(parts) < 3 or not parts[1]:
        return chat_id  # malformed: leave it alone rather than invent a target
    day = _now(timezone, now_ms).strftime("%Y-%m-%d")
    scope = parts[3] if len(parts) > 3 else ""
    if _is_space_scope(scope):
        # A *space* is not a conversation. HomeCore's Profesiones (Finanzas,
        # Programmer, Teacher, Designer) each hold a whole chat of their own,
        # and the scope is how a reply finds its way back into one. A reminder
        # someone set while talking to the Profesor belongs in the Profesor's
        # chat, answered by the Profesor — replacing the scope with a bare
        # conversation id delivers it into the ordinary chat instead, where a
        # different Alfred picks it up.
        #
        # The scope is kept and everything after it is dropped: a 5th segment,
        # when there is one, names a conversation from weeks ago and is exactly
        # what this function exists to stop a job inheriting. Landing without
        # one, the reply is stored unstamped and joins whatever conversation is
        # open in that profession — the same treatment ev-* gets in the ordinary
        # chat, and for the same reason.
        return f"{parts[0]}:{parts[1]}:{day}:{scope}"
    conv = int(now_ms if now_ms is not None else time.time() * 1000)
    return f"{parts[0]}:{parts[1]}:{day}:{conv}"


def _is_space_scope(segment: str) -> bool:
    """True for a fourth segment that names a HomeCore *space*.

    Three things can occupy it, and they are told apart by shape rather than by
    a list, because the list lives in HomeCore and nothing here should have to
    track it:

    - all digits — a conversation id. Replaced: it is weeks old.
    - ``ev-*`` — a machine-event session (relayed notifications, geofences).
      Also replaced: those are stored unstamped on purpose so they inherit the
      conversation on screen, which is the behaviour this function exists to
      stop a job inheriting.
    - anything else — a space scope (``fin``, ``dev``, ``edu``, ``dsg``). Kept.
    """
    return bool(segment) and not segment.isdigit() and not segment.startswith("ev-")


def new_conversation_chat_id(
    user_id: str,
    timezone: str | None = None,
    now_ms: int | None = None,
) -> str:
    """``homeweb:<user>:<today>:<now>`` built from scratch, not rewritten.

    For anything that knows *who* it is writing to (its own
    ``HOMECORE_USER_ID``) rather than carrying a chat_id from the past — which
    is the only reason ``retarget_for_delivery`` above has to exist.
    """
    now = _now(timezone, now_ms)
    conv = int(now_ms if now_ms is not None else time.time() * 1000)
    return f"{HOMEWEB_PREFIX}{user_id}:{now.strftime('%Y-%m-%d')}:{conv}"


def is_space_session(chat_id: str | None) -> bool:
    """True when *chat_id* names a conversation inside a HomeCore *space*.

    ``homeweb:<user>:<day>:<scope>[:<conv>]`` where the scope names a
    profession — the same fourth-segment test `retarget_for_delivery` uses, so
    the two cannot drift into disagreeing about what a space is.

    Used to keep a profession off the fast model even on turns nobody asked for
    interactively (a cron delivery, a job finishing), which arrive with no
    profile because only HomeCore's chat requests carry one.

    ``dlg-*`` counts. It is one profession working for another, running that
    profession's rules — the one case where the fast model would be least
    visible and most wrong, since what comes back is presented as the caller's
    own work.
    """
    if not chat_id or not chat_id.startswith(HOMEWEB_PREFIX):
        return False
    parts = chat_id.split(":")
    return len(parts) > 3 and _is_space_scope(parts[3])


def is_ask_session(chat_id: str | None) -> bool:
    """True when *chat_id* is another member's question, not this user's turn.

    ``homeweb:<user>:<day>:ev-ask-<asker>`` — HomeCore's /chat/ask-family. The
    text in one of these was written by a different account and interpolated
    into a prompt, so it can always forge instructions that outrank the frame
    telling it to answer read-only. What it must not be able to do is reach a
    tool or a skill action that writes: the turn runs under THIS user's proxy
    token, so a non-admin's question landing in an admin's instance would
    otherwise carry admin privileges (adjust_points, delete_chore).

    Accepts any session whose key ends up here — an unknown scope is treated as
    a normal turn, so this has to match the ev-ask prefix and nothing else.
    """
    if not chat_id:
        return False
    # The session key is the chat id with a channel prefix (websocket:…), so
    # match on the segment rather than the position.
    return any(part.startswith("ev-ask") for part in chat_id.split(":"))


def is_whatsapp_session(chat_id: str | None) -> bool:
    """True for a turn driven by an incoming WhatsApp message from somebody else.

    Distinct from `is_third_party_session` because the two third parties are not
    the same size of stranger. An ``ev-ask`` turn comes from one of five people
    who live here; a ``whatsapp`` one comes from anybody with the number,
    including whoever else is in a group. They get different allowlists.

    ``whatsapp-own`` is the owner and is not third-party at all, so it is not
    matched here either — the segment is compared whole for that reason.
    """
    if not chat_id:
        return False
    return any(part == "whatsapp" for part in chat_id.split(":"))


def is_third_party_session(chat_id: str | None) -> bool:
    """True when the words driving this turn were written by someone who is not
    the owner of this instance.

    Two sources, one hazard. ``ev-ask`` is another family member's question.
    ``whatsapp`` is anybody at all — a contact, a stranger in a group, whatever
    a forwarded chain says — and it is by far the larger surface: the family is
    five people who mostly mean well, WhatsApp is everyone else.

    The consequence is identical either way, and it is the one
    ``is_ask_session`` spells out: the turn runs under THIS user's proxy token,
    so text that can forge instructions would be forging them with Alex's admin
    rights. Reads are fine. Writes are not.

    Matching a bare ``whatsapp`` segment is deliberate: the session key is
    ``<channel>:<chat_id>`` unless overridden, so the channel's own name is
    already the first segment and this holds even if the override in
    ``WhatsAppChannel`` is ever removed.

    ``whatsapp-own`` is the exception and the only one: WhatsApp marks a message
    ``fromMe`` when the linked account itself wrote it, which nothing else can
    claim. That is the owner asking his own assistant a question from his own
    phone, and holding *him* to a stranger's allowlist would mean the one person
    the account belongs to is the one person who cannot use it. It is a distinct
    segment rather than an absent one so that the permissive case has to be
    spelled out — a typo in the restrictive prefix should fail closed.
    """
    if not chat_id:
        return False
    if is_ask_session(chat_id):
        return True
    return any(part == "whatsapp" for part in chat_id.split(":"))
