"""Shared execution loop for tool-using agents."""

from __future__ import annotations

import ast
import dataclasses
import asyncio
from dataclasses import dataclass, field
import inspect
import os
import re
import secrets
import shlex
import string
import time
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.utils.debug_log import log_error as _log_error
from nanobot.utils.prompt_templates import render_template
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.utils.profiling import PROFILER
from nanobot.utils.helpers import (
    build_assistant_message,
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    find_legal_message_start,
    maybe_persist_tool_result,
    truncate_text,
)
from nanobot.utils.runtime import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
    build_finalization_retry_message,
    build_length_recovery_message,
    ensure_nonempty_tool_result,
    is_blank_text,
    repeated_external_lookup_error,
    unrequested_list_item_error,
)

_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."
_PERSISTED_MODEL_ERROR_PLACEHOLDER = "[Assistant reply unavailable due to model error.]"
_MAX_EMPTY_RETRIES = 2
_MAX_LENGTH_RECOVERIES = 3
_MAX_INJECTIONS_PER_TURN = 3
_MAX_INJECTION_CYCLES = 5
_SNIP_SAFETY_BUFFER = 1024
_MICROCOMPACT_KEEP_RECENT = 10
_MICROCOMPACT_MIN_CHARS = 500
_COMPACTABLE_TOOLS = frozenset({
    "read_file", "exec", "grep", "glob",
    "web_search", "web_fetch", "list_dir",
})
_BACKFILL_CONTENT = "[Tool result unavailable — call was interrupted or lost]"

# Appended to a tool result when the same reply also carried a skill block that
# was stripped (see _resolve_block_beside_calls). The log line alone taught the
# model nothing: on 2026-09-22 a lights turn wrote {"skill": "lights", ...}
# beside an `exec` twenty times running, saw only the exec's output each time,
# never learned the skill had not run, and spent its five minutes probing Home
# Assistant with curl instead. This is the part it reads.
_STRIPPED_BLOCK_NOTE = (
    "[Not run: the skill-invocation block you wrote in that same reply was "
    "discarded, because a reply that calls a tool only runs the tool. Nothing "
    "the block asked for happened. To use a skill, write its block as the whole "
    "of a reply, with no tool call beside it.]"
)

# ---------------------------------------------------------------------------
# Text-based tool call parsing
# ---------------------------------------------------------------------------
# Some models write tool calls as plain text rather than structured objects.
# Supported formats:
#   - Python-style:    tool_name(key="value", ...)
#   - XML-style:       <tool_name attr="val" />
#   - Qwen3.5/Coder:   <function=name><parameter=key>val</parameter>...</function>
#   - Qwen3/Hermes:    <tool_call>{"name": "...", "arguments": {...}}</tool_call>
#   - JSON skill:      {"skill": "name", "action": "...", ...}
#   - Bash fence:      ```bash ... ``` (skill context only)
# The helpers below detect and parse those patterns so the runner can execute
# them exactly like native tool calls.

_TC_ID_CHARS = string.ascii_letters + string.digits


def _tc_id() -> str:
    return "".join(secrets.choice(_TC_ID_CHARS) for _ in range(9))


def _find_matching_paren(text: str, open_pos: int) -> int:
    """Return the index of the closing ')' that matches the '(' at *open_pos*.

    Correctly skips over string literals (single and double quotes with
    backslash escapes) and nested parentheses. Returns -1 if unmatched.
    """
    depth = 0
    i = open_pos
    in_str: str | None = None
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\" and i + 1 < len(text):
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in ('"', "'"):
            # Check for triple-quote strings
            triple = text[i:i+3]
            if triple in ('"""', "'''"):
                end = text.find(triple, i + 3)
                i = end + 3 if end != -1 else len(text)
                continue
            in_str = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _parse_call_kwargs(call_text: str) -> dict[str, Any] | None:
    """Parse keyword arguments from a tool-call string like ``name(k="v")``.

    Uses :mod:`ast` for accuracy; returns *None* on parse failure so the
    caller can decide whether to keep or drop the match.
    """
    try:
        tree = ast.parse(call_text.strip(), mode="eval")
    except SyntaxError:
        return None
    if not isinstance(tree.body, ast.Call):
        return None
    kwargs: dict[str, Any] = {}
    for kw in tree.body.keywords:
        if kw.arg is None:
            continue
        try:
            kwargs[kw.arg] = ast.literal_eval(kw.value)
        except Exception:
            # non-literal value – store the raw source fragment as a string
            src = ast.get_source_segment(call_text, kw.value)
            kwargs[kw.arg] = src if src is not None else ""
    # Also handle positional args if a tool accepts them by position
    for i, arg in enumerate(tree.body.args):
        try:
            kwargs[f"__pos_{i}"] = ast.literal_eval(arg)
        except Exception:
            pass
    return kwargs


def _parse_kwargs_regex(args_text: str) -> dict[str, Any]:
    """Fallback kwarg parser using regex when :func:`_parse_call_kwargs` fails."""
    kwargs: dict[str, Any] = {}
    # Match   key = "quoted"   or   key = 'quoted'
    for m in re.finditer(
        r'(\w+)\s*=\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')',
        args_text,
    ):
        key = m.group(1)
        raw = m.group(2)
        try:
            kwargs[key] = ast.literal_eval(raw)
        except Exception:
            kwargs[key] = raw[1:-1]  # strip surrounding quotes
    return kwargs


import base64 as _base64
import json as _json

from nanobot.agent.skill_invocation import (
    echoed_skill_invocation as _echoed_skill_invocation,
    register_skill_translation,
)
from nanobot.utils.homeweb_chat_id import is_third_party_session, is_whatsapp_session

# Parses the `- **name** — description  `path/SKILL.md`` lines from the system
# message back into a name → path map, which is what lets a {"skill": ...} block
# route to its SKILL.md.
#
# The middle is [^\n]* — anything but a newline, backticks included. It used to
# exclude backticks, so a description that formatted a word as `code` hid the
# path that followed it and the skill silently stopped routing: its invocation
# blocks were published to the chat as raw JSON instead of being executed. That
# cost camera-feed, geo and github. The trailing group is greedy-anchored, so a
# description that mentions `SKILL.md` in passing still yields the real path
# (the last such token on the line, which is the one the summary emits).
_SKILL_PATH_RE = re.compile(
    r"^\s*-\s+\*\*([^*\n]+)\*\*[^\n]*`([^`\n]*SKILL\.md[^`\n]*)`",
    re.MULTILINE | re.IGNORECASE,
)

_PYTHON_GUIDE_RE = re.compile(
    r"##\s+Python Translation Guide\s*\n([\s\S]+?)(?=\n##\s|\Z)",
    re.IGNORECASE,
)

# Module-level store mapping ToolCallRequest.id → original skill invocation JSON.
# Set by extract_json_skill_invocations; consumed (and cleared) by _maybe_translate_skill_calls.
_SKILL_INVOCATION_STORE: dict[str, dict] = {}


def _extract_python_guide(skill_content: str) -> str | None:
    """Extract the '## Python Translation Guide' section from a SKILL.md."""
    m = _PYTHON_GUIDE_RE.search(skill_content)
    if not m:
        return None
    guide = m.group(1).strip()
    fence = re.search(r"```(?:python)?\n([\s\S]+?)\n```", guide)
    if fence:
        guide = fence.group(1).strip()
    return guide if guide else None


# Keys that address the invocation itself rather than the function being
# called. Dropped from the call's kwargs — unless the function actually takes a
# parameter by that name, see _guide_signature_params.
_RESERVED_INVOCATION_KEYS = frozenset({"skill", "action", "name", "description"})


def _guide_signature_params(action: str, python_guide: str) -> frozenset[str]:
    """Parameter names of ``def <action>(...)`` in *python_guide*.

    Empty when the signature can't be read; callers use this only to rescue
    reserved keys, so "unreadable" and "takes none of them" behave alike.
    """
    m = re.search(
        rf"^def {re.escape(action)}\s*\(([\s\S]*?)\)\s*(?:->[^:]*)?:",
        python_guide,
        re.MULTILINE,
    )
    if not m:
        return frozenset()
    # Split on top-level commas only, so a default like ``x=(1, 2)`` stays whole.
    parts, depth, current = [], 0, ""
    for ch in m.group(1):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    names = (p.split("=")[0].split(":")[0].strip().lstrip("*") for p in parts)
    return frozenset(n for n in names if n.isidentifier())


def _guide_required_params(action: str, python_guide: str) -> tuple[list[str], bool]:
    """The parameters of ``def <action>(...)`` that have no default, in order,
    and whether it takes ``*args``/``**kwargs`` (then nothing is rebound)."""
    m = re.search(
        rf"^def {re.escape(action)}\s*\(([\s\S]*?)\)\s*(?:->[^:]*)?:",
        python_guide,
        re.MULTILINE,
    )
    if not m:
        return [], True
    required, variadic = [], False
    for part in (p.strip() for p in m.group(1).split(",")):
        if not part:
            continue
        if part.startswith("*"):
            variadic = True
            continue
        if "=" not in part:
            required.append(part.split(":")[0].strip())
    return required, variadic


# The two sentences this loop says to a person on its own, when a turn dead-ends
# -- every other word a household reads comes from the model, in their language.
# These were English only, so a Spanish-speaking household was told "I reached
# the maximum number of tool call iterations" mid-conversation, and the bench
# failed deepseek-v4-flash twice for "answered in English" on exactly that.
# The language is `SEARCH_LANGUAGE`, which the manifest fills from
# `locale.default` -- the household's language, not only the search one. A
# language with no entry here keeps the English, as the rest of the stack's i18n
# falls back to English. Worded without tú/vos/usted: Alfred's register differs
# from one person to the next, and these do not know who is reading.
_DEAD_ENDS: dict[str, dict[str, str]] = {
    "es": {
        "max_iterations": ("Llegué al límite de {max_iterations} pasos sin terminar la "
                           "tarea. Puedo seguir si la dividimos en partes más pequeñas."),
        "loop": ("Me detuve: estaba repitiendo la misma acción sin avanzar. Puedo "
                 "intentarlo de otra forma, o con una parte más pequeña de la tarea."),
    },
}


def _dead_end(kind: str, **fields) -> str | None:
    """The dead-end sentence in the household's language, or None for English."""
    lang = (os.environ.get("SEARCH_LANGUAGE") or "").strip().lower()[:2]
    text = _DEAD_ENDS.get(lang, {}).get(kind)
    return text.format(**fields) if text else None


def _split_call_in_action(invocation: dict, python_guide: str) -> dict | None:
    """``{"action": "turn_on(Oficina Tomi)"}`` as ``{"action": "turn_on",
    "device": "Oficina Tomi"}``.

    qwen3.5:9b writes the call into the action, arguments and all -- measured
    2026-09-24 against the lights skill: `toggle(Oficina Tomi)`,
    `get_state(Mac:B0C1D2E3)`. Arguments bind to the function's parameters in
    order, `key=value` and `key: value` by name when the key is a parameter;
    any argument that fits nowhere and the call is left alone.
    """
    m = re.fullmatch(r"\s*([A-Za-z_]\w*)\s*\((.*)\)\s*", str(invocation.get("action") or ""), re.S)
    if not m:
        return None
    action, inner = m.group(1), m.group(2).strip()
    sig = re.search(rf"^def {re.escape(action)}\s*\(([^)]*)\)", python_guide, re.MULTILINE)
    if not sig:
        return None
    order = [p.split("=")[0].split(":")[0].strip() for p in sig.group(1).split(",")]
    order = [p for p in order if p.isidentifier()]
    out = {k: v for k, v in invocation.items() if k != "action"}
    out["action"] = action
    free = [p for p in order if p not in out]
    for raw in (a.strip() for a in inner.split(",") if a.strip()) if inner else ():
        kv = re.match(r"([^=:]*?)\s*([=:])\s*(.*)", raw, re.S)
        key, sep, val = kv.groups() if kv else (raw, "", "")
        if sep and key.strip() in order and key.strip() not in out:
            name, value = key.strip(), val
        elif free:
            # `Mac:B0C1D2E3` -- a label the listing showed, not a parameter.
            name, value = free[0], (val if sep and key.strip() not in order else raw)
        else:
            return None
        if name in free:
            free.remove(name)
        value = value.strip().strip("'\"")
        out[name] = int(value) if value.isdigit() else value
    return out


def _static_skill_translation(invocation: dict, python_guide: str) -> str | None:
    """Deterministically translate a simple ``{skill, action, **kwargs}``
    invocation into Python: the guide's code followed by a printed call.

    Skips the stateless-LLM translation round-trip (seconds of latency) for
    the common case, and removes its main failure mode — the translator
    emitting the bare call without the guide's function definitions, which
    made every invocation die with NameError. Returns None when the action
    doesn't map to a function in the guide; the LLM fallback handles those.

    ``name`` and ``description`` are reserved *and* ordinary parameter names —
    ``add_grocery(name, ...)``, ``save_place(name, ...)``, ``add_prize(name,
    ..., description=None)``. Dropping them unconditionally emitted
    ``add_grocery()``, so "agrega yerba a la lista" died on a TypeError the
    model then retried forever. A reserved key survives when the function
    declares a parameter by that name; the skill-name alias never does,
    because extract_json_skill_invocations strips it before we get here.
    """
    action = invocation.get("action")
    if isinstance(action, str) and not action.isidentifier():
        invocation = _split_call_in_action(invocation, python_guide)
        action = invocation.get("action") if invocation else None
    if not isinstance(action, str) or not action.isidentifier():
        return None
    if not re.search(rf"^def {re.escape(action)}\s*\(", python_guide, re.MULTILINE):
        return None
    params = _guide_signature_params(action, python_guide)
    kwargs = {
        k: v for k, v in invocation.items()
        if k not in _RESERVED_INVOCATION_KEYS or k in params
    }
    # One argument under the wrong name. Models name the one thing a call is
    # about after what the listing showed them -- `name`, `light`, `mac` --
    # where the function says `device`. `name` is reserved and was dropped;
    # the others went through and died as unexpected keywords. Either way the
    # call failed on a TypeError the model read as "the skill cannot do it":
    # asked to test every light, qwen3.5:9b sent turn_on seven times with the
    # light as `name` and reported the lights skill broken (2026-09-24). With
    # exactly one required parameter missing and exactly one value left over,
    # the value is that parameter -- nothing else could be meant.
    required, variadic = _guide_required_params(action, python_guide)
    if not variadic:
        missing = [p for p in required if p not in kwargs]
        spare = [k for k in invocation
                 if k not in ("skill", "action") and k not in params]
        if len(missing) == 1 and len(spare) == 1:
            kwargs.pop(spare[0], None)
            kwargs[missing[0]] = invocation[spare[0]]
    if not all(isinstance(k, str) and k.isidentifier() for k in kwargs):
        return None
    # repr() of JSON-decoded values (str/int/float/bool/None/list/dict) is
    # valid Python source.
    args_src = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
    return (
        f"{python_guide}\n\n"
        f"import json as _json\n"
        f"print(_json.dumps({action}({args_src}), ensure_ascii=False, indent=2, default=str))"
    )


