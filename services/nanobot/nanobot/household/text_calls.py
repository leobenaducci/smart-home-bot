"""household: text tool calls and JSON skill invocations (fork runner, verbatim).

Upstream v0.4.1 deliberately requires structured tool calls (8b37976d3,
"Require structured tool calls"). The household's local models write calls
as text -- DSML, XML, Qwen ``<function=...>``, Hermes ``<tool_call>``,
Python-call syntax, ``{"skill": ...}`` blocks and bash fences -- so this
module stays ours. It is applied from ``HouseholdResponseHook.on_model_response``
(``nanobot/household/hooks.py``) through the runner's one household patch.

Everything below the header is the fork's runner helpers, moved unchanged
except for imports and ``_collect_skill_paths`` (v0.4.1's skills summary
lists paths relative to a section root).
"""

from __future__ import annotations

import ast
import dataclasses
import os
import re
import secrets
import shlex
import string
from pathlib import Path
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from loguru import logger

from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.household.profiling import note_event
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


import json as _json

from nanobot.household.skill_invocation import (
    register_skill_translation,
)

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
    # devices (the household's phones and tablets). Not `set_volume`: a model
    # calling that as a function may mean a TV, which is Home Assistant.
    "open_app": "devices", "ring_device": "devices", "stop_ring_device": "devices",
    "list_devices": "devices", "list_apps": "devices",
    # studio (the house's own generator)
    "make_image": "studio", "make_song": "studio", "make_instrumental": "studio",
    "make_video": "studio", "studio_queue": "studio",
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
    from nanobot.household.skills import SKILL_ALIASES

    candidates = (raw.strip().lower(), _normalize_skill_name(raw))
    for candidate in candidates:
        if candidate in skill_paths:
            return candidate
    for candidate in candidates:
        renamed = SKILL_ALIASES.get(candidate)
        if renamed in skill_paths:
            return renamed
    return None


# v0.4.1 groups the summary under "### <label> (`<root>`)" headings and
# lists each path relative to that root.
_SKILL_SECTION_RE = re.compile(r"^###\s+[^\n]*\(`([^`\n]+)`\)\s*$", re.MULTILINE)
SKILL_ROOT_WORKSPACE: ContextVar[Path | None] = ContextVar("household_skill_root", default=None)


def _collect_skill_paths(messages: list[dict]) -> dict[str, str]:
    """Build a mapping {skill_name: skill_path} from the system message."""
    paths: dict[str, str] = {}
    workspace = SKILL_ROOT_WORKSPACE.get()
    for msg in messages:
        if msg.get("role") != "system":
            continue
        body = msg.get("content", "")
        if not isinstance(body, str):
            continue
        sections = [(m.start(), m.group(1).strip()) for m in _SKILL_SECTION_RE.finditer(body)]
        for m in _SKILL_PATH_RE.finditer(body):
            path = m.group(2).strip()
            root = next((r for pos, r in reversed(sections) if pos < m.start()), None)
            if root and not Path(path).is_absolute():
                base = Path(root)
                if not base.is_absolute() and workspace is not None:
                    base = workspace / base
                path = str(base / path)
            paths[m.group(1).strip().lower()] = path
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
# The bars come doubled too, and the wrapper is sometimes just "calls":
# deepseek-v4.1-flash answered a Studio request on 2026-09-28 with
#   <｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="exec">...
# which the single-bar pattern let through verbatim, call unrun.
_BAR = r"[｜|]+"
_DSML = rf"<\s*/?\s*{_BAR}\s*DSML\s*{_BAR}\s*"
_DSML_OPEN = rf"<\s*{_BAR}\s*DSML\s*{_BAR}\s*"
_DSML_CLOSE = rf"<\s*/\s*{_BAR}\s*DSML\s*{_BAR}\s*"
_DSML_REWRITES = (
    (re.compile(_DSML_OPEN + r"invoke\s+name\s*=\s*\"([^\"]+)\"\s*>", re.I), r"<function=\1>"),
    (re.compile(_DSML_CLOSE + r"invoke\s*>", re.I), "</function>"),
    (re.compile(_DSML_OPEN + r"parameter\s+name\s*=\s*\"(\w+)\"[^>]*>", re.I), r"<parameter=\1>"),
    (re.compile(_DSML_CLOSE + r"parameter\s*>", re.I), "</parameter>"),
    (re.compile(_DSML + r"(?:tool_calls|function_calls|calls)\s*>", re.I), ""),
    # Anything else in the markup is not for a person to read.
    (re.compile(_DSML + r"[^>]*>", re.I), ""),
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


def _available_skill_names(spec: "TurnView") -> set[str]:
    """Which skills this instance actually has, by directory.

    The same three roots SkillsLoader reads, in the same order, and read from
    disk rather than asked of the loader: this runs on the failure path, where
    the useful thing is what is on the filesystem right now — a remote skill
    materialises only while its service answers, so `code` is present or absent
    depending on whether the code-broker is up.
    """
    from nanobot.household.remote_skills import cache_dir as _remote_cache_dir
    from nanobot.agent.skill_metadata import BUILTIN_SKILLS_DIR

    from nanobot.household.skill_roots import HOUSEHOLD_SKILLS_DIR

    roots = [BUILTIN_SKILLS_DIR, HOUSEHOLD_SKILLS_DIR]
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
    #
    # Only when a fence really is left open, by count. The pattern alone also
    # matches the *closing* fence of an ordinary code block, and then every
    # reply ending in one -- a script, the Studio's JSON shot list -- read as a
    # broken invocation and was replaced with "nothing was executed".
    if out.count("```") % 2 == 1:
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
    note_event("parse:block_beside_calls")
    logger.warning(
        "Text tool call parsing: {} wrote a skill-invocation block beside {}; "
        "the block is taken out of the text and run too if it resolves",
        session_key or "default", [tc.name for tc in response.tool_calls],
    )
    return response


def _calls_from_block(content: str | None, spec: "TurnView",
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


def _budget(spec: "TurnView"):
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
class TurnView:
    """The parts of the fork's AgentRunSpec the parsers read."""

    tools: Any
    workspace: Path | None = None
    session_key: str | None = None
    max_iterations: int = 200


# Moved from the fork's AgentRunner._apply_text_tool_call_parsing.
def apply_text_tool_call_parsing(
    response: LLMResponse,
    spec: TurnView,
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
        note_event("parse:dsml")
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
                note_event("parse:block_unresolved")
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
            note_event("parse:block_no_call")
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
        note_event("parse:dup_calls")
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
