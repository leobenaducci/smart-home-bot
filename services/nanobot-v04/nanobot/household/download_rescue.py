"""household: deliver a file the reply announced and then left out (fork loop.py).

Models announce an attachment and omit it ("here you go" and nothing after).
The fork checked every turn's tool results for ``download:`` links (and files
written to ``media/``) that the text the reader saw never carried, and sent
each as a message of its own. v0.4.1 has no such step. The functions below are
the fork's, verbatim; ``undelivered_for_turn`` reads them off a v0.4.1 turn.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from loguru import logger

_DOWNLOAD_LINK_RE = re.compile(
    r"\[[^\]\n]*\]\((?:<download:[^>\n]+>|download:[^)\n]+)\)")


# A skill saying this result is a side effect, not something handed to the
# reader. Matched in the serialised result rather than parsed, like everything
# else here: the tool content is text by the time it reaches this function.


_NOT_A_DELIVERY_RE = re.compile(r'"deliver"\s*:\s*(?:false|False)')


def _link_target(link: str) -> str:
    """The path out of a rescued link, in either markdown form."""
    inside = link.rsplit("](", 1)[-1].rstrip(")").strip()
    if inside.startswith("<") and inside.endswith(">"):
        inside = inside[1:-1]
    return inside.split("download:", 1)[-1]
# Fallback signal: the file itself. A skill that improvises its own code — and
# the model does improvise, badly — often prints just the path it wrote, with no
# link to lift. workspace/media exists solely to hold things the chat will show,
# so a file landing there is the same intent expressed one layer down.
#
# Documents, not only images. Asked for the insurance policy, the model wrote
# its own Python instead of calling the skill, saved a real PDF into media/ and
# announced "I sent them the file" having sent nothing — and this rescue skipped
# it because a .pdf is not a .jpg. The reader cannot tell a document that never
# arrived from a photo that never arrived.
# The name, which is the part that was wrong. It was `[\w.\-]+`, and files
# here can carry spaces and accents: "media/Sheet 1 — Cover.png" matched nothing, so
# a picture the model announced and never attached went unrescued. An accented
# name *without* a space did match (`\w` is Unicode), which is what kept the
# gap hidden.
#
# Spaces are allowed, but never two together and never one just before the
# extension — a filename has neither, and that is exactly what stops this from
# reading "media/a.txt es un .png" out of a sentence as one path.


_MEDIA_NAME = r"(?:[^\s/\n\"'<>|]|[ ](?![ .]))+?"


_MEDIA_IMAGE_RE = re.compile(
    r"\bmedia/" + _MEDIA_NAME + r"\.(?:jpg|jpeg|png|gif|webp"
    r"|pdf|docx|doc|xlsx|xls|pptx|csv|txt|md|zip"
    r"|mp3|m4a|ogg|wav|mp4|mov)\b",
    re.IGNORECASE,
)
# These enumerate the workspace instead of producing something for the reader;
# a listing that happens to mention media/ is not an undelivered photo.


_LISTING_TOOLS = frozenset({"list_dir", "glob", "grep", "read_file"})


_MAX_RESCUED_LINKS = 4
# Trailing epoch stamp skills append to keep filenames unique, e.g.
# cam_front_1785263359.jpg. Stripping it is what makes two captures of the same
# camera recognisable as the same subject.


_TIMESTAMP_SUFFIX_RE = re.compile(r"[_-]\d{9,}$")


def _undelivered_download_links(
    messages: list, final_content: str | None, turn_start: int = 0,
) -> list[str]:
    """Links a tool produced for the user that never made it into the reply.

    Models announce an attachment and then omit it — "here you go 👇" with
    nothing after the arrow. Camera snapshots were captured, saved and served
    correctly fifteen times running without a single one reaching the chat.
    Two prompt-shaped fixes failed: SKILL.md said to include the link verbatim,
    in bold, with a worked example, and the skill was changed to hand back a
    finished sentence with the link already in it. Neither survived contact with
    the model, so this stops asking and checks.

    Returns nothing when the reply already carries a link — the normal case,
    where the model did its job and this must not duplicate it.

    *turn_start* is where this turn's messages begin; everything before it is
    conversation history. Scanning history was a real bug in both directions: a
    request for the patio delivered a Front snapshot captured hours earlier,
    because that morning's tool output was still in the list and looked
    undelivered. In the other direction, an old assistant message carrying a
    link would suppress a rescue that this turn genuinely needed. Only what
    happened in this turn can be undelivered now.
    """
    if _DOWNLOAD_LINK_RE.search(final_content or ""):
        return []

    messages = messages[turn_start:]

    # The reply is not the only thing the reader sees. A streaming turn shows
    # every assistant segment, and the model often delivers the photo in one of
    # those and then signs off with a line that carries no link — which read as
    # undelivered here and sent the picture a second time. If any assistant
    # message in this turn already carried a link, the file arrived.
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and _DOWNLOAD_LINK_RE.search(content):
            return []

    tool_messages = [
        m for m in messages
        if m.get("role") == "tool" and isinstance(m.get("content"), str)
    ]

    seen: set[str] = set()
    links: list[str] = []

    def _add(link: str) -> None:
        if link not in seen:
            seen.add(link)
            links.append(link)

    for message in tool_messages:
        # A result that says it is not a delivery keeps its link out of this.
        # Saving, copying and moving all hand back a ready-made link now, so
        # the model can offer the file without writing a path from memory —
        # but "ordena mi carpeta, mueve los 6 PDF" is not six announcements,
        # and the rescue was posting a download button for each of them under
        # a reply that correctly said only "listo".
        if _NOT_A_DELIVERY_RE.search(message["content"]):
            continue
        for link in _DOWNLOAD_LINK_RE.findall(message["content"]):
            _add(link)

    # Only when nothing handed us a ready-made link. A skill that prints the
    # path it wrote and nothing else still produced a file for the reader; a
    # tool that merely lists the directory did not.
    if not links:
        for message in tool_messages:
            if message.get("name") in _LISTING_TOOLS:
                continue
            for path in _MEDIA_IMAGE_RE.findall(message["content"]):
                _add(f"[{Path(path).stem}](download:{path})")

    links = _newest_per_subject(links)

    if len(links) > _MAX_RESCUED_LINKS:
        logger.warning(
            "Delivering {} of {} undelivered files; a turn producing this many "
            "is usually a skill being called in a loop",
            _MAX_RESCUED_LINKS, len(links),
        )
        links = links[-_MAX_RESCUED_LINKS:]
    return links


def _newest_per_subject(links: list[str]) -> list[str]:
    """Collapse repeated captures of the same thing down to the latest one.

    The model calls snapshot more than once in a turn — listing cameras, second
    guessing the name, retrying — and each call writes a new timestamped file.
    Delivering all of them sent the family three identical photos of the front
    door. They are not three answers; they are one answer taken three times.

    Files are grouped by name with the trailing timestamp removed, so
    cam_front_1785263359.jpg and cam_front_1785263400.jpg collapse while
    cam_patio_… stays separate: asking for two different cameras still returns
    two pictures. Order follows the last capture of each subject, which is the
    freshest view.
    """
    newest: dict[str, str] = {}
    for link in links:
        target = _link_target(link)
        # The folder is part of the subject. Grouping on the bare name was
        # collision-free while every rescued file lived in the one flat media/
        # directory; with share paths, `user1/alfred/lista.md` and
        # `familia/lista.md` are two different files that both stem to "lista",
        # and one of them was silently dropped.
        stem = _TIMESTAMP_SUFFIX_RE.sub("", Path(target).stem) or target
        subject = f"{Path(target).parent}/{stem}"
        newest[subject] = link          # later capture wins
    if len(newest) < len(links):
        logger.info(
            "Collapsed {} captures into {} (same subject, newer file wins)",
            len(links), len(newest),
        )
    return list(newest.values())


# An earlier image is re-attached to a text turn only when the message actually
# refers to it (keeps vision routing "only when required").


def _message_tool_text(messages: list[dict[str, Any]]) -> str | None:
    """The last text this turn sent through the ``message`` tool, if any."""
    sent = None
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            if fn.get("name") != "message":
                continue
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    continue
            if isinstance(args, dict) and isinstance(args.get("content"), str):
                sent = args["content"]
    return sent


def undelivered_for_turn(ctx: Any) -> list[str]:
    """Links this v0.4.1 turn produced that the reader never saw.

    What the reader saw is the ``message`` tool's text when the turn used it
    (fork ``last_sent_content``), else the final reply. Only this turn's
    messages count: they start at the current input, the last of the messages
    the turn was built with (upstream's ``transcript_input.message_count``).
    """
    transcript = getattr(ctx, "transcript_input", None)
    messages = list(getattr(ctx, "all_messages", None) or [])
    if transcript is None or not messages:
        return []
    start = max(0, int(getattr(transcript, "message_count", 0) or 0) - 1)
    seen = _message_tool_text(messages[start:]) or ctx.final_content
    return _undelivered_download_links(messages, seen, turn_start=start)