def _history_tool_calls(response: "LLMResponse") -> list[dict[str, Any]]:
    """The tool calls as the conversation keeps them.

    A translated skill call runs as `python -c "exec(base64(...))"` carrying
    the skill's whole Python guide, and that string stayed in the history as
    the call's arguments: base64 tokenises badly, so one lights call added
    ~16k tokens to every later request of the turn (deepseek 34k -> 49k, a
    Qwen3.8 plan step 9.7k -> 25.5k, 2026-09-24). What was asked is what the
    model needs to read back; the program is the runtime's business.
    """
    skills = getattr(response, "_skills_by_index", None) or {}
    invocations = getattr(response, "_skill_invocations", None) or {}
    out = []
    for i, tc in enumerate(response.tool_calls):
        call = tc.to_openai_tool_call()
        if i in skills:
            # As the model thinks of it: a call named after the skill, with
            # the action and its arguments. Recorded as `exec` with a comment,
            # Bonsai 2 copied that comment back as a shell command eighteen
            # times in one step (2026-09-25); a call named after a skill is
            # one the runner already turns back into the skill if it is copied
            # (_lift_skill_named_tool_calls).
            call["function"]["name"] = skills[i]
            call["function"]["arguments"] = _json.dumps(invocations.get(i, {}), ensure_ascii=False)
        out.append(call)
    return out


def _load_skill_python_guide(skill_md_path: str) -> str | None:
    """Load Python translation guide from SKILL_PYTHON.md next to the SKILL.md,
    falling back to an embedded '## Python Translation Guide' section."""
    skill_dir = Path(skill_md_path).parent
    python_guide_path = skill_dir / "SKILL_PYTHON.md"
    if python_guide_path.exists():
        try:
            content = python_guide_path.read_text(encoding="utf-8")
            fence = re.search(r"```(?:python)?\n([\s\S]+?)\n```", content)
            if fence:
                return fence.group(1).strip()
            return content.strip()
        except Exception:
            pass
    # Fallback: embedded section in SKILL.md
    try:
        skill_content = Path(skill_md_path).read_text(encoding="utf-8")
        return _extract_python_guide(skill_content)
    except Exception:
        return None


def _find_json_objects(text: str) -> list[tuple[int, str]]:
    """Extract all balanced {...} blocks from text. Returns (start, json_str) pairs."""
    results: list[tuple[int, str]] = []
    i = 0
    while i < len(text):
        if text[i] != "{":
            i += 1
            continue
        # Walk forward counting braces, skip string literals
        depth = 0
        j = i
        in_str = False
        escape = False
        while j < len(text):
            ch = text[j]
            if escape:
                escape = False
            elif in_str:
                if ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        results.append((i, text[i : j + 1]))
                        i = j + 1
                        break
            j += 1
        else:
            break
    return results


# Maps common action names to skill names — used when the model emits
# <function=skill action="search_documents"> without a skill identifier.
# Skill actions a turn driven by SOMEONE ELSE'S words may run — another
# member's question (`ev-ask`) or anyone at all over WhatsApp — answered under
# THIS user's credential (see is_third_party_session). An allowlist rather than
# a deny-list, because the cost of forgetting an entry differs by orders of
# magnitude in each direction — a missing read makes one question unanswerable
# and says so, a missing write is a family member's Alfred spending their
# points or deleting their chores at somebody else's request. WhatsApp widened
# who can reach this from "the other four people in this house" to "anyone with
# the number", which makes the allowlist shape matter more, not less.
#
# Reads only, and only of the shared household things the frame says are fair
# game. Not `download_document`/`get_document` — a question is not a way to
# pull someone's documents. Not `ask_family`, which is what would let two
# instances question each other in a loop.
#
# And emphatically NOT the `whatsapp` or `notifications` reads, even though
# they read rather than write. "Read" is not the test — "shared household
# thing" is. Whoever is typing in a WhatsApp group is the same category of
# stranger as the text they typed, and `search_messages` in this allowlist
# would mean any of them could ask Alfred what Sam wrote in the family group
# and be told. The chores list is the family's; the chats are not.
# What a read-only plan step may run: an action named as a read, and never one
# carrying a go-ahead (`find_in_ha` with confirmed=true switches lamps). A name
# that does not look like a read is refused -- the cost of a wrong refusal is
# one line to the planner, the cost of a wrong pass is the house's lights.
_READ_ACTION_PREFIXES = ("list", "get", "search", "show", "read", "find", "check",
                         "describe", "status", "lookup", "query", "my_", "room_from")


_GO_AHEAD_KEYS = ("confirmed", "apply", "confirm")


def _has_go_ahead(invocation: dict) -> bool:
    return any(invocation.get(k) in (True, "true", "yes", 1) for k in _GO_AHEAD_KEYS)


def _person_answered_pending(messages: list[dict]) -> bool:
    """Whether a skill answered `pending` and the person spoke after it.

    A skill that switches the house around asks first: its call without the
    go-ahead answers {"pending": true, "would_switch": [...]}. Only the person
    can give the go-ahead, and they give it by answering; a model that sends
    `confirmed: true` in the same turn gave it to itself -- deepseek did, for
    all eight bulbs, reasoning that "you asked to test" (2026-09-24).
    """
    pending_at = -1
    for i, m in enumerate(messages):
        if m.get("role") == "tool" and re.search(r'["\']pending["\']\s*:\s*(true|True)',
                                                 str(m.get("content") or "")):
            pending_at = i
    if pending_at < 0:
        return False
    return any(m.get("role") == "user" for m in messages[pending_at + 1:])


def is_read_action(invocation: dict) -> bool:
    action = str(invocation.get("action") or "").split("(")[0].strip().lower()
    if not action.startswith(_READ_ACTION_PREFIXES):
        return False
    return not _has_go_ahead(invocation)


_ASK_READABLE_ACTIONS: frozenset[str] = frozenset({
    "list_chores", "my_points", "list_prizes", "list_redemptions",
    "list_recurring_chores",
    "list_groceries",
    "list_menu",
    "list_places",
    "list_family", "get_profile", "search_family",
    "get_weather", "get_forecast", "get_conditions",
})

# What a turn driven by an incoming WhatsApp message may read — and it is a much
# shorter list than the one above, because the two third parties are not the same
# size of stranger. `ev-ask` is one of the five people who live here. WhatsApp is
# anybody with the number, including whoever else happens to be in a group.
#
# The list above was written for the first case and carried over to the second
# without being re-decided, which put the family's saved places — the home
# address, the colegio — and the family directory one "alfred, ..." away from a
# stranger in a group chat. Nothing personal survives here: not who lives here,
# not where, not what anybody owes or owns, not what is for dinner. The weather
# is the weather.
#
# Alfred can still talk. He simply cannot look anything up about this household
# on behalf of someone who is not in it.
_WHATSAPP_READABLE_ACTIONS: frozenset[str] = frozenset({
    "get_weather", "get_forecast", "get_conditions",
})

_ACTION_TO_SKILL: dict[str, str] = {
    # notifications (what the phone relayed — i.e. what people SENT the user)
    "search_notifications": "notifications", "list_notifications": "notifications",
    "reply_notification": "notifications",
    # whatsapp (the conversations themselves, not the on-screen alert)
    "search_messages": "whatsapp", "list_messages": "whatsapp",
    "list_chats": "whatsapp",
    # paperless
    "search_documents": "paperless", "list_documents": "paperless",
    "get_document": "paperless", "get_document_metadata": "paperless",
    "upload_document": "paperless", "update_document": "paperless",
    "delete_document": "paperless", "trash_document": "paperless",
    "list_tags": "paperless", "list_correspondents": "paperless",
    "list_document_types": "paperless", "list_tasks": "paperless",
    # weather
    "get_weather": "weather", "get_forecast": "weather",
    "get_conditions": "weather",
    # home-assistant (how the house is WIRED -- automations, devices,
    # entities. Turning things on and off is `lights` and the MCP
    # tools, which is why none of these names is a verb about a light.)
    "search_ha": "home-assistant", "get_entity": "home-assistant",
    "automations_for": "home-assistant",
    "list_automations": "home-assistant", "get_automation": "home-assistant",
    "describe_device": "home-assistant",
    "list_automation_backups": "home-assistant",
    "set_automation": "home-assistant", "restore_automation": "home-assistant",
    "enable_automation": "home-assistant",
    "disable_automation": "home-assistant",
    "reload_automations": "home-assistant",
    "list_ha_lights": "home-assistant", "rename_entity": "home-assistant",
    "rename_device": "home-assistant", "set_area": "home-assistant",
    "new_devices": "home-assistant", "device_triggers": "home-assistant",
    "listen_button": "home-assistant", "pair_zigbee": "home-assistant",
    "identify_device": "home-assistant", "create_area": "home-assistant",
    "create_automation": "home-assistant",
    "delete_automation": "home-assistant",
    # lights
    "list_lights": "lights", "turn_on": "lights",
    "turn_off": "lights", "set_brightness": "lights",
    "set_color": "lights", "get_state": "lights",
    "list_rooms": "lights", "room_on": "lights",
    "room_off": "lights", "room_toggle": "lights",
    "room_brightness": "lights", "room_color": "lights",
    "set_room": "lights", "rename_room": "lights", "rename_light": "lights",
    "toggle": "lights", "set_icon": "lights",
    "flash_light": "lights", "find_in_ha": "lights", "room_from_ha": "lights",
    # menu (minuta semanal: almuerzos y cenas)
    "list_menu": "menu", "set_menu": "menu", "clear_menu": "menu",
    "request_dish": "menu", "approve_dish": "menu", "reject_dish": "menu",
    "remove_menu_entry": "menu",
    # n8n
    "list_workflows": "n8n", "get_workflow": "n8n",
    "execute_workflow": "n8n", "activate_workflow": "n8n",
    "deactivate_workflow": "n8n", "list_executions": "n8n",
    # camera-feed
    "list_cameras": "camera-feed", "snapshot": "camera-feed",
    "stream_url": "camera-feed",
    # Managing the wall, not just reading it. Added 2026-09-20.
    "add_camera": "camera-feed", "remove_camera": "camera-feed",
    "rediscover_camera": "camera-feed",
    # file-share
    "list_files": "file-share", "download_file": "file-share",
    "upload_file": "file-share", "save_text": "file-share",
    "copy_file": "file-share", "move_file": "file-share",
    "make_folder": "file-share", "delete_file": "file-share",
    # family-message (Alfred → another member's Alfred)
    "send_family_message": "family-message", "send_message_to": "family-message",
    "ask_family": "family-message",
    # notifications (other apps' notifications relayed from the phone)
    "reply_notification": "notifications", "list_notifications": "notifications",
    # file-share (continued)
    "share_with": "file-share", "unshare": "file-share",
    "list_my_shares": "file-share", "list_shared_with_me": "file-share",
    "download_shared": "file-share",
    # chores (the skill was `tasks` until 2026-09-11; "*_chore" avoids paperless's list_tasks)
    "list_chores": "chores", "complete_chore": "chores",
    "revert_chore": "chores", "excuse_chore": "chores",
    "postpone_chore": "chores", "excuse_day": "chores", "my_points": "chores",
    "list_prizes": "chores", "redeem_prize": "chores",
    "list_redemptions": "chores",
    "add_chore": "chores", "edit_chore": "chores", "delete_chore": "chores",
    "add_recurring_chore": "chores", "edit_recurring_chore": "chores",
    "list_recurring_chores": "chores",
    "review_queue": "chores", "approve_chore": "chores",
    "reject_chore": "chores", "add_prize": "chores",
    "fulfill_redemption": "chores", "cancel_redemption": "chores",
    "adjust_points": "chores",
    # grocery (shared family shopping list)
    "list_groceries": "grocery", "add_grocery": "grocery",
    "request_grocery": "grocery", "mark_bought": "grocery",
    "remove_grocery": "grocery", "approve_grocery": "grocery",
    "reject_grocery": "grocery", "clear_bought": "grocery",
    # family (shared family directory / memory)
    "list_family": "family", "get_profile": "family",
    "search_family": "family", "set_fact": "family",
    "add_note": "family", "set_profile": "family",
    "remove_fact": "family", "remove_note": "family",
    # geo (saved places, location reminders, whereabouts, live location)
    "where_is": "geo", "whereabouts": "geo",
    "save_place": "geo", "list_places": "geo", "delete_place": "geo",
    "add_reminder": "geo", "list_reminders": "geo", "cancel_reminder": "geo",
    "get_location": "geo", "locate": "geo",
    "track": "geo", "stop_track": "geo", "track_status": "geo",
    "ring_phone": "geo", "stop_ring": "geo",
}


def _normalize_skill_name(name: str) -> str:
    """Normalize skill name to lower-case with hyphens (e.g. home_lights → home-lights)."""
    return name.strip().lower().replace("_", "-")


def _resolve_skill_name(raw: str, skill_paths: dict[str, str]) -> str | None:
    """Return the canonical skill path key for *raw*, trying hyphen/underscore
    variants and then a renamed skill's old name (`SKILL_ALIASES`)."""
    from nanobot.agent.skills import SKILL_ALIASES

    candidates = (raw.strip().lower(), _normalize_skill_name(raw))
    for candidate in candidates:
        if candidate in skill_paths:
            return candidate
    for candidate in candidates:
        renamed = SKILL_ALIASES.get(candidate)
        if renamed in skill_paths:
            return renamed
    return None


def _collect_skill_paths(messages: list[dict]) -> dict[str, str]:
    """Build a mapping {skill_name: skill_path} from the system message."""
    paths: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") != "system":
            continue
        body = msg.get("content", "")
        if not isinstance(body, str):
            continue
        for m in _SKILL_PATH_RE.finditer(body):
            paths[m.group(1).strip().lower()] = m.group(2).strip()
    return paths


