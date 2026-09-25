"""Runtime-specific helper functions and constants."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from loguru import logger

from nanobot.utils.helpers import stringify_text_blocks

_MAX_REPEAT_EXTERNAL_LOOKUPS = 2

EMPTY_FINAL_RESPONSE_MESSAGE = (
    "I completed the tool steps but couldn't produce a final answer. "
    "Please try again or narrow the task."
)

FINALIZATION_RETRY_PROMPT = (
    "Please provide your response to the user based on the conversation above."
)

LENGTH_RECOVERY_PROMPT = (
    "Output limit reached. Continue exactly where you left off "
    "— no recap, no apology. Break remaining work into smaller steps if needed."
)


def empty_tool_result_message(tool_name: str) -> str:
    """Short prompt-safe marker for tools that completed without visible output."""
    return f"({tool_name} completed with no output)"


def ensure_nonempty_tool_result(tool_name: str, content: Any) -> Any:
    """Replace semantically empty tool results with a short marker string."""
    if content is None:
        return empty_tool_result_message(tool_name)
    if isinstance(content, str) and not content.strip():
        return empty_tool_result_message(tool_name)
    if isinstance(content, list):
        if not content:
            return empty_tool_result_message(tool_name)
        text_payload = stringify_text_blocks(content)
        if text_payload is not None and not text_payload.strip():
            return empty_tool_result_message(tool_name)
    return content


def is_blank_text(content: str | None) -> bool:
    """True when *content* is missing or only whitespace."""
    return content is None or not content.strip()


def build_finalization_retry_message() -> dict[str, str]:
    """A short no-tools-allowed prompt for final answer recovery."""
    return {"role": "user", "content": FINALIZATION_RETRY_PROMPT}


def build_length_recovery_message() -> dict[str, str]:
    """Prompt the model to continue after hitting output token limit."""
    return {"role": "user", "content": LENGTH_RECOVERY_PROMPT}


def external_lookup_signature(tool_name: str, arguments: dict[str, Any]) -> str | None:
    """Stable signature for repeated external lookups we want to throttle."""
    if tool_name == "web_fetch":
        url = str(arguments.get("url") or "").strip()
        if url:
            return f"web_fetch:{url.lower()}"
    if tool_name == "web_search":
        query = str(arguments.get("query") or arguments.get("search_term") or "").strip()
        if query:
            return f"web_search:{query.lower()}"
    return None


def repeated_external_lookup_error(
    tool_name: str,
    arguments: dict[str, Any],
    seen_counts: dict[str, int],
) -> str | None:
    """Block repeated external lookups after a small retry budget."""
    signature = external_lookup_signature(tool_name, arguments)
    if signature is None:
        return None
    count = seen_counts.get(signature, 0) + 1
    seen_counts[signature] = count
    if count <= _MAX_REPEAT_EXTERNAL_LOOKUPS:
        return None
    logger.warning(
        "Blocking repeated external lookup {} on attempt {}",
        signature[:160],
        count,
    )
    return (
        "Error: repeated external lookup blocked. "
        "Use the results you already have to answer, or try a meaningfully different source."
    )


# --- items nobody asked for ---------------------------------------------------
#
# 10 Sep 2026, ornith-1.5:9b, asked "¿qué tareas tengo pendientes para hoy?".
# Unsure whether Home Assistant was answering, it tested the connection the way
# a developer would: HassListAddItem("test connectivity"), then
# HassListAddItem("connectivity probe", "[Read] Shopping List"). Neither landed
# only because it named lists that do not exist. The family's shopping list is
# not a scratch pad, and a read-only question must not leave writes behind.
#
# The rule is narrow on purpose: an item added to a list has to share a word
# with the conversation -- what anyone said, including Alfred's own earlier
# replies, so "agregá todo eso" after a recipe still works. It refuses the
# invented item, not the feature.
_LIST_ADD_TOOL_SUFFIXES = ("HassListAddItem", "todo_add_item")
_ITEM_WORD_RE = re.compile(r"\w{3,}", re.UNICODE)
_ITEM_STOPWORDS = frozenset((
    "the", "and", "for", "with", "con", "los", "las", "del", "una", "uno",
    "por", "que", "para", "mas", "sin",
))


def _fold(text: str) -> str:
    """Casefolded, without accents: "Jamón" and "jamon" are the same word."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def conversation_text(messages: list[dict[str, Any]]) -> str:
    """What was said in *messages*: user and assistant text, nothing else."""
    parts = []
    for message in messages or []:
        if message.get("role") not in ("user", "assistant"):
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            # Text blocks only; a photo beside them is not something said.
            parts.extend(b["text"] for b in content
                         if isinstance(b, dict) and isinstance(b.get("text"), str))
    return "\n".join(parts)


def _mentioned(word: str, folded_conversation: str) -> bool:
    if word in folded_conversation:
        return True
    # Plurals and diminutives: "huevos" for a person who said "huevo",
    # "tomates" for "tomate". Only on long words, so a four-letter stem never
    # matches half the dictionary.
    return len(word) >= 6 and word[:-2] in folded_conversation


def unrequested_list_item_error(
    tool_name: str,
    arguments: dict[str, Any],
    messages: list[dict[str, Any]],
) -> str | None:
    """Refuse adding a list item that shares no word with the conversation."""
    if not str(tool_name or "").endswith(_LIST_ADD_TOOL_SUFFIXES):
        return None
    item = str((arguments or {}).get("item") or (arguments or {}).get("summary") or "").strip()
    words = {w for w in _ITEM_WORD_RE.findall(_fold(item))} - _ITEM_STOPWORDS
    if not words:
        return None                         # "té", "sal": too short to judge
    conversation = _fold(conversation_text(messages))
    if any(_mentioned(w, conversation) for w in words):
        return None
    logger.warning("Refused list item nobody asked for: {} {!r}", tool_name, item[:80])
    return (
        f"Error: nobody in this conversation asked to add \"{item}\" to a list, so it "
        "was not added. Add only what the person named. To check that a list is "
        "reachable, read it -- never write a test item into the family's lists."
    )
