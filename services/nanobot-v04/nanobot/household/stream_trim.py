"""What the reader should end up with after a streamed segment (household A14, fork ``_trim_to``).

The deltas a chat page paints are the model's raw text. The reply that is
kept has a skill-invocation block (and any think block) stripped, so the fork
sent the kept version on the segment's ``stream_end`` as ``trim_to`` and
HomeCore's chat page repaints the bubble with it. Without it a camera turn
left a dangling fenced JSON block on screen.

The path: ``progress_hook.on_stream_end`` puts it on the ``StreamEndEvent``;
``turn_delivery`` copies it into the message metadata as ``_trim_to``; the
channel manager keeps it when it merges a delta into the end; the websocket
frame carries it as ``trim_to``.
"""

from __future__ import annotations

from typing import Any

METADATA_KEY = "_trim_to"


def trim_to(response: Any) -> str | None:
    """The kept text of *response*, "" for a reply with nothing to show, None to keep the painting."""
    from nanobot.household.skill_roots import household_enabled

    content = getattr(response, "content", None)
    if not household_enabled() or content is None:
        return None
    from nanobot.household.text_calls import _strip_skill_invocation_text
    from nanobot.utils.helpers import strip_think

    thought = strip_think(content)
    return _strip_skill_invocation_text(thought) if thought else ""


__all__ = ["METADATA_KEY", "trim_to"]