def extract_json_skill_invocations(
    content: str,
    messages: list[dict],
    tool_names: frozenset[str],
) -> list[ToolCallRequest]:
    """Detect JSON action blocks that a model emits in place of real tool calls
    and convert them into ``read_file`` calls on the relevant skill's SKILL.md.

    Two patterns are handled:
    1. ``{"skill": "name", ...}`` — the skill name is embedded in the JSON.
    2. ``{"action": "...", ...}`` — the skill is inferred from the surrounding
       text (the model usually names the skill in the sentence before the JSON).

    The skill → path mapping is extracted from the Skills section of the
    system message.
    """
    if "read_file" not in tool_names:
        return []

    skill_paths = _collect_skill_paths(messages)
    if not skill_paths:
        return []

    results: list[tuple[int, ToolCallRequest]] = []
    seen_paths: set[str] = set()

    for start, json_str in _find_json_objects(content):
        try:
            data = _json.loads(json_str)
        except (_json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        # Only consider JSON that looks like a skill/action invocation.
        # Also accept OpenAI-style {"name": "<skill>", ...} when the name
        # matches a known skill (avoids false-positives on generic JSON).
        has_name_match = (
            "name" in data
            and isinstance(data["name"], str)
            and _resolve_skill_name(data["name"], skill_paths) is not None
        )
        if not any(k in data for k in ("skill", "action", "description")) and not has_name_match:
            continue
        m_start = start

        # 1. Explicit "skill" key, or OpenAI-style "name" key matching a known skill
        skill_name: str | None = None
        name_is_skill = False
        raw_skill = data.get("skill")
        if not isinstance(raw_skill, str):
            raw_name = data.get("name")
            if isinstance(raw_name, str) and _resolve_skill_name(raw_name, skill_paths) is not None:
                raw_skill = raw_name
                name_is_skill = True
        if isinstance(raw_skill, str):
            skill_name = _resolve_skill_name(raw_skill, skill_paths)

        # 2. Infer from text preceding this JSON block
        if not skill_name or skill_name not in skill_paths:
            preceding = content[:m_start].lower()
            for name in skill_paths:
                if name in preceding or name.replace("-", "_") in preceding:
                    skill_name = name
                    break

        if not skill_name:
            continue
        path = skill_paths.get(skill_name)
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        tc_id = _tc_id()

        # If the invocation carries action args and a Python guide exists, emit a
        # __skill_translate pseudo-call instead of read_file. This bypasses dedup
        # (read_file may already be in previously_called from an earlier read of the
        # same SKILL.md) and keeps Python internals away from the main model.
        has_action = any(k not in ("skill", "name", "description") for k in data)
        if has_action and _load_skill_python_guide(path):
            # Hand the resolved skill down under "skill", so downstream never has
            # to re-guess whether "name" addressed the skill or the function. When
            # it was the skill's own name it is dropped here; otherwise it stays,
            # and it is an argument like any other (add_grocery(name=...)).
            invocation = dict(data)
            if name_is_skill:
                invocation.pop("name", None)
            invocation["skill"] = skill_name
            results.append((m_start, ToolCallRequest(
                id=tc_id,
                name="__skill_translate",
                arguments={"path": path, "invocation": invocation},
            )))
        else:
            results.append((m_start, ToolCallRequest(
                id=tc_id,
                name="read_file",
                arguments={"path": path},
            )))

    results.sort(key=lambda x: x[0])
    return [tc for _, tc in results]


_BASH_FENCE_RE = re.compile(
    r"```(?:bash|sh|shell|cmd|zsh)\s*\n([\s\S]+?)```",
    re.IGNORECASE,
)

# Matches <tool_name attr="val" .../> or <tool_name attr="val" ...>
_XML_TOOL_CALL_RE = re.compile(
    r"<([A-Za-z][A-Za-z0-9_-]*)\s+((?:[^>\"'\\]|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')+?)\s*/?>"
)
_XML_ATTR_RE = re.compile(
    r'(\w+)\s*=\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')'
)

# Matches <function=TOOL_NAME>...</function> (Qwen3.5/Qwen3-Coder style)
_QWEN35_FUNC_RE = re.compile(
    r"<function=([A-Za-z][A-Za-z0-9_-]*)>\s*([\s\S]*?)\s*</function>",
    re.IGNORECASE,
)
# Matches <parameter=KEY>VALUE</parameter> inside a Qwen3.5 function block
_QWEN35_PARAM_RE = re.compile(
    r"<parameter=(\w+)>\s*([\s\S]*?)\s*</parameter>",
    re.IGNORECASE,
)

# DeepSeek's own tool-call markup ("DSML"), written as text instead of sent as a
# structured call. deepseek-v4-flash ended the morning greeting of 2026-09-26
# with it, and the markup was published verbatim in two people's chats:
#   <｜DSML｜tool_calls>
#   <｜DSML｜invoke name="weather">
#   <｜DSML｜parameter name="action" string="true">get_forecast</｜DSML｜parameter>
#   </｜DSML｜invoke>
#   </｜DSML｜tool_calls>
# It carries what the Qwen3.5 format carries, so it is rewritten into that one
# and every Qwen path -- a tool, a skill by name, the stripping -- applies.
_DSML = r"<\s*/?\s*[｜|]\s*DSML\s*[｜|]\s*"
_DSML_REWRITES = (
    (re.compile(_DSML.replace("/?", "") + r"invoke\s+name\s*=\s*\"([^\"]+)\"\s*>", re.I), r"<function=\1>"),
    (re.compile(r"<\s*/\s*[｜|]\s*DSML\s*[｜|]\s*invoke\s*>", re.I), "</function>"),
    (re.compile(_DSML.replace("/?", "") + r"parameter\s+name\s*=\s*\"(\w+)\"[^>]*>", re.I), r"<parameter=\1>"),
    (re.compile(r"<\s*/\s*[｜|]\s*DSML\s*[｜|]\s*parameter\s*>", re.I), "</parameter>"),
    (re.compile(_DSML + r"(?:tool_calls|function_calls)\s*>", re.I), ""),
)


def _dsml_as_qwen35(text: str) -> str:
    """DeepSeek's DSML tool-call markup rewritten as the Qwen3.5 format."""
    if not text or "DSML" not in text:
        return text
    for rx, repl in _DSML_REWRITES:
        text = rx.sub(repl, text)
    return text


# Matches <tool_call>{"name": "...", "arguments": {...}}</tool_call> (Qwen3/Hermes style)
_HERMES_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*([\s\S]*?)\s*</tool_call>",
    re.IGNORECASE,
)

# Commands that have dedicated nanobot tools — skip converting these to exec()
_SKIP_BASH_CMDS = re.compile(
    r"^\s*(?:ls|dir|cat|head|tail|cd|pwd|echo|cp|mv|rm|mkdir|rmdir|touch)\s",
    re.IGNORECASE,
)


def _stable_args_key(arguments: dict[str, Any]) -> str:
    """Stable string key for a tool call's arguments dict."""
    try:
        return _json.dumps(arguments, sort_keys=True, ensure_ascii=False)
    except Exception:
        return str(sorted(arguments.items()))


def _collect_previous_calls(messages: list[dict]) -> set[tuple[str, str]]:
    """Return the set of (tool_name, args_key) pairs already called in the *current turn*.

    Only scans messages after the last user message so that cross-turn
    deduplication doesn't block the model from re-reading a skill file
    in a new conversation turn.
    """
    last_user_idx = -1
    for idx, msg in enumerate(messages):
        if msg.get("role") == "user":
            last_user_idx = idx
    current_turn = messages[last_user_idx + 1:] if last_user_idx >= 0 else messages
    called: set[tuple[str, str]] = set()
    for msg in current_turn:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls", []):
            func = tc.get("function", {})
            name = func.get("name", "")
            args_str = func.get("arguments", "{}")
            try:
                args = _json.loads(args_str)
            except Exception:
                args = {}
            called.add((name, _stable_args_key(args)))
    return called


def _in_skill_context(messages: list[dict]) -> bool:
    """Return True if the message history shows a read_file call on a SKILL.md path."""
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            # Check if an assistant message had a read_file call on SKILL.md
            for tc in msg.get("tool_calls", []):
                func = tc.get("function", {})
                if func.get("name") == "read_file":
                    args_str = func.get("arguments", "")
                    if "SKILL.md" in args_str.upper() or "skill.md" in args_str:
                        return True
        elif role == "tool" and msg.get("name") == "read_file":
            # The tool result content is the file body — check for nanobot skill format
            content = msg.get("content", "")
            if isinstance(content, str) and '"nanobot"' in content:
                return True
    return False


def extract_bash_code_blocks(
    content: str,
    tool_names: frozenset[str],
    messages: list[dict] | None = None,
) -> list[ToolCallRequest]:
    """Convert ```bash ... ``` code blocks to exec() tool calls.

    Only activates when the 'exec' tool is registered AND we are in a skill
    context (i.e. the model recently read a SKILL.md file).  This avoids
    converting filesystem-exploration code blocks to exec calls.
    """
    if "exec" not in tool_names:
        return []
    if messages is not None and not _in_skill_context(messages):
        return []
    results: list[tuple[int, ToolCallRequest]] = []
    for m in _BASH_FENCE_RE.finditer(content):
        command = m.group(1).strip()
        if not command:
            continue
        # Skip commands that have dedicated tools (list_dir, read_file, etc.)
        first_line = command.split("\n")[0]
        if _SKIP_BASH_CMDS.match(first_line):
            continue
        results.append((m.start(), ToolCallRequest(
            id=_tc_id(),
            name="exec",
            arguments={"command": command},
        )))
    results.sort(key=lambda x: x[0])
    return [tc for _, tc in results]


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(\{[\s\S]*?\})\s*\n?```", re.IGNORECASE)
# A code fence left open at the end of the text with nothing inside it.
_ORPHAN_FENCE_RE = re.compile(r"\n*```(?:json)?[ \t]*\n?\s*$", re.IGNORECASE)


def _is_skill_invocation_json(js: str) -> bool:
    """True for the {"skill": ..., "action": ...} block SKILL.md asks models to
    emit. Deliberately narrower than the predicate used for trimming: this one
    decides what to *delete* from a user-facing message, so a JSON object the
    user actually asked to see must not match."""
    try:
        data = _json.loads(js)
    except Exception:
        return False
    return isinstance(data, dict) and ("skill" in data or "action" in data)


def _available_skill_names(spec: "AgentRunSpec") -> set[str]:
    """Which skills this instance actually has, by directory.

    The same three roots SkillsLoader reads, in the same order, and read from
    disk rather than asked of the loader: this runs on the failure path, where
    the useful thing is what is on the filesystem right now — a remote skill
    materialises only while its service answers, so `code` is present or absent
    depending on whether the code-broker is up.
    """
    from nanobot.agent.remote_skills import cache_dir as _remote_cache_dir
    from nanobot.agent.skills import BUILTIN_SKILLS_DIR

    roots = [BUILTIN_SKILLS_DIR]
    if spec.workspace:
        roots += [spec.workspace / "skills", _remote_cache_dir(spec.workspace)]
    names: set[str] = set()
    for root in roots:
        try:
            for entry in root.iterdir():
                if entry.is_dir() and (entry / "SKILL.md").is_file():
                    names.add(entry.name)
        except Exception:
            continue          # a root that is not there is simply no skills
    return names


