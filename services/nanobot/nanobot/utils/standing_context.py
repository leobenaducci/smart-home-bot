"""Text a caller re-sends every turn, and that must not pile up in history.

Some callers prepend a standing instruction to each user message. HomeCore does
it for every turn inside a Profesión: the persona for that space, re-sent every
time on purpose, because a session that grows past the context window is
consolidated and an instruction living only in the first message is exactly what
a summary drops.

The catch is that the user turn is persisted **verbatim** and replayed in every
later prompt. So a 1 200-token persona re-sent for thirty turns is not 1 200
tokens — it is thirty copies, 37 000 tokens, over half the window, all of them
identical. Measured on the Programador space: turn 30 carried 37 050 tokens of
the same paragraph. Worse, it is self-defeating — the duplication is what drives
the session into the consolidation the re-sending exists to survive.

The fix is to mark the region. The model still sees it on every single turn, in
full and unmarked; the *session* keeps only the newest copy, because each turn
stores the message without it. Two functions, deliberately not one:

- `for_prompt` removes the markers and keeps the text — what the model reads.
- `for_history` removes the marked region entirely — what gets stored.

Both are total: text with no markers passes through unchanged, so any caller
that does not use this is unaffected.
"""

from __future__ import annotations

import re

# On their own lines, and bracketed enough to be implausible in ordinary Spanish
# or in a pasted document. They never reach the model: `for_prompt` strips them.
OPEN = "[[[standing-context]]]"
CLOSE = "[[[/standing-context]]]"

# DOTALL so a multi-paragraph persona matches; non-greedy so two blocks in one
# message stay two blocks rather than swallowing everything between them.
_REGION = re.compile(
    rf"[ \t]*{re.escape(OPEN)}[ \t]*\n?(.*?)\n?[ \t]*{re.escape(CLOSE)}[ \t]*\n?",
    re.DOTALL,
)
# A marker that owns its line takes the line with it; one sitting inside a
# sentence takes only itself. Both cases have to be handled, and separately:
# a single rule that also ate the padding turned "antes MARK despues" into
# "antesdespues" — two words silently welded, in a message the person typed.
_MARKER_LINE = re.compile(
    rf"^[ \t]*(?:{re.escape(OPEN)}|{re.escape(CLOSE)})[ \t]*\n?", re.MULTILINE
)
_MARKER_INLINE = re.compile(rf"(?:{re.escape(OPEN)}|{re.escape(CLOSE)})")


def wrap(block: str) -> str:
    """Mark *block* as standing context. Used by callers that build the block."""
    return f"{OPEN}\n{block}\n{CLOSE}"


def strip_markers(text: str | None) -> str:
    """Remove every marker, keeping the text around it.

    Callers use it on a *user's* message before adding their own block: without
    it, someone could wrap part of what they wrote and have it silently vanish
    from their history. `for_prompt` is the same operation seen from the other
    end, so it delegates here.
    """
    return _MARKER_INLINE.sub("", _MARKER_LINE.sub("", text or ""))


def extract(text: str | None) -> str:
    """The standing block itself, markers and all, or '' if there is none.

    Callers keep the last one they saw so a turn that arrives *without* one can
    still be answered in the right voice. That happens whenever something other
    than the person starts a turn — a finished background task being phrased, a
    scheduled job firing — because those carry no block of their own, and since
    the block is no longer stored they can no longer pick it up from history.

    Returned wrapped rather than bare, so re-attaching it is `f"{block}\\n\\n{rest}"`
    and the result goes through exactly the same strip on the way out.
    """
    if not text or OPEN not in text:
        return ""
    m = _REGION.search(text)
    return wrap(m.group(1)) if m else ""


def for_prompt(text: str | None) -> str:
    """The text as the model should read it: markers gone, content intact.

    An unbalanced marker — a truncated message, or one a person typed by hand —
    is dropped as a stray line rather than being allowed to swallow the rest of
    the message. Losing a marker must never lose the question next to it.
    """
    return strip_markers(text)


def for_history(text: str | None) -> str:
    """The text as the session should store it: the marked region removed.

    A message that is *only* standing context stores as empty, which is correct
    — there was nothing the user said to remember.
    """
    if not text or OPEN not in text:
        # No opening marker means nothing to drop, only stray markers to clean,
        # so history never shows one either way.
        return strip_markers(text)
    # lstrip newlines rather than .strip(): the blank line the wrapper left is
    # noise, but the indentation of a pasted code block is the person's text.
    return strip_markers(_REGION.sub("", text)).lstrip("\n").rstrip()
