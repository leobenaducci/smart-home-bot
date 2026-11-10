"""Runtime-specific helper functions and constants."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from loguru import logger

from nanobot.utils.helpers import stringify_text_blocks

_MAX_REPEAT_EXTERNAL_LOOKUPS = 2
_MAX_CONSECUTIVE_EXEC_ERRORS = 3

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


# A final reply that only says what the model is about to do. A reply with no
# tool call ends the turn, so the sentence becomes the whole answer: on
# 2026-11-09 and 11-10 the morning greeting was delivered as "Let me get a
# forecast for low/high and rain." and "Let me get the weather.", with nothing
# looked up. The model is asked once to act or answer instead.
ANNOUNCED_STEP_PROMPT = (
    "[Your last reply ended by saying what you were about to do, with no tool "
    "call beside it, so that step has not been done by that reply. If it needs "
    "doing now, make the call now. If it is already done, or it is meant for "
    "later or waits on the person, reply with the final answer exactly as they "
    "should read it.]"
)

_ANNOUNCED_STEP_MAX_CHARS = 240
_ACTION_VERBS_EN = (
    "get|check|look|fetch|find|search|pull|grab|try|see|read|open|run|call|use|"
    "query|calculate|compute|write|create|make|send|set|turn|load|list|verify|"
    "confirm|review|inspect|retrieve|gather|figure"
)
_ACTION_VERBS_ES = (
    "buscar|revisar|consultar|ver|mirar|obtener|averiguar|leer|abrir|ejecutar|"
    "correr|chequear|comprobar|verificar|calcular|crear|escribir|enviar|mandar|"
    "preparar|traer|sacar|cargar|listar|probar|intentar|fijarme|pedir|armar"
)
_ANNOUNCED_STEP_RES = (
    re.compile(
        r"^(?:[a-z ]{0,20}[,:]\s+|ok(?:ay)?\s+|now\s+|next\s+|first\s+|so\s+)?"
        r"(?:let me|i'll|i will|i'm going to|i am going to|going to)\s+"
        r"(?:now\s+|also\s+|quickly\s+|first\s+)?(?:" + _ACTION_VERBS_EN + r")\b"
    ),
    re.compile(
        r"^(?:[a-z ]{0,20}[,:]\s+|ok\s+|ahora\s+|primero\s+|bueno\s+)?"
        r"(?:voy a|dejame|permiteme|vamos a|ahora voy a)\s+(?:" + _ACTION_VERBS_ES + r")\b"
    ),
)


# A promise about later, or one that waits on the person, is an answer: "I'll
# send you a message then", "once you confirm", "mañana a las 7". Nudging it
# would ask for the call now -- a second reminder, or a change a read-only plan
# step must not make.
_LATER_OR_CONDITIONAL_RE = re.compile(
    r"\b(?:then|later|tonight|tomorrow|tomorrow's|every|each|again|soon|"
    r"once|(?:when|if|after|until) (?:you|they|he|she|we)\b|at \d|in (?:a|an|\d+) (?:few )?(?:min|minute|hour|day|moment)|"
    r"confirm|go-ahead|approve|say (?:yes|so)|"
    r"luego|despues|mas tarde|esta noche|manana|cada|otra vez|de nuevo|pronto|"
    r"cuando|si (?:me|lo|la|vos|usted|queres|quieres|confirm)|a las \d|en (?:un|unos|\d+) |"
    r"confirm|aprob|autoriz)"
)


def announces_next_step(content: str | None) -> bool:
    """True when *content* is a short reply whose last sentence only announces
    an action ("Let me get the weather.", "Voy a buscar el pronóstico.").

    Narrow on purpose: a long reply, one that ends on a question, or one ending
    "let me know ..." is an answer, and asking again would cost a model call
    for nothing.
    """
    if is_blank_text(content):
        return False
    text = re.sub(r"[*_`]+", "", content).strip()
    if len(text) > _ANNOUNCED_STEP_MAX_CHARS or text.endswith("?"):
        return False
    sentences = [s.strip() for s in re.split(r"[.!\n…]+", text) if s.strip()]
    if not sentences:
        return False
    last = _fold(sentences[-1]).strip(" \t\"'*_-:,")
    if last.startswith("let me know") or _LATER_OR_CONDITIONAL_RE.search(last):
        return False
    return any(rx.search(last) for rx in _ANNOUNCED_STEP_RES)


def build_announced_step_message() -> dict[str, str]:
    """Ask the model to act on the step it announced, or answer."""
    return {"role": "user", "content": ANNOUNCED_STEP_PROMPT}


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


def repeated_exec_error(tool_name: str, counts: dict[str, int]) -> str | None:
    """Refuse further exec calls after consecutive exec failures in one turn.

    The streak is recorded by :func:`record_exec_outcome`. Once it reaches the
    threshold, every later exec call is refused so the model can answer with
    what it already knows; other tools keep working.
    """
    if tool_name != "exec":
        return None
    streak = counts.get("exec", 0)
    if streak < _MAX_CONSECUTIVE_EXEC_ERRORS:
        return None
    logger.warning(
        "Blocking exec after {} consecutive failures in this turn",
        streak,
    )
    return (
        "Error: exec failed several times in a row in this turn. "
        "Report the last error and answer with what you already know; "
        "do not keep trying shell workarounds."
    )


def record_exec_outcome(tool_name: str, failed: bool, counts: dict[str, int]) -> None:
    """Update the consecutive-exec-failure streak for this turn.

    A failure is what the runner treats as one (an exception, or a result
    starting with ``"Error"``). A command that exits non-zero comes back as
    output plus ``Exit code: N`` and is not a failure.
    """
    if tool_name != "exec":
        return
    if failed:
        counts["exec"] = counts.get("exec", 0) + 1
    else:
        counts["exec"] = 0


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