def _named_skill_invocations(content: str | None) -> list[str]:
    """The skill names in any invocation blocks in `content`, in order.

    Used to say which skill did not resolve. The name matters more than the
    block: «no tengo la skill code» is something a person can act on, and the
    raw JSON is not.
    """
    names: list[str] = []
    if not content:
        return names
    for _, js in _find_json_objects(content):
        if not _is_skill_invocation_json(js):
            continue
        try:
            data = _json.loads(js)
        except Exception:
            continue
        name = data.get("skill") or data.get("action")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _strip_skill_invocation_text(content: str | None) -> str:
    """Remove skill-invocation syntax from text about to be shown to a user.

    Accepts None so a caller holding an optional string cannot turn a blank
    reply into a TypeError from a regex, which is how a whole turn once failed
    with "expected string or bytes-like object, got 'NoneType'".

    The block is an instruction to the runtime, not prose — SKILL.md tells the
    model to write it as text precisely so we can intercept it. It reaches the
    user whenever the interceptor does not consume it: the model restates a call
    it already made and the dedup drops it, or it writes the block beside a
    structured tool call, or the parse simply misses. None of those should end
    with raw JSON in the chat, so strip it on every path out.
    """
    if not content:
        return ""
    out = _JSON_FENCE_RE.sub(
        lambda m: "" if _is_skill_invocation_json(m.group(1)) else m.group(0), content
    )
    for start, js in reversed(_find_json_objects(out)):   # unfenced blocks
        if _is_skill_invocation_json(js):
            out = out[:start] + out[start + len(js):]
    out = _QWEN35_FUNC_RE.sub("", out)
    # An opening fence with nothing left inside it. Content is trimmed at the end
    # of the JSON *object*, which cuts a fenced block in half — the closing ```
    # never arrives, so the fenced pattern above cannot match and the object is
    # removed by the unfenced pass, leaving "```json" dangling at the end. That
    # is what reached the chat on every camera turn.
    out = _ORPHAN_FENCE_RE.sub("", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _without_skill_invocation_text(response: LLMResponse) -> LLMResponse:
    """The same response with any skill-invocation block taken out of its text."""
    if not response.content:
        return response
    stripped = _strip_skill_invocation_text(response.content)
    if stripped == response.content:
        return response
    return dataclasses.replace(response, content=stripped)


# A call that runs nothing. Models reach for one when they want to write a
# skill block and still feel like they took an action — the block is the action,
# but a tool call is what they were trained to end a turn with.
_NOOP_EXEC_COMMANDS = frozenset(("", "true", ":", "/bin/true", "exit", "exit 0"))


# `echo ok`, `echo invoked`, `echo done` — the same placeholder reflex as the
# literals above, but with something printed so it looks like work happened.
# Added 2026-09-20 after a turn spent 25 of these in three minutes: the model
# wrote its skill block beside `echo ok`, `echo ok` was not recognised as a
# no-op, so the block was stripped as though it sat beside real work, the model
# saw its own message come back without the block and wrote it again. The
# family asked which light was on in Paula's room and got no answer at all.
#
# Only a form that cannot touch anything counts: no redirection, pipe, chaining,
# substitution or subshell. `echo hi > /etc/passwd` is an `echo` and is not a
# no-op, which is why this is a whitelist of harmless characters rather than a
# prefix test.
# How many times one identical tool call may appear in a single turn before the
# turn is treated as stuck. `max_tool_iterations` is 200, which is a ceiling for
# long legitimate work and far too loose to catch a loop: a turn repeating
# itself every three seconds runs for ten minutes and returns nothing, which
# the family reads as being ignored rather than as a failure.
#
# Counted per signature across the whole turn, not consecutively: the loop
# measured on 2026-09-20 alternated `exec("echo ok")` with GetLiveContext
# probes, so a consecutive-repeat test would never have fired while the same
# no-op ran 25 times.
_REPEATED_TOOL_CALL_LIMIT = 8

_NOOP_ECHO_RE = re.compile(r"echo\s+[\w .,:!?'\"-]*$", re.IGNORECASE)


def _is_noop_exec(tc: ToolCallRequest) -> bool:
    if tc.name != "exec":
        return False
    command = (tc.arguments or {}).get("command") or ""
    normalised = command.strip().rstrip(";").strip()
    if normalised.lower() in _NOOP_EXEC_COMMANDS:
        return True
    return bool(_NOOP_ECHO_RE.fullmatch(normalised))


# `echo` and `printf` with nothing but the block after them. Anything more --
# a pipe, a redirect, a second command -- is a command somebody meant to run,
# and it is left to run.
_ECHO_PREFIX_RE = re.compile(r"^(?:echo(?:\s+-[neE]+)*|printf)\s+", re.IGNORECASE)


def _echoed_skill_block(tc: ToolCallRequest) -> str | None:
    """The skill block an ``exec`` only prints, or None.

    ornith-1.5:9b, asked "¿qué tareas tengo pendientes para hoy?" on
    2026-09-10, wrote exactly the block SKILL.md asks for --
    ``{"skill":"tasks","action":"list_chores"}`` -- and then put it *inside*
    ``exec("echo ...")``, because a tool call is what it was trained to end a
    turn with. The shell printed the block back, nothing ran, and the model
    concluded the skill was broken: it went on to invent an API on a host that
    does not exist, send it a bearer token, and grep the environment for
    credentials, twenty-odd round trips until the turn timed out. The block
    was right the whole time; only its envelope was wrong.
    """
    if tc.name != "exec":
        return None
    command = str((tc.arguments or {}).get("command") or "").strip().rstrip(";").strip()
    m = _ECHO_PREFIX_RE.match(command)
    body = command[m.end():].strip() if m else command
    if not body:
        return None
    candidates = [body]
    if len(body) >= 2 and body[0] == body[-1] and body[0] in "'\"":
        candidates.append(body[1:-1])
    candidates.append(body.replace('\\"', '"'))
    if len(body) >= 2 and body[0] == body[-1] == '"':
        candidates.append(body[1:-1].replace('\\"', '"'))
    for text in candidates:
        objs = _find_json_objects(text)
        if len(objs) == 1 and objs[0][1].strip() == text.strip() \
                and _is_skill_invocation_json(objs[0][1]):
            return objs[0][1]
    return None


def _messaged_skill_block(tc: ToolCallRequest) -> str | None:
    """The skill block a ``message`` call carries as its whole text, or None.

    The same mistake as `_echoed_skill_block` in a different envelope. In the
    model benchmark on 2026-09-11, Qwen3.6-35B-A3B wrote
    ``{"skill": "tasks", "action": "list_chores"}`` -- exactly the block the
    skill asks for -- three times, each time as the text of a ``message`` call,
    and ran out of turns; the Q3 build of the same model did it twice for the
    weather. The block was right; it went to the chat instead of the runtime.

    Only a message that is nothing *but* the block: a sentence around it is a
    message somebody meant to send, and it is left to go.
    """
    if tc.name != "message":
        return None
    text = str((tc.arguments or {}).get("content") or "").strip()
    fenced = _JSON_FENCE_RE.fullmatch(text)
    if fenced:
        text = fenced.group(1).strip()
    objs = _find_json_objects(text)
    if len(objs) == 1 and objs[0][1].strip() == text \
            and _is_skill_invocation_json(objs[0][1]):
        return objs[0][1]
    return None


def _lift_echoed_skill_blocks(
    response: LLMResponse, session_key: str | None,
) -> LLMResponse:
    """Turn an ``exec`` or a ``message`` that only carries a skill block into
    that block.

    The block goes into the reply text, where the parser below resolves it
    exactly as if the model had written it there, and the carrying call is
    dropped. Other calls in the same response are left alone -- and then meet
    `_resolve_block_beside_calls` like any block written beside a real call.
    """
    lifted: list[str] = []
    kept: list[ToolCallRequest] = []
    carriers: list[str] = []
    for tc in response.tool_calls:
        block = _echoed_skill_block(tc) or _messaged_skill_block(tc)
        if block is None:
            kept.append(tc)
        else:
            lifted.append(block)
            carriers.append(tc.name)
    if not lifted:
        return response
    logger.info(
        "Text tool call parsing: {} sent {} skill block(s) through {}; "
        "running the block instead",
        session_key or "default", len(lifted), ", ".join(sorted(set(carriers))),
    )
    content = "\n".join(part for part in (response.content or "", *lifted) if part)
    return dataclasses.replace(
        response, content=content, tool_calls=kept,
        finish_reason=response.finish_reason if kept else "stop",
    )


def _lift_skill_named_tool_calls(
    response: LLMResponse,
    messages: list[dict[str, Any]] | None,
    tool_names: frozenset[str],
    session_key: str | None,
) -> LLMResponse:
    """Rescue a call to a tool that does not exist but names a skill.

    Function-calling models reach for a skill the way they reach for a tool:
    `grocery(...)`, `weather:get_forecast(...)`, `home_lights(...)`. That fails
    as "Tool not found", the turn is spent, and the answer is lost even though
    the model picked the right skill.

    Measured 2026-09-10 over the model benchmark: every malformed call any model
    produced named a real skill, and all fifteen resolved by lowercasing and
    swapping `_` for `-`. Nothing is inferred here -- `_resolve_skill_name` is a
    lookup against the catalogue already in the prompt, so a name that does not
    resolve is left alone to fail as before.

    With an action recoverable (`skill:action`, or an `action` argument) the
    call becomes the invocation the model meant. Without one it becomes a read
    of that skill's own SKILL.md, exactly as the text path does, so the next
    turn is right. Either way the rescue is logged: a model that needed one got
    the call wrong, and that has to stay visible.
    """
    if not messages:
        return response
    skill_paths = _collect_skill_paths(messages)
    if not skill_paths:
        return response
    kept: list[ToolCallRequest] = []
    fixed: list[str] = []
    for tc in response.tool_calls:
        if tc.name in tool_names:
            kept.append(tc)
            continue
        head, _, tail = tc.name.partition(":")
        skill = _resolve_skill_name(head, skill_paths)
        if not skill:
            kept.append(tc)
            continue
        path = skill_paths[skill]
        args = tc.arguments if isinstance(tc.arguments, dict) else {}
        action = str(tail or args.get("action") or "").strip()
        if action and _load_skill_python_guide(path):
            invocation = dict(args)
            invocation["skill"] = skill
            invocation["action"] = action
            kept.append(ToolCallRequest(
                id=tc.id, name="__skill_translate",
                arguments={"path": path, "invocation": invocation}))
            fixed.append(f"{tc.name} -> {skill}.{action}")
        else:
            kept.append(ToolCallRequest(
                id=tc.id, name="read_file", arguments={"path": path}))
            fixed.append(f"{tc.name} -> read {skill}/SKILL.md")
    if not fixed:
        return response
    logger.info(
        "Skill-named tool call(s) rescued for {}: {}",
        session_key or "default", "; ".join(fixed),
    )
    return dataclasses.replace(response, tool_calls=kept)


def _has_skill_invocation_text(content: str | None) -> bool:
    """Whether *content* carries a skill block: something the stripper removes
    beyond whitespace. Compared trimmed, because the stripper also trims: a
    reply of "\n\n" came back as "" and read as a block, the only real call
    beside it was dropped as a placeholder, and the turn ended with "nothing
    was executed" (deepseek-v4-pro, 2026-09-24, asked to cross-reference the
    lights with Home Assistant)."""
    text = (content or "").strip()
    return bool(text) and _strip_skill_invocation_text(text) != text


def _resolve_block_beside_calls(
    response: LLMResponse, session_key: str | None,
) -> LLMResponse:
    """Decide what a skill block is worth when the model also called a tool.

    Both outcomes used to be one silent strip, and it cost an evening. Asked to
    mark two prize redemptions delivered, Alfred wrote
    ``{"skill": "tasks", "action": "fulfill_redemption", ...}`` as his reply and
    put ``exec("true")`` beside it. The call ran, the block was stripped, and
    what came back was an empty message and ``Exit code: 0`` — no error, nothing
    executed, nothing to learn from. He tried the identical shape 132 times in
    five minutes, one LLM round trip each, until the socket timed out; the reply
    that finally reached the family was written by the sub-agent the failure
    escalated to.

    So: a block beside calls that do nothing is the turn's real intent — drop
    the placeholders and let the parser run the block. A block beside a call
    that does something is taken out of the text (that is what keeps raw JSON
    out of the chat) and, since 2026-09-22, run after that call when it
    resolves -- see _apply_text_tool_call_parsing.
    """
    if not _has_skill_invocation_text(response.content):
        return response
    if all(_is_noop_exec(tc) for tc in response.tool_calls):
        logger.info(
            "Text tool call parsing: dropped {} no-op call(s) written beside a "
            "skill-invocation block for {}; running the block instead. Dropped: {}",
            len(response.tool_calls), session_key or "default",
            [(tc.name, _json.dumps(tc.arguments, ensure_ascii=False)[:200])
             for tc in response.tool_calls],
        )
        return dataclasses.replace(response, tool_calls=[], finish_reason="stop")
    logger.warning(
        "Text tool call parsing: {} wrote a skill-invocation block beside {}; "
        "the block is taken out of the text and run too if it resolves",
        session_key or "default", [tc.name for tc in response.tool_calls],
    )
    return response


def _calls_from_block(content: str | None, spec: "AgentRunSpec",
                      messages: list[dict[str, Any]]) -> list[ToolCallRequest]:
    """The call a skill-invocation block in *content* stands for, or [].

    The same extractors the text-only path uses, and the same dedup against
    what already ran this turn -- a block restating a call that has run must
    not run it twice. At most one, as there.
    """
    tool_names = frozenset(getattr(spec.tools, "tool_names", []))
    if not content or not tool_names:
        return []
    calls = extract_json_skill_invocations(content, messages, tool_names)
    if not calls:
        calls = extract_qwen35_skill_invocations(content, messages, tool_names)
    previously_called = _collect_previous_calls(messages)
    calls = [tc for tc in calls
             if (tc.name, _stable_args_key(tc.arguments)) not in previously_called]
    return calls[:1]


def _find_tool_call_end(content: str, tc: ToolCallRequest) -> int | None:
    """Return the character index just after the tool call invocation in *content*.

    Used to trim hallucinated text that the model writes after the call
    (e.g. fake ``Result:`` sections) so it does not accumulate in the context.
    Returns *None* when the invocation cannot be located.
    """
    # Bash code fence: ```bash\n<command>\n```
    if tc.name == "exec":
        command = tc.arguments.get("command", "")
        for m in _BASH_FENCE_RE.finditer(content):
            if m.group(1).strip() == command.strip():
                return m.end()

    # JSON skill invocation block
    if tc.name in ("read_file", "__skill_translate"):
        for start, json_str in _find_json_objects(content):
            try:
                data = _json.loads(json_str)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if any(k in data for k in ("skill", "action", "description", "name")):
                return start + len(json_str)

        # Qwen3.5 skill invocation: <function=skill-name>...</function>
        for m in _QWEN35_FUNC_RE.finditer(content):
            return m.end()

    # Qwen3.5 tool call: <function=name>...</function>
    for m in _QWEN35_FUNC_RE.finditer(content):
        if m.group(1).lower().replace("_", "-") == tc.name.lower().replace("_", "-"):
            return m.end()

    # Qwen3/Hermes: <tool_call>...</tool_call>
    for m in _HERMES_TOOL_CALL_RE.finditer(content):
        try:
            data = _json.loads(m.group(1).strip())
        except Exception:
            continue
        if isinstance(data, dict) and str(data.get("name") or "") == tc.name:
            return m.end()

    # XML-style: <tool_name ...>
    for m in _XML_TOOL_CALL_RE.finditer(content):
        if m.group(1) == tc.name:
            return m.end()

    # Python-style: tool_name(...)
    name_re = re.compile(r"(?<![.\w])" + re.escape(tc.name) + r"\s*\(")
    for m in name_re.finditer(content):
        open_pos = content.index("(", m.start())
        close_pos = _find_matching_paren(content, open_pos)
        if close_pos != -1:
            return close_pos + 1

    return None


def extract_xml_tool_calls(
    content: str,
    tool_names: frozenset[str],
) -> list[ToolCallRequest]:
    """Scan *content* for ``<tool_name attr="val" …>`` XML-style tool calls.

    Handles self-closing (``/>``) and open tags.  Only tool names present in
    *tool_names* are matched.
    """
    if not content or not tool_names:
        return []

    results: list[tuple[int, ToolCallRequest]] = []
    seen_starts: set[int] = set()

    for m in _XML_TOOL_CALL_RE.finditer(content):
        tool_name = m.group(1)
        if tool_name not in tool_names:
            continue
        start = m.start()
        if start in seen_starts:
            continue
        kwargs: dict[str, Any] = {}
        for attr_m in _XML_ATTR_RE.finditer(m.group(2)):
            key = attr_m.group(1)
            raw = attr_m.group(2)
            try:
                kwargs[key] = ast.literal_eval(raw)
            except Exception:
                kwargs[key] = raw[1:-1]
        seen_starts.add(start)
        results.append((start, ToolCallRequest(
            id=_tc_id(),
            name=tool_name,
            arguments=kwargs,
        )))

    results.sort(key=lambda x: x[0])
    return [tc for _, tc in results]


def extract_text_tool_calls(
    content: str,
    tool_names: frozenset[str],
) -> list[ToolCallRequest]:
    """Scan *content* for ``tool_name(key="val", …)`` patterns.

    Only tool names present in *tool_names* are matched, so arbitrary Python
    function calls in the text are ignored.  Matches are returned in the order
    they appear in the text.
    """
    if not content or not tool_names:
        return []

    # Build a pattern that anchors on word boundaries and matches any
    # registered tool name (longest first to avoid prefix ambiguity).
    sorted_names = sorted(tool_names, key=len, reverse=True)
    name_re = re.compile(
        r"(?<![.\w])(" + "|".join(re.escape(n) for n in sorted_names) + r")\s*\(",
    )

    results: list[tuple[int, ToolCallRequest]] = []
    seen_starts: set[int] = set()

    for m in name_re.finditer(content):
        start = m.start()
        if start in seen_starts:
            continue
        tool_name = m.group(1)

        # Locate the opening '(' for this call
        open_pos = content.index("(", m.start())
        close_pos = _find_matching_paren(content, open_pos)
        if close_pos == -1:
            continue

        call_text = content[start : close_pos + 1]
        kwargs = _parse_call_kwargs(call_text)
        if kwargs is None:
            # ast failed – try the regex fallback
            args_text = content[open_pos + 1 : close_pos]
            kwargs = _parse_kwargs_regex(args_text)

        seen_starts.add(start)
        results.append((start, ToolCallRequest(
            id=_tc_id(),
            name=tool_name,
            arguments=kwargs,
        )))

    results.sort(key=lambda x: x[0])
    return [tc for _, tc in results]


def extract_qwen35_tool_calls(
    content: str,
    tool_names: frozenset[str],
) -> list[ToolCallRequest]:
    """Scan *content* for Qwen3.5/Qwen3-Coder-style function call blocks.

    Format::

        <function=tool_name>
        <parameter=key>value</parameter>
        ...
        </function>

    Parameters may also be provided as a bare JSON object inside the tag when
    ``<parameter=...>`` syntax is absent.
    """
    if not content or not tool_names:
        return []

    results: list[tuple[int, ToolCallRequest]] = []
    seen_starts: set[int] = set()

    for m in _QWEN35_FUNC_RE.finditer(content):
        start = m.start()
        if start in seen_starts:
            continue
        tool_name = m.group(1)
        if tool_name not in tool_names:
            continue
        body = m.group(2)
        arguments: dict[str, Any] = {}
        for pm in _QWEN35_PARAM_RE.finditer(body):
            arguments[pm.group(1)] = pm.group(2).strip()
        if not arguments:
            try:
                parsed = _json.loads(body.strip())
                if isinstance(parsed, dict):
                    arguments = parsed
            except Exception:
                pass
        seen_starts.add(start)
        results.append((start, ToolCallRequest(
            id=_tc_id(),
            name=tool_name,
            arguments=arguments,
        )))

    results.sort(key=lambda x: x[0])
    return [tc for _, tc in results]


def extract_hermes_tool_calls(
    content: str,
    tool_names: frozenset[str],
) -> list[ToolCallRequest]:
    """Scan *content* for Qwen3/Hermes-style ``<tool_call>`` JSON blocks.

    Format::

        <tool_call>
        {"name": "tool_name", "arguments": {"key": "value"}}
        </tool_call>
    """
    if not content or not tool_names:
        return []

    results: list[tuple[int, ToolCallRequest]] = []
    seen_starts: set[int] = set()

    for m in _HERMES_TOOL_CALL_RE.finditer(content):
        start = m.start()
        if start in seen_starts:
            continue
        try:
            data = _json.loads(m.group(1).strip())
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        tool_name = str(data.get("name") or "")
        if not tool_name or tool_name not in tool_names:
            continue
        arguments = data.get("arguments") or data.get("parameters") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        seen_starts.add(start)
        results.append((start, ToolCallRequest(
            id=_tc_id(),
            name=tool_name,
            arguments=arguments,
        )))

    results.sort(key=lambda x: x[0])
    return [tc for _, tc in results]


def extract_qwen35_skill_invocations(
    content: str,
    messages: list[dict],
    tool_names: frozenset[str],
) -> list[ToolCallRequest]:
    """Detect Qwen3.5/Qwen3-Coder ``<function=skill-name>`` blocks that invoke
    nanobot skills and convert them to ``read_file`` / ``__skill_translate`` calls,
    exactly as :func:`extract_json_skill_invocations` does for JSON blocks.
    """
    if "read_file" not in tool_names:
        return []

    skill_paths = _collect_skill_paths(messages)
    if not skill_paths:
        return []

    results: list[tuple[int, ToolCallRequest]] = []
    seen_paths: set[str] = set()

    for m in _QWEN35_FUNC_RE.finditer(content):
        raw_name = m.group(1)
        skill_name = _resolve_skill_name(raw_name, skill_paths)
        if not skill_name:
            body_probe = m.group(2)
            # 1. Try "skill" or "name" parameter inside the body
            for pm in _QWEN35_PARAM_RE.finditer(body_probe):
                if pm.group(1) in ("skill", "name"):
                    skill_name = _resolve_skill_name(pm.group(2).strip(), skill_paths)
                    if skill_name:
                        break
        if not skill_name:
            # 2. Map action parameter → skill name
            body_probe = m.group(2)
            for pm in _QWEN35_PARAM_RE.finditer(body_probe):
                if pm.group(1) == "action":
                    mapped = _ACTION_TO_SKILL.get(pm.group(2).strip())
                    if mapped and mapped in skill_paths:
                        skill_name = mapped
                        break
        if not skill_name:
            # 3. Infer from preceding text
            preceding = content[:m.start()].lower()
            for name in skill_paths:
                if name in preceding or name.replace("-", "_") in preceding:
                    skill_name = name
                    break
        if not skill_name:
            continue
        path = skill_paths.get(skill_name)
        if not path or path in seen_paths:
            continue
        body = m.group(2)
        invocation: dict[str, Any] = {"skill": skill_name}
        for pm in _QWEN35_PARAM_RE.finditer(body):
            invocation[pm.group(1)] = pm.group(2).strip()
        if len(invocation) == 1 and body.strip():
            try:
                parsed = _json.loads(body.strip())
                if isinstance(parsed, dict):
                    invocation.update(parsed)
            except Exception:
                pass
        seen_paths.add(path)
        tc_id = _tc_id()
        has_action = any(k not in ("skill", "name", "description") for k in invocation)
        if has_action and _load_skill_python_guide(path):
            results.append((m.start(), ToolCallRequest(
                id=tc_id,
                name="__skill_translate",
                arguments={"path": path, "invocation": invocation},
            )))
        else:
            results.append((m.start(), ToolCallRequest(
                id=tc_id,
                name="read_file",
                arguments={"path": path},
            )))

    results.sort(key=lambda x: x[0])
    return [tc for _, tc in results]


def _budget(spec: "AgentRunSpec"):
    """0, 1, 2 ... while under ``spec.max_iterations``, read afresh each time.

    `range(spec.max_iterations)` fixed the budget when the turn began. A turn
    working through a plan (tools/plan.py) earns more calls as it sets its
    steps, so the budget is read on every pass; the loop's `for ... else`
    still means "ran out"."""
    i = 0
    while i < spec.max_iterations:
        yield i
        i += 1


def _runtime_printf(text: str) -> str:
    """A shell command that prints the runtime's own note in place of a call
    it would not run. Registered like a translated skill call: a plan step's
    shell runs only what the runtime wrote, and without this the step read
    "there is no shell" instead of why its call was stopped (2026-09-25)."""
    command = "printf '%s' " + shlex.quote(text)
    register_skill_translation(command)
    return command


@dataclass(slots=True)
class AgentRunSpec:
    """Configuration for a single agent execution."""

    initial_messages: list[dict[str, Any]]
    tools: ToolRegistry
    model: str
    max_iterations: int
    max_tool_result_chars: int
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    hook: AgentHook | None = None
    error_message: str | None = _DEFAULT_ERROR_MESSAGE
    max_iterations_message: str | None = None
    concurrent_tools: bool = False
    fail_on_tool_error: bool = False
    workspace: Path | None = None
    session_key: str | None = None
    context_window_tokens: int | None = None
    context_block_limit: int | None = None
    provider_retry_mode: str = "standard"
    progress_callback: Any | None = None
    retry_wait_callback: Any | None = None
    checkpoint_callback: Any | None = None
    injection_callback: Any | None = None
    # A plan step the planner did not mark as changing anything: skill actions
    # that are not reads are refused (tools/plan.py `acts`).
    read_only: bool = False
    # Where a read-only step records what it was stopped from doing, so the
    # planner hears it from the runtime rather than from the step.
    refused: list[str] | None = None


@dataclass(slots=True)
class AgentRunResult:
    """Outcome of a shared agent execution."""

    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    error: str | None = None
    tool_events: list[dict[str, str]] = field(default_factory=list)
    had_injections: bool = False
    # Set when an outage fallback answered any call in this run, i.e. the model
    # that replied is not the one `spec.model` asked for. The prompt for this
    # turn was already sent by then and says the model that was asked for; this
    # is how anything downstream (the cost ledger) can still file it correctly.
    served_by_model: str | None = None


class AgentRunner:
    """Run a tool-capable LLM loop without product-layer concerns."""

    def __init__(self, provider: LLMProvider):
        self.provider = provider

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [
                    item if isinstance(item, dict) else {"type": "text", "text": str(item)}
                    for item in value
                ]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    @classmethod
    def _append_injected_messages(
        cls,
        messages: list[dict[str, Any]],
        injections: list[dict[str, Any]],
    ) -> None:
        """Append injected user messages while preserving role alternation."""
        for injection in injections:
            if (
                messages
                and injection.get("role") == "user"
                and messages[-1].get("role") == "user"
            ):
                merged = dict(messages[-1])
                merged["content"] = cls._merge_message_content(
                    merged.get("content"),
                    injection.get("content"),
                )
                messages[-1] = merged
                continue
            messages.append(injection)

    async def _try_drain_injections(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        assistant_message: dict[str, Any] | None,
        injection_cycles: int,
        *,
        phase: str = "after error",
        iteration: int | None = None,
    ) -> tuple[bool, int]:
        """Drain pending injections. Returns (should_continue, updated_cycles).

        If injections are found and we haven't exceeded _MAX_INJECTION_CYCLES,
        append them to *messages* (and emit a checkpoint if *assistant_message*
        and *iteration* are both provided) and return (True, cycles+1) so the
        caller continues the iteration loop.  Otherwise return (False, cycles).
        """
        if injection_cycles >= _MAX_INJECTION_CYCLES:
            return False, injection_cycles
        injections = await self._drain_injections(spec)
        if not injections:
            return False, injection_cycles
        injection_cycles += 1
        if assistant_message is not None:
            messages.append(assistant_message)
            if iteration is not None:
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "final_response",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [],
                    },
                )
        self._append_injected_messages(messages, injections)
        logger.info(
            "Injected {} follow-up message(s) {} ({}/{})",
            len(injections), phase, injection_cycles, _MAX_INJECTION_CYCLES,
        )
        return True, injection_cycles

    async def _drain_injections(self, spec: AgentRunSpec) -> list[dict[str, Any]]:
        """Drain pending user messages via the injection callback.

        Returns normalized user messages (capped by
        ``_MAX_INJECTIONS_PER_TURN``), or an empty list when there is
        nothing to inject. Messages beyond the cap are logged so they
        are not silently lost.
        """
        if spec.injection_callback is None:
            return []
        try:
            signature = inspect.signature(spec.injection_callback)
            accepts_limit = (
                "limit" in signature.parameters
                or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
            )
            if accepts_limit:
                items = await spec.injection_callback(limit=_MAX_INJECTIONS_PER_TURN)
            else:
                items = await spec.injection_callback()
        except Exception:
            logger.exception("injection_callback failed")
            return []
        if not items:
            return []
        injected_messages: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict) and item.get("role") == "user" and "content" in item:
                injected_messages.append(item)
                continue
            text = getattr(item, "content", str(item))
            if text.strip():
                injected_messages.append({"role": "user", "content": text})
        if len(injected_messages) > _MAX_INJECTIONS_PER_TURN:
            dropped = len(injected_messages) - _MAX_INJECTIONS_PER_TURN
            logger.warning(
                "Injection callback returned {} messages, capping to {} ({} dropped)",
                len(injected_messages), _MAX_INJECTIONS_PER_TURN, dropped,
            )
            injected_messages = injected_messages[:_MAX_INJECTIONS_PER_TURN]
        return injected_messages

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        hook = spec.hook or AgentHook()
        messages = list(spec.initial_messages)
        final_content: str | None = None
        tools_used: list[str] = []
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        error: str | None = None
        stop_reason = "completed"
        tool_events: list[dict[str, str]] = []
        external_lookup_counts: dict[str, int] = {}
        empty_content_retries = 0
        length_recovery_count = 0
        had_injections = False
        injection_cycles = 0
        served_by_model: str | None = None
        tool_call_counts: dict[tuple[str, str], int] = {}

        for iteration in _budget(spec):
            try:
                # Keep the persisted conversation untouched. Context governance
                # may repair or compact historical messages for the model, but
                # those synthetic edits must not shift the append boundary used
                # later when the caller saves only the new turn.
                messages_for_model = self._drop_orphan_tool_results(messages)
                messages_for_model = self._backfill_missing_tool_results(messages_for_model)
                messages_for_model = self._microcompact(messages_for_model)
                messages_for_model = self._apply_tool_result_budget(spec, messages_for_model)
                messages_for_model = self._snip_history(spec, messages_for_model)
                # Snipping may have created new orphans; clean them up.
                messages_for_model = self._drop_orphan_tool_results(messages_for_model)
                messages_for_model = self._backfill_missing_tool_results(messages_for_model)
            except Exception as exc:
                logger.warning(
                    "Context governance failed on turn {} for {}: {}; applying minimal repair",
                    iteration,
                    spec.session_key or "default",
                    exc,
                )
                try:
                    messages_for_model = self._drop_orphan_tool_results(messages)
                    messages_for_model = self._backfill_missing_tool_results(messages_for_model)
                except Exception:
                    messages_for_model = messages
            context = AgentHookContext(iteration=iteration, messages=messages)
            await hook.before_iteration(context)
            response = await self._request_model(spec, messages_for_model, hook, context)
            raw_usage = self._usage_dict(response.usage)
            context.response = response
            context.usage = dict(raw_usage)
            context.tool_calls = list(response.tool_calls)
            self._accumulate_usage(usage, raw_usage)
            served_by_model = response.served_by_model or served_by_model

            if response.should_execute_tools:
                # Is this turn going round in circles? Same tool, same
                # arguments, over and over means the model is not converging on
                # an answer and no further iteration will change that.
                stuck_call = None
                for _tc in response.tool_calls:
                    # Every no-op exec is the same call. deepseek-v4-pro on
                    # 2026-09-21, done with the lights lookup and unable to
                    # write the answer, sent `echo x`, `echo y`, `echo z` ...
                    # `echo done52`: 83 calls in five minutes, each with a
                    # fresh argument, so counted by exact arguments none of
                    # them ever repeated. What repeats is the shape -- a call
                    # that does nothing -- and that is what gets counted.
                    if _is_noop_exec(_tc):
                        _sig = ("exec", "<no-op>")
                    else:
                        _sig = (_tc.name,
                                _json.dumps(_tc.arguments or {}, sort_keys=True, default=str))
                    tool_call_counts[_sig] = tool_call_counts.get(_sig, 0) + 1
                    if tool_call_counts[_sig] >= _REPEATED_TOOL_CALL_LIMIT:
                        stuck_call = _sig
                if stuck_call is not None:
                    logger.warning(
                        "Repeated tool call: {} called {}({}) {} times in one turn; "
                        "stopping the turn rather than looping to max_iterations",
                        spec.session_key or "default", stuck_call[0],
                        stuck_call[1][:120], tool_call_counts[stuck_call],
                    )
                    stop_reason = "repeated_tool_calls"
                    # Before giving up: if tools already ran this turn, the
                    # model may be holding the answer and unable to switch from
                    # calls to prose -- deepseek-v4-pro on 2026-09-21 had the
                    # lights list and the HA context in hand and sent `echo x`,
                    # `echo y` instead of writing. One more call with the tools
                    # switched off asks for the answer from what it has. Blank
                    # or another call means it really is stuck, and the honest
                    # sentence below stands.
                    if tools_used:
                        finalised = await self._request_finalization_retry(spec, messages)
                        fin_usage = self._usage_dict(finalised.usage)
                        self._accumulate_usage(usage, fin_usage)
                        raw_usage = self._merge_usage(raw_usage, fin_usage)
                        served_by_model = finalised.served_by_model or served_by_model
                        context.response = finalised
                        context.usage = dict(raw_usage)
                        fin_clean = hook.finalize_content(context, finalised.content)
                        if not finalised.tool_calls and not is_blank_text(fin_clean):
                            logger.info(
                                "Repeated tool call: {} answered once the tools were "
                                "switched off ({} chars)", spec.session_key or "default",
                                len(fin_clean),
                            )
                            messages.append(build_assistant_message(
                                fin_clean,
                                reasoning_content=finalised.reasoning_content,
                                thinking_blocks=finalised.thinking_blocks,
                            ))
                            final_content = fin_clean
                            stop_reason = "completed"
                            context.final_content = final_content
                            context.stop_reason = stop_reason
                            await hook.after_iteration(context)
                            break
                    # Deliberately the same ending as running out of iterations,
                    # because that is what this is — the same dead end, reached
                    # sooner. Callers already handle it: the subagent announces
                    # its own fallback when this comes back empty, and a turn
                    # that ends any other way would be a second thing for every
                    # caller to learn. The partial text the model emitted while
                    # stuck ("working", "let me check") is filler, not an answer,
                    # and passing it off as one is worse than saying nothing.
                    if spec.max_iterations_message is not None:
                        # Callers that supply their own ending keep it — the
                        # subagent's is empty on purpose so it can announce its
                        # own fallback, and this must not start speaking for it.
                        final_content = spec.max_iterations_message.format(
                            max_iterations=spec.max_iterations,
                        )
                    else:
                        # Its own sentence, not the max-iterations template.
                        # Reusing that one told a household "I reached the
                        # maximum number of tool call iterations (200)" after
                        # nine calls — a number that never happened, about a
                        # limit that was not the reason. An explanation that is
                        # false is worse than a vague one: it sends the person
                        # looking for a setting to raise.
                        final_content = _dead_end("loop") or (
                            "I kept making the same call over and over without "
                            "getting anywhere, so I stopped instead of looping. "
                            "Tell me what to try instead, or ask for a smaller "
                            "piece of it."
                        )
                    self._append_final_message(messages, final_content)
                    context.final_content = final_content
                    context.stop_reason = stop_reason
                    await hook.after_iteration(context)
                    break

                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=True)

                assistant_message = build_assistant_message(
                    response.content or "",
                    tool_calls=_history_tool_calls(response),
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                messages.append(assistant_message)
                # `skill:finanzas` rather than the `exec` it was rewritten
                # into — substituted, never added, so a turn is counted once.
                _skills = getattr(response, "_skills_by_index", None) or {}
                tools_used.extend(
                    f"skill:{_skills[i]}" if i in _skills else tc.name
                    for i, tc in enumerate(response.tool_calls))
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "awaiting_tools",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [tc.to_openai_tool_call() for tc in response.tool_calls],
                    },
                )

                await hook.before_execute_tools(context)

                results, new_events, fatal_error = await self._execute_tools(
                    spec,
                    response.tool_calls,
                    external_lookup_counts,
                )
                tool_events.extend(new_events)
                context.tool_results = list(results)
                context.tool_events = list(new_events)
                completed_tool_results: list[dict[str, Any]] = []
                for tool_call, result in zip(response.tool_calls, results):
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": self._normalize_tool_result(
                            spec,
                            tool_call.id,
                            tool_call.name,
                            result,
                        ),
                    }
                    messages.append(tool_message)
                    completed_tool_results.append(tool_message)
                if getattr(response, "_stripped_skill_block", False) and completed_tool_results:
                    last = completed_tool_results[-1]
                    if isinstance(last["content"], list):
                        last["content"] = [*last["content"], {"type": "text", "text": _STRIPPED_BLOCK_NOTE}]
                    else:
                        last["content"] = f"{last['content'] or ''}\n\n{_STRIPPED_BLOCK_NOTE}".lstrip()
                if fatal_error is not None:
                    error = f"Error: {type(fatal_error).__name__}: {fatal_error}"
                    final_content = error
                    stop_reason = "tool_error"
                    self._append_final_message(messages, final_content)
                    context.final_content = final_content
                    context.error = error
                    context.stop_reason = stop_reason
                    await hook.after_iteration(context)
                    should_continue, injection_cycles = await self._try_drain_injections(
                        spec, messages, None, injection_cycles,
                        phase="after tool error",
                    )
                    if should_continue:
                        had_injections = True
                        continue
                    break
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "tools_completed",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": completed_tool_results,
                        "pending_tool_calls": [],
                    },
                )
                empty_content_retries = 0
                length_recovery_count = 0
                # Checkpoint 1: drain injections after tools, before next LLM call
                _drained, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after tool execution",
                )
                if _drained:
                    had_injections = True
                await hook.after_iteration(context)
                continue

            if response.has_tool_calls:
                logger.warning(
                    "Ignoring tool calls under finish_reason='{}' for {}",
                    response.finish_reason,
                    spec.session_key or "default",
                )

            clean = hook.finalize_content(context, response.content)
            if response.finish_reason != "error" and is_blank_text(clean):
                empty_content_retries += 1
                if empty_content_retries < _MAX_EMPTY_RETRIES:
                    logger.warning(
                        "Empty response on turn {} for {} ({}/{}); retrying",
                        iteration,
                        spec.session_key or "default",
                        empty_content_retries,
                        _MAX_EMPTY_RETRIES,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=False)
                    await hook.after_iteration(context)
                    continue
                logger.warning(
                    "Empty response on turn {} for {} after {} retries; attempting finalization",
                    iteration,
                    spec.session_key or "default",
                    empty_content_retries,
                )
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=False)
                response = await self._request_finalization_retry(spec, messages_for_model)
                retry_usage = self._usage_dict(response.usage)
                self._accumulate_usage(usage, retry_usage)
                served_by_model = response.served_by_model or served_by_model
                raw_usage = self._merge_usage(raw_usage, retry_usage)
                context.response = response
                context.usage = dict(raw_usage)
                context.tool_calls = list(response.tool_calls)
                clean = hook.finalize_content(context, response.content)

            if response.finish_reason == "length" and not is_blank_text(clean):
                length_recovery_count += 1
                if length_recovery_count <= _MAX_LENGTH_RECOVERIES:
                    logger.info(
                        "Output truncated on turn {} for {} ({}/{}); continuing",
                        iteration,
                        spec.session_key or "default",
                        length_recovery_count,
                        _MAX_LENGTH_RECOVERIES,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=True)
                    messages.append(build_assistant_message(
                        clean,
                        reasoning_content=response.reasoning_content,
                        thinking_blocks=response.thinking_blocks,
                    ))
                    messages.append(build_length_recovery_message())
                    await hook.after_iteration(context)
                    continue

            assistant_message: dict[str, Any] | None = None
            if response.finish_reason != "error" and not is_blank_text(clean):
                assistant_message = build_assistant_message(
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

            # Check for mid-turn injections BEFORE signaling stream end.
            # If injections are found we keep the stream alive (resuming=True)
            # so streaming channels don't prematurely finalize the card.
            should_continue, injection_cycles = await self._try_drain_injections(
                spec, messages, assistant_message, injection_cycles,
                phase="after final response",
                iteration=iteration,
            )
            if should_continue:
                had_injections = True

            if hook.wants_streaming():
                await hook.on_stream_end(context, resuming=should_continue)

            if should_continue:
                await hook.after_iteration(context)
                continue

            if response.finish_reason == "error":
                final_content = clean or spec.error_message or _DEFAULT_ERROR_MESSAGE
                stop_reason = "error"
                error = final_content
                self._append_model_error_placeholder(messages)
                _log_error("runner.llm_error", error or "unknown LLM error", session_key=spec.session_key)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after LLM error",
                )
                if should_continue:
                    had_injections = True
                    continue
                break
            if is_blank_text(clean):
                final_content = EMPTY_FINAL_RESPONSE_MESSAGE
                stop_reason = "empty_final_response"
                error = final_content
                self._append_final_message(messages, final_content)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after empty response",
                )
                if should_continue:
                    had_injections = True
                    continue
                break

            messages.append(assistant_message or build_assistant_message(
                clean,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            ))
            await self._emit_checkpoint(
                spec,
                {
                    "phase": "final_response",
                    "iteration": iteration,
                    "model": spec.model,
                    "assistant_message": messages[-1],
                    "completed_tool_results": [],
                    "pending_tool_calls": [],
                },
            )
            final_content = clean
            if getattr(response, "bad_invocation", False):
                # A skill block that resolved to nothing: the note is in the
                # content, the message is in history, and the turn is *not*
                # completed -- see `_apply_text_tool_call_parsing`.
                stop_reason = "bad_invocation"
            context.final_content = final_content
            context.stop_reason = stop_reason
            await hook.after_iteration(context)
            break
        else:
            stop_reason = "max_iterations"
            if spec.max_iterations_message:
                final_content = spec.max_iterations_message.format(
                    max_iterations=spec.max_iterations,
                )
            else:
                final_content = _dead_end(
                    "max_iterations", max_iterations=spec.max_iterations,
                ) or render_template(
                    "agent/max_iterations_message.md",
                    strip=True,
                    max_iterations=spec.max_iterations,
                )
            self._append_final_message(messages, final_content)
            # Drain any remaining injections so they are appended to the
            # conversation history instead of being re-published as
            # independent inbound messages by _dispatch's finally block.
            # We ignore should_continue here because the for-loop has already
            # exhausted all iterations.
            drained_after_max_iterations, injection_cycles = await self._try_drain_injections(
                spec, messages, None, injection_cycles,
                phase="after max_iterations",
            )
            if drained_after_max_iterations:
                had_injections = True

        return AgentRunResult(
            final_content=final_content,
            messages=messages,
            tools_used=tools_used,
            usage=usage,
            stop_reason=stop_reason,
            error=error,
            tool_events=tool_events,
            had_injections=had_injections,
            served_by_model=served_by_model,
        )

    def _build_request_kwargs(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "model": spec.model,
            "retry_mode": spec.provider_retry_mode,
            "on_retry_wait": spec.retry_wait_callback,
        }
        if spec.temperature is not None:
            kwargs["temperature"] = spec.temperature
        if spec.max_tokens is not None:
            kwargs["max_tokens"] = spec.max_tokens
        if spec.reasoning_effort is not None:
            kwargs["reasoning_effort"] = spec.reasoning_effort
        return kwargs

    async def _request_model(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        hook: AgentHook,
        context: AgentHookContext,
    ) -> LLMResponse:
        kwargs = self._build_request_kwargs(
            spec,
            messages,
            tools=spec.tools.get_definitions(),
        )
        if hook.wants_streaming():
            async def _stream(delta: str) -> None:
                await hook.on_stream(context, delta)

            async def _reasoning(delta: str) -> None:
                await hook.on_reasoning(context, delta)

            response = await self.provider.chat_stream_with_retry(
                **kwargs,
                on_content_delta=_stream,
                on_reasoning_delta=_reasoning,
            )
        else:
            response = await self.provider.chat_with_retry(**kwargs)
        response = self._apply_text_tool_call_parsing(response, spec, messages)
        if response.has_tool_calls and messages:
            response = await self._maybe_translate_skill_calls(response, spec, messages)
        return response

    async def _maybe_translate_skill_calls(
        self,
        response: "LLMResponse",
        spec: "AgentRunSpec",
        messages: list[dict],
    ) -> "LLMResponse":
        """Resolve skill invocations into Python exec calls via a stateless translation LLM.

        Handles two cases:
        1. ``__skill_translate`` pseudo-calls produced by ``extract_json_skill_invocations``
           when action args are present and a Python guide exists.
        2. Native/text-parsed calls to *unregistered* tool names whose arguments contain a
           ``skill`` or ``skill_name`` key matching a known skill — the model hallucinated a
           wrapper tool like ``invoke(...)`` or ``invoke_skill(...)`` instead of emitting plain JSON.
        """
        new_tool_calls = list(response.tool_calls)
        changed = False
        # Which call was which skill. Every skill runs through `exec`, so
        # without this the record of a turn says "exec" forty times and nothing
        # about what was actually invoked — and "which skill costs us money" is
        # exactly the question the usage page exists to answer. Kept per index
        # rather than as a list, so the caller can substitute name-for-name and
        # a turn is never counted twice.
        skills_by_index: dict[int, str] = {}
        invocations_by_index: dict[int, dict] = {}
        skill_paths = _collect_skill_paths(messages)
        registered_tools = frozenset(getattr(spec.tools, "tool_names", []))

        for i, tc in enumerate(new_tool_calls):
            path: str | None = None
            invocation: dict | None = None

            if tc.name == "__skill_translate":
                path = tc.arguments.get("path", "")
                invocation = tc.arguments.get("invocation", {})

            elif tc.name == "exec":
                # `echo '{"skill":"grocery","action":"add_grocery",...}'` — the
                # model reaching for the shell to "say" the block. ExecTool
                # refuses it and says to write it as text, which works, but the
                # refusal costs a whole round trip and the model opens nearly
                # every skill call this way: two turns for one call, every time,
                # its own reasoning noting "I keep making the same mistake".
                # Route it like any other wrong-shaped invocation instead. The
                # refusal stays for the cases this cannot resolve — an unknown
                # skill, or a block among other arguments.
                echoed = _echoed_skill_invocation(tc.arguments.get("command", ""))
                if echoed:
                    resolved = _resolve_skill_name(echoed.get("skill", ""), skill_paths)
                    if resolved:
                        path = skill_paths[resolved]
                        invocation = echoed

            elif tc.name not in registered_tools:
                # Hallucinated wrapper tool — check if args look like a skill invocation
                args = tc.arguments
                skill_raw = args.get("skill") or args.get("skill_name")
                if isinstance(skill_raw, str):
                    resolved = _resolve_skill_name(skill_raw, skill_paths)
                    if resolved:
                        path = skill_paths[resolved]
                        invocation = args

            if not path or not invocation:
                continue

            # A question from another member gets reads and nothing else. This
            # is the enforcement point rather than the tool registry because
            # skills RUN through exec — dropping tools would take the skills
            # with them, and leaving exec would leave every write reachable.
            # Refused here, the model is told plainly and can still answer with
            # what it may read.
            # getattr, not attribute access: spec is duck-typed at several
            # call sites, and a turn must not die because one of them is
            # missing a field. Absent means 'not an ask session', which is
            # the permissive answer only for the user's own instance.
            session_key = getattr(spec, "session_key", None)
            if is_third_party_session(session_key):
                action = str(invocation.get("action", ""))
                from_whatsapp = is_whatsapp_session(session_key)
                allowed = (_WHATSAPP_READABLE_ACTIONS if from_whatsapp
                           else _ASK_READABLE_ACTIONS)
                if action not in allowed:
                    logger.warning(
                        "{} session: refused action {} on skill {}",
                        "whatsapp" if from_whatsapp else "ask",
                        action, invocation.get("skill"),
                    )
                    # Delivered as the tool's own output, so the model reads it
                    # as a result and can answer around it, rather than as an
                    # unknown-tool error it would retry against.
                    #
                    # Two refusals, because they are two different noes. A family
                    # member is being told this question is read-only; somebody
                    # outside the house is being told the answer is not theirs to
                    # have, which is not the same sentence and must not be
                    # answered with "I still cannot" as though it were a
                    # limitation about to be lifted.
                    excuse = (
                        f"Whoever is asking is not from the household, so you "
                        f"cannot consult '{action}' or say anything about the "
                        f"household, their places, their chores or their things. "
                        f"Answer kindly that you cannot share that, without "
                        f"explaining why and without offering to ask anybody."
                    ) if from_whatsapp else (
                        f"This is a question from another person, and in those you "
                        f"can only READ. '{action}' changes something, so it was "
                        f"not run. Answer \"that is not for me\"."
                    )
                    new_tool_calls[i] = ToolCallRequest(
                        id=tc.id, name="exec",
                        arguments={"command": _runtime_printf(excuse)})
                    changed = True
                    continue

            if _has_go_ahead(invocation) and not _person_answered_pending(messages):
                action = str(invocation.get("action", ""))
                logger.warning("refused a go-ahead nobody gave: {} on skill {}",
                               action, invocation.get("skill"))
                refusal = (f"'{action}' was called as already confirmed, but the person has "
                           f"not been asked. Call it without the confirmation to see what it "
                           f"would do, tell the person, and wait for their yes.")
                new_tool_calls[i] = ToolCallRequest(
                    id=tc.id, name="exec",
                    arguments={"command": _runtime_printf(refusal)})
                changed = True
                continue

            if getattr(spec, "read_only", False) and not is_read_action(invocation):
                action = str(invocation.get("action", ""))
                logger.warning("read-only plan step: refused action {} on skill {}",
                               action, invocation.get("skill"))
                if spec.refused is not None:
                    spec.refused.append(f"{invocation.get('skill')}.{action}")
                refusal = (f"This step only reads, so '{action}' was not run: it changes "
                           f"something. Finish the step with what you found, and say what "
                           f"you would change -- the person is asked first.")
                new_tool_calls[i] = ToolCallRequest(
                    id=tc.id, name="exec",
                    arguments={"command": _runtime_printf(refusal)})
                changed = True
                continue

            python_guide = _load_skill_python_guide(path)
            if not python_guide:
                if tc.name == "__skill_translate":
                    new_tool_calls[i] = ToolCallRequest(id=tc.id, name="read_file", arguments={"path": path})
                    changed = True
                continue

            # The roster first, the path second. `skill_paths` is built from
            # the skills listed in the system message, and a call can arrive
            # for one that is not in that list — a path the model carried over
            # from earlier context, a turn assembled without the listing. The
            # fallback used to be the literal "skill", which is a name that
            # tells nobody anything: it goes in the log line, in the compile
            # filename, and now in what the usage page records, where a row
            # reading «Skill · skill» is worse than no row.
            skill_name = next(
                (n for n, p in skill_paths.items() if p == path),
                Path(path).parent.name or "skill",
            )

            python_code = _static_skill_translation(invocation, python_guide)
            if not python_code:
                python_code = await self._translate_skill_invocation(
                    skill_name, invocation, python_guide, spec.model
                )
                if python_code:
                    # The guide defines the API the call needs; prepend it so a
                    # translator that emitted only the bare call can't NameError.
                    python_code = f"{python_guide}\n\n{python_code}"
            if not python_code:
                logger.warning("Skill translation: no code generated for {}, falling back to read_file", skill_name)
                new_tool_calls[i] = ToolCallRequest(id=tc.id, name="read_file", arguments={"path": path})
                changed = True
                continue

            # The LLM fallback rewrites the guide rather than copying it, and a
            # rewrite can come back malformed. Shipping that to exec turns a bad
            # translation into a traceback the model then tries to work around
            # by hand — which is where the invalid commands come from. Reading
            # the skill instead keeps it on the documented path.
            try:
                compile(python_code, f"<skill:{skill_name}>", "exec")
            except (SyntaxError, ValueError) as exc:
                logger.warning(
                    "Skill translation produced invalid Python for {} ({}); falling back to read_file",
                    skill_name, exc,
                )
                new_tool_calls[i] = ToolCallRequest(id=tc.id, name="read_file", arguments={"path": path})
                changed = True
                continue

            encoded = _base64.b64encode(python_code.encode("utf-8")).decode("ascii")
            exec_cmd = (
                f"python -c \"import base64; exec(base64.b64decode(b'{encoded}').decode('utf-8'))\""
            )
            # Vouch for it before it is handed over: the exec tool's
            # reimplementation guard lets a skill's own generated code past,
            # and this is what tells it apart from the model wearing the same
            # base64 wrapper.
            register_skill_translation(exec_cmd)
            skills_by_index[i] = skill_name
            invocations_by_index[i] = {k: v for k, v in invocation.items() if k != "skill"}
            new_tool_calls[i] = ToolCallRequest(id=tc.id, name="exec", arguments={"command": exec_cmd})
            changed = True
            logger.info("Skill translation: {} → python exec (via {})", skill_name, tc.name)

        if not changed:
            return response
        out = dataclasses.replace(response, tool_calls=new_tool_calls)
        # Carried on the response rather than returned alongside it: this is a
        # note for whoever records the turn, and every other caller is entitled
        # to ignore it — including the ones that never look, which is why it is
        # read back with getattr and a default.
        if skills_by_index:
            out._skills_by_index = skills_by_index
            out._skill_invocations = invocations_by_index
        return out

    async def _translate_skill_invocation(
        self,
        skill_name: str,
        invocation: dict,
        python_guide: str,
        model: str,
    ) -> str | None:
        """Stateless LLM call: convert a skill invocation JSON to executable Python code."""
        invocation_str = _json.dumps(invocation, ensure_ascii=False, indent=2)
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a Python code translator. "
                    "Given a Python API and a skill invocation, output ONLY executable Python code. "
                    "No explanations, no markdown fences, no comments. "
                    "The API's definitions are prepended to your output automatically — do NOT repeat them. "
                    "Import json at the top. Call the right function and print the result with json.dumps()."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Python API for '{skill_name}':\n\n"
                    f"```python\n{python_guide}\n```\n\n"
                    f"Skill invocation to execute:\n{invocation_str}\n\n"
                    "Output only the Python code:"
                ),
            },
        ]
        try:
            resp = await asyncio.wait_for(
                self.provider.chat_with_retry(
                    messages=messages,
                    tools=None,
                    model=model,
                    retry_mode="standard",
                ),
                timeout=45.0,
            )
            code = (resp.content or "").strip()
            fence = re.search(r"```(?:python)?\n([\s\S]+?)\n```", code)
            if fence:
                code = fence.group(1).strip()
            return code if code else None
        except asyncio.TimeoutError:
            logger.warning("Skill translation timed out for {}", skill_name)
            return None
        except Exception as _e:
            logger.warning("Skill translation LLM call failed: {}", _e)
            return None

    @staticmethod
    def _apply_text_tool_call_parsing(
        response: LLMResponse,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """If the model returned no structured tool calls but wrote tool calls
        as plain text (e.g. ``exec(command="...")``) or as a JSON skill
        invocation (``{"skill": "name", ...}``), parse them and inject them so
        the runner executes them like native calls.

        Only activates when:
        - the response has no structured tool_calls yet
        - the response content contains at least one parseable call
        - finish_reason is not an error

        Every early return still strips invocation blocks out of the content.
        A response's text is shown to the user whether or not it also carries a
        structured tool call, so a model that makes a real call *and* writes a
        ``{"skill": ...}`` block beside it would otherwise publish raw JSON into
        the chat — which is exactly what happened to the camera stream URLs.
        A call that runs nothing is not "a real call" for that purpose: see
        _resolve_block_beside_calls, which hands those turns back here with the
        placeholders removed so the block itself gets resolved.
        """
        if response.finish_reason == "error":
            return _without_skill_invocation_text(response)
        if response.content and "DSML" in response.content:
            response = dataclasses.replace(response, content=_dsml_as_qwen35(response.content))
        if response.has_tool_calls:
            response = _lift_echoed_skill_blocks(response, spec.session_key)
        if response.has_tool_calls:
            response = _lift_skill_named_tool_calls(
                response, messages,
                frozenset(getattr(spec.tools, "tool_names", [])), spec.session_key)
        if response.has_tool_calls:
            response = _resolve_block_beside_calls(response, spec.session_key)
            if response.has_tool_calls:
                stripped_block = _has_skill_invocation_text(response.content)
                raw_content = response.content
                block_calls = (_calls_from_block(response.content, spec, messages)
                               if stripped_block and messages else [])
                response = _without_skill_invocation_text(response)
                if block_calls:
                    # Run it as well. Telling the model its block was dropped
                    # did not change what it wrote: on 2026-09-22 deepseek-v4-flash
                    # read that note twenty times in one turn and wrote block +
                    # exec every time, until the loop guard ended the turn. The
                    # block is the intent; the call beside it is habit.
                    logger.info(
                        "Text tool call parsing: running the skill-invocation block "
                        "written beside {} as well, for {}",
                        [tc.name for tc in response.tool_calls], spec.session_key or "default",
                    )
                    return dataclasses.replace(
                        response, tool_calls=[*response.tool_calls, *block_calls])
                if stripped_block:
                    logger.warning(
                        "Text tool call parsing: the block beside {} for {} resolved to "
                        "no call; it is stripped, not executed. It wrote: {!r}",
                        [tc.name for tc in response.tool_calls],
                        spec.session_key or "default", (raw_content or "")[:400],
                    )
                    # Read by the loop, which tells the model in the tool result.
                    response._stripped_skill_block = True
                return response
        if not response.content:
            return response

        tool_names = frozenset(getattr(spec.tools, "tool_names", []))
        if not tool_names:
            return _without_skill_invocation_text(response)

        # Build history of previous calls once for dedup and bash-block guards
        previously_called: set[tuple[str, str]] = (
            _collect_previous_calls(messages) if messages else set()
        )

        # 1. Python function-call syntax: tool_name(key="val", ...)
        text_calls = extract_text_tool_calls(response.content, tool_names)

        # 1b. XML-style: <tool_name attr="val" ...>
        if not text_calls:
            text_calls = extract_xml_tool_calls(response.content, tool_names)

        # 1c. Qwen3.5/Qwen3-Coder style: <function=name><parameter=k>v</parameter>...</function>
        if not text_calls:
            text_calls = extract_qwen35_tool_calls(response.content, tool_names)

        # 1d. Qwen3.5 skill invocation via <function=skill-name>...</function>
        if not text_calls and messages:
            text_calls = extract_qwen35_skill_invocations(response.content, messages, tool_names)

        # 1e. Qwen3/Hermes style: <tool_call>{"name": "...", "arguments": {...}}</tool_call>
        if not text_calls:
            text_calls = extract_hermes_tool_calls(response.content, tool_names)

        # 2. JSON skill invocation: {"skill": "name", ...}
        if not text_calls and messages:
            text_calls = extract_json_skill_invocations(
                response.content, messages, tool_names,
            )

        # 3. ```bash ... ``` code blocks → exec()  (only after reading a SKILL.md,
        #    and only when no exec has been text-parsed yet in this conversation)
        if not text_calls:
            exec_count = sum(1 for name, _ in previously_called if name == "exec")
            if exec_count == 0:
                text_calls = extract_bash_code_blocks(response.content, tool_names, messages)

        if not text_calls:
            # Nothing parsed. Almost always right — this is every ordinary text
            # answer — so it stays quiet unless the model *did* write something
            # shaped like an invocation and no extractor could resolve it (an
            # unknown skill name, a block the JSON scanner could not read).
            # That combination is rare, and it ends the turn with a promise and
            # no action, which is indistinguishable from the dedup stall below
            # by the time anyone sees the chat: the page strips the block before
            # rendering, so the person is left with the sentence and nothing to
            # report but "no hizo nada".
            if _has_skill_invocation_text(response.content):
                asked = _named_skill_invocations(response.content)
                logger.warning(
                    "Text tool call parsing: {} wrote a skill-invocation block that "
                    "resolved to no call ({}); the turn ends here with nothing executed. "
                    "It wrote: {!r}",
                    spec.session_key or "default", ", ".join(asked) or "unnamed",
                    response.content[:400],
                )
                # And say so, rather than only writing it down. The comment
                # above described this outcome exactly and left the person with
                # a sentence promising an action that never happened — which
                # is worse than an error, because it reads like success.
                #
                # A skill can be absent for an ordinary reason: the ones served
                # by their own service materialise only when that service
                # answers, so `code` is missing whenever the code-broker is
                # down. That is a fact about the house, not a mistake the
                # person made, and it is the sentence they need.
                available = sorted(_available_skill_names(spec))
                missing = [a for a in asked if a not in available]
                if missing:
                    one = len(missing) == 1
                    note = (
                        f"[No pude usar {' ni '.join(missing)}: "
                        + ("esa skill no está disponible" if one
                           else "esas skills no están disponibles")
                        + " en esta instancia. "
                        + (f"Tengo: {', '.join(available)}." if available
                           else "No tengo ninguna.")
                        + "]"
                    )
                else:
                    note = ("[Escribí una invocación que no se pudo interpretar, "
                            "así que no se ejecutó nada.]")
                clean = _strip_skill_invocation_text(response.content)
                response.content = f"{clean}\n\n{note}" if clean else note
                # The turn ends here with a promise and no action. Name that
                # as a stop reason so the loop can hand the same turn to a
                # stronger model instead of showing the note as an answer.
                response.bad_invocation = True
            return response

        # Deduplicate: drop calls whose exact (name, args) already appear in history
        # to prevent the model from looping on the same failed call.
        parsed_calls = text_calls
        text_calls = [
            tc for tc in text_calls
            if (tc.name, _stable_args_key(tc.arguments)) not in previously_called
        ]
        if not text_calls:
            # Everything parsed here already ran in this turn. Usually harmless:
            # the model restates the call alongside its actual answer, and all
            # that is needed is to keep the invocation syntax out of the chat —
            # returning it verbatim is what put raw {"skill": ...} blocks there.
            #
            # But this is ALSO the shape of a turn that dies mid-task. The model
            # says "voy a revisar la estructura exacta del skill document",
            # re-emits a read it already did, and gets nothing back — the turn
            # ends on the announcement, no tool runs, no background task, and
            # the person is left looking at a promise. Both outcomes leave this
            # function the same way, so log which one happened: this branch used
            # to be the only silent exit in the whole parser, and a stall that
            # writes nothing to the log cannot be told apart from a model that
            # simply chose to stop talking.
            logger.warning(
                "Text tool call parsing: dropped {} already-run call(s) {} for {}; "
                "nothing left to execute, so this response ends the turn",
                len(parsed_calls),
                [tc.name for tc in parsed_calls],
                spec.session_key or "default",
            )
            stripped = _strip_skill_invocation_text(response.content)
            if stripped != response.content:
                return dataclasses.replace(response, content=stripped)
            return response

        # Take only the first parsed call to avoid spurious multi-call responses
        # (models sometimes emit documentation/example calls in their explanation text)
        text_calls = text_calls[:1]

        logger.info(
            "Text tool call parsing: found {} call(s) in response text: {}",
            len(text_calls),
            [tc.name for tc in text_calls],
        )

        # Trim content to end at the tool call invocation, removing any
        # hallucinated "Result:" / "Expected:" text the model wrote after it.
        trimmed_content = response.content
        end_pos = _find_tool_call_end(response.content, text_calls[0])
        if end_pos is not None:
            trimmed_content = response.content[:end_pos].rstrip()
        # Trimming stops *after* the invocation, so what is left is the block
        # itself — and this turn's content is shown to the user, which is how a
        # raw {"skill": ...} ended up in the chat above the answer. The call now
        # lives in tool_calls as structured data, so the text is redundant.
        trimmed_content = _strip_skill_invocation_text(trimmed_content)

        return dataclasses.replace(
            response,
            content=trimmed_content,
            tool_calls=text_calls,
            finish_reason="tool_calls",
        )

    async def _request_finalization_retry(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ):
        retry_messages = list(messages)
        retry_messages.append(build_finalization_retry_message())
        kwargs = self._build_request_kwargs(spec, retry_messages, tools=None)
        return await self.provider.chat_with_retry(**kwargs)

    @staticmethod
    def _usage_dict(usage: dict[str, Any] | None) -> dict[str, int]:
        if not usage:
            return {}
        result: dict[str, int] = {}
        for key, value in usage.items():
            try:
                result[key] = int(value or 0)
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _accumulate_usage(target: dict[str, int], addition: dict[str, int]) -> None:
        for key, value in addition.items():
            target[key] = target.get(key, 0) + value

    @staticmethod
    def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
        merged = dict(left)
        for key, value in right.items():
            merged[key] = merged.get(key, 0) + value
        return merged

    async def _execute_tools(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
        external_lookup_counts: dict[str, int],
    ) -> tuple[list[Any], list[dict[str, str]], BaseException | None]:
        batches = self._partition_tool_batches(spec, tool_calls)
        tool_results: list[tuple[Any, dict[str, str], BaseException | None]] = []
        for batch in batches:
            if spec.concurrent_tools and len(batch) > 1:
                tool_results.extend(await asyncio.gather(*(
                    self._run_tool(spec, tool_call, external_lookup_counts)
                    for tool_call in batch
                )))
            else:
                for tool_call in batch:
                    tool_results.append(await self._run_tool(spec, tool_call, external_lookup_counts))

        results: list[Any] = []
        events: list[dict[str, str]] = []
        fatal_error: BaseException | None = None
        for result, event, error in tool_results:
            results.append(result)
            events.append(event)
            if error is not None and fatal_error is None:
                fatal_error = error
        return results, events, fatal_error

    async def _run_tool(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        external_lookup_counts: dict[str, int],
    ) -> tuple[Any, dict[str, str], BaseException | None]:
        _HINT = "\n\n[Analyze the error above and try a different approach.]"
        lookup_error = repeated_external_lookup_error(
            tool_call.name,
            tool_call.arguments,
            external_lookup_counts,
        )
        if lookup_error:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": "repeated external lookup blocked",
            }
            if spec.fail_on_tool_error:
                return lookup_error + _HINT, event, RuntimeError(lookup_error)
            return lookup_error + _HINT, event, None
        list_error = unrequested_list_item_error(
            tool_call.name, tool_call.arguments, spec.initial_messages,
        )
        if list_error:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": "list item nobody asked for",
            }
            if spec.fail_on_tool_error:
                return list_error, event, RuntimeError(list_error)
            return list_error, event, None
        prepare_call = getattr(spec.tools, "prepare_call", None)
        tool, params, prep_error = None, tool_call.arguments, None
        if callable(prepare_call):
            try:
                prepared = prepare_call(tool_call.name, tool_call.arguments)
                if isinstance(prepared, tuple) and len(prepared) == 3:
                    tool, params, prep_error = prepared
            except Exception:
                pass
        # Timed here rather than around the batch: tools in one iteration are
        # gathered, so the batch's wall time says nothing about which of them
        # the turn was actually waiting for. The profiler keeps the interval,
        # not just the duration, so overlapping tools can be unioned back into
        # a wall-time figure that does not exceed the turn.
        #
        # Above the prep-error return, not below it: a call that never reached
        # the tool -- unparseable arguments, a name nothing is registered under
        # -- is a failed tool call, and a turn that burned six iterations on
        # one used to show `n tools: 0` with its whole cost filed under
        # `other_ms`, i.e. as nanobot's own overhead. Same argument as the
        # string-returned failure below.
        _started = time.perf_counter()

        def _profile(status: str, detail: str = "", result: Any = None) -> None:
            PROFILER.record_tool(
                name=tool_call.name,
                duration_s=time.perf_counter() - _started,
                status=status,
                started_at=_started,
                detail=detail,
                result_chars=len(result) if isinstance(result, str) else 0,
            )

        if prep_error:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": prep_error.split(": ", 1)[-1][:120],
            }
            _profile("error", event["detail"])
            return prep_error + _HINT, event, RuntimeError(prep_error) if spec.fail_on_tool_error else None

        try:
            if tool is not None:
                result = await tool.execute(**params)
            else:
                result = await spec.tools.execute(tool_call.name, params)
        except asyncio.CancelledError:
            _profile("cancelled")
            raise
        except BaseException as exc:
            _profile("error", str(exc))
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": str(exc),
            }
            _log_error("runner.tool_error", exc, session_key=spec.session_key, extra={"tool": tool_call.name})
            if spec.fail_on_tool_error:
                return f"Error: {type(exc).__name__}: {exc}", event, exc
            return f"Error: {type(exc).__name__}: {exc}", event, None

        if isinstance(result, str) and result.startswith("Error"):
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": result.replace("\n", " ").strip()[:120],
            }
            # A tool that returns its failure as a string rather than raising is
            # still a failed tool: counting it as ok would make the panel's
            # error rate a measure of which tools use exceptions.
            #
            # Untruncated on purpose. `record_tool` redacts and then cuts, and
            # `redact()` replaces whole secret values -- so a token in raw exec
            # stderr that straddles the cut arrives as a fragment, matches
            # nothing, and is kept in a buffer the debug endpoint serves. The
            # 120-char form above is for the narration panel, which is a
            # different consumer with a different rule.
            _profile("error", result.replace("\n", " ").strip(), result)
            if spec.fail_on_tool_error:
                return result + _HINT, event, RuntimeError(result)
            return result + _HINT, event, None

        _profile("ok", result=result)
        detail = "" if result is None else str(result)
        detail = detail.replace("\n", " ").strip()
        if not detail:
            detail = "(empty)"
        elif len(detail) > 120:
            detail = detail[:120] + "..."
        return result, {"name": tool_call.name, "status": "ok", "detail": detail}, None

    async def _emit_checkpoint(
        self,
        spec: AgentRunSpec,
        payload: dict[str, Any],
    ) -> None:
        callback = spec.checkpoint_callback
        if callback is not None:
            await callback(payload)

    @staticmethod
    def _append_final_message(messages: list[dict[str, Any]], content: str | None) -> None:
        if not content:
            return
        if (
            messages
            and messages[-1].get("role") == "assistant"
            and not messages[-1].get("tool_calls")
        ):
            if messages[-1].get("content") == content:
                return
            messages[-1] = build_assistant_message(content)
            return
        messages.append(build_assistant_message(content))

    @staticmethod
    def _append_model_error_placeholder(messages: list[dict[str, Any]]) -> None:
        if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
            return
        messages.append(build_assistant_message(_PERSISTED_MODEL_ERROR_PLACEHOLDER))

    def _normalize_tool_result(
        self,
        spec: AgentRunSpec,
        tool_call_id: str,
        tool_name: str,
        result: Any,
    ) -> Any:
        result = ensure_nonempty_tool_result(tool_name, result)
        try:
            content = maybe_persist_tool_result(
                spec.workspace,
                spec.session_key,
                tool_call_id,
                result,
                max_chars=spec.max_tool_result_chars,
            )
        except Exception as exc:
            logger.warning(
                "Tool result persist failed for {} in {}: {}; using raw result",
                tool_call_id,
                spec.session_key or "default",
                exc,
            )
            content = result
        if isinstance(content, str) and len(content) > spec.max_tool_result_chars:
            return truncate_text(content, spec.max_tool_result_chars)
        return content

    @staticmethod
    def _drop_orphan_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Drop tool results that have no matching assistant tool_call earlier in the history."""
        declared: set[str] = set()
        updated: list[dict[str, Any]] | None = None
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
            if role == "tool":
                tid = msg.get("tool_call_id")
                if tid and str(tid) not in declared:
                    if updated is None:
                        updated = [dict(m) for m in messages[:idx]]
                    continue
            if updated is not None:
                updated.append(dict(msg))

        if updated is None:
            return messages
        return updated

    @staticmethod
    def _backfill_missing_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Insert synthetic error results for orphaned tool_use blocks."""
        declared: list[tuple[int, str, str]] = []  # (assistant_idx, call_id, name)
        fulfilled: set[str] = set()
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        name = ""
                        func = tc.get("function")
                        if isinstance(func, dict):
                            name = func.get("name", "")
                        declared.append((idx, str(tc["id"]), name))
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid:
                    fulfilled.add(str(tid))

        missing = [(ai, cid, name) for ai, cid, name in declared if cid not in fulfilled]
        if not missing:
            return messages

        updated = list(messages)
        offset = 0
        for assistant_idx, call_id, name in missing:
            insert_at = assistant_idx + 1 + offset
            while insert_at < len(updated) and updated[insert_at].get("role") == "tool":
                insert_at += 1
            updated.insert(insert_at, {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": _BACKFILL_CONTENT,
            })
            offset += 1
        return updated

    @staticmethod
    def _microcompact(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace old compactable tool results with one-line summaries."""
        compactable_indices: list[int] = []
        for idx, msg in enumerate(messages):
            if msg.get("role") == "tool" and msg.get("name") in _COMPACTABLE_TOOLS:
                compactable_indices.append(idx)

        if len(compactable_indices) <= _MICROCOMPACT_KEEP_RECENT:
            return messages

        stale = compactable_indices[: len(compactable_indices) - _MICROCOMPACT_KEEP_RECENT]
        updated: list[dict[str, Any]] | None = None
        for idx in stale:
            msg = messages[idx]
            content = msg.get("content")
            if not isinstance(content, str) or len(content) < _MICROCOMPACT_MIN_CHARS:
                continue
            name = msg.get("name", "tool")
            summary = f"[{name} result omitted from context]"
            if updated is None:
                updated = [dict(m) for m in messages]
            updated[idx]["content"] = summary

        return updated if updated is not None else messages

    def _apply_tool_result_budget(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        updated = messages
        for idx, message in enumerate(messages):
            if message.get("role") != "tool":
                continue
            normalized = self._normalize_tool_result(
                spec,
                str(message.get("tool_call_id") or f"tool_{idx}"),
                str(message.get("name") or "tool"),
                message.get("content"),
            )
            if normalized != message.get("content"):
                if updated is messages:
                    updated = [dict(m) for m in messages]
                updated[idx]["content"] = normalized
        return updated

    def _snip_history(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not messages or not spec.context_window_tokens:
            return messages

        # `context_block_limit` budgets the *conversation* -- everything but the
        # system prompt. It used to budget the whole prompt, which is not what
        # anyone setting it meant: the deployer writes it as "how much history a
        # turn may carry". Once the system prompt grew past the limit (~31k
        # against 16k on 2026-09-22) the history budget became the 128-token
        # floor below, every turn kept only the message just typed, and Alfred
        # answered "Revisa de nuevo" with "this conversation was just opened".
        # Nothing errored; it read as the model being forgetful.
        if spec.context_block_limit:
            non_system_tokens = sum(
                estimate_message_tokens(msg) for msg in messages if msg.get("role") != "system"
            )
            if non_system_tokens <= spec.context_block_limit:
                return messages
            system_messages = [dict(msg) for msg in messages if msg.get("role") == "system"]
            non_system = [dict(msg) for msg in messages if msg.get("role") != "system"]
            remaining_budget = spec.context_block_limit
        else:
            provider_max_tokens = getattr(getattr(self.provider, "generation", None), "max_tokens", 4096)
            max_output = spec.max_tokens if isinstance(spec.max_tokens, int) else (
                provider_max_tokens if isinstance(provider_max_tokens, int) else 4096
            )
            budget = spec.context_window_tokens - max_output - _SNIP_SAFETY_BUFFER
            if budget <= 0:
                return messages

            estimate, _ = estimate_prompt_tokens_chain(
                self.provider,
                spec.model,
                messages,
                spec.tools.get_definitions(),
            )
            if estimate <= budget:
                return messages

            system_messages = [dict(msg) for msg in messages if msg.get("role") == "system"]
            non_system = [dict(msg) for msg in messages if msg.get("role") != "system"]
            if not non_system:
                return messages

            system_tokens = sum(estimate_message_tokens(msg) for msg in system_messages)
            remaining_budget = max(128, budget - system_tokens)
            if budget - system_tokens < 128:
                logger.warning(
                    "History snip for {}: the system prompt (~{} tokens) fills the "
                    "{}-token budget; only the newest message reaches the model",
                    spec.session_key, system_tokens, budget,
                )
        kept: list[dict[str, Any]] = []
        kept_tokens = 0
        for message in reversed(non_system):
            msg_tokens = estimate_message_tokens(message)
            if kept and kept_tokens + msg_tokens > remaining_budget:
                break
            kept.append(message)
            kept_tokens += msg_tokens
        kept.reverse()

        if kept:
            for i, message in enumerate(kept):
                if message.get("role") == "user":
                    kept = kept[i:]
                    break
            else:
                # Recover nearest user message from outside the kept window;
                # GLM rejects system→assistant (error 1214).  Budget is
                # intentionally exceeded — oversized beats invalid.
                for idx in range(len(non_system) - 1, -1, -1):
                    if non_system[idx].get("role") == "user":
                        kept = non_system[idx:]
                        break
                # If no user exists at all, _enforce_role_alternation
                # will insert a synthetic one as a safety net.
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
        if not kept:
            kept = non_system[-min(len(non_system), 4) :]
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
        return system_messages + kept

    def _partition_tool_batches(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
    ) -> list[list[ToolCallRequest]]:
        if not spec.concurrent_tools:
            return [[tool_call] for tool_call in tool_calls]

        batches: list[list[ToolCallRequest]] = []
        current: list[ToolCallRequest] = []
        for tool_call in tool_calls:
            get_tool = getattr(spec.tools, "get", None)
            tool = get_tool(tool_call.name) if callable(get_tool) else None
            can_batch = bool(tool and tool.concurrency_safe)
            if can_batch:
                current.append(tool_call)
                continue
            if current:
                batches.append(current)
                current = []
            batches.append([tool_call])
        if current:
            batches.append(current)
        return batches

