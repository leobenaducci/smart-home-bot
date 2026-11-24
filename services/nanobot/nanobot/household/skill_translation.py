"""household: JSON skill invocations become Python exec calls (fork A2).

A ``{"skill": ..., "action": ...}`` block (parsed in text_calls) or a call to
a hallucinated wrapper tool resolves to its SKILL.md; the skill's Python
guide is translated statically when the call maps onto it, else by one
stateless model call, and runs through ``exec``. Moved from the fork's
``AgentRunner._maybe_translate_skill_calls``; three refusals that rewrote
the call into ``exec printf`` now go through the runner's veto
(``ModelResponseAction.refuse``), so they work whether or not the step has a
shell:

- another member's question (``ask``) and a WhatsApp sender only *read*;
- a call marked as already confirmed (the go-ahead gate for house actions)
  is refused until the person has answered the pending question;
- a read-only plan step refuses a write.
"""

from __future__ import annotations

import asyncio
import base64 as _base64
import dataclasses
import json as _json
import re
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.household.profiling import note_event
from nanobot.household.skill_invocation import echoed_skill_invocation as _echoed_skill_invocation
from nanobot.household.skill_invocation import register_skill_translation
from nanobot.household.text_calls import (
    _ASK_READABLE_ACTIONS,
    _WHATSAPP_READABLE_ACTIONS,
    _collect_skill_paths,
    _has_go_ahead,
    _load_skill_python_guide,
    _person_answered_pending,
    _resolve_skill_name,
    _runtime_printf,
    _static_skill_translation,
    is_read_action,
)
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.utils.homeweb_chat_id import is_third_party_session, is_whatsapp_session


def _refuse(
    calls: list[ToolCallRequest],
    index: int,
    tc: ToolCallRequest,
    text: str,
    refusals: dict[str, str] | None,
) -> None:
    """Answer the call with *text* instead of running it."""
    if refusals is not None:
        refusals[tc.id] = text
        return
    # No veto channel (a caller outside the runner): the fork's exec printf.
    calls[index] = ToolCallRequest(id=tc.id, name="exec", arguments={"command": _runtime_printf(text)})


async def maybe_translate_skill_calls(
    response: "LLMResponse",
    spec: Any,
    messages: list[dict],
    *,
    provider: Any,
    model: str,
    refusals: dict[str, str] | None = None,
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
                note_event("refused:whatsapp" if from_whatsapp else "refused:ask")
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
                _refuse(new_tool_calls, i, tc, excuse, refusals)
                changed = True
                continue

        if _has_go_ahead(invocation) and not _person_answered_pending(messages):
            action = str(invocation.get("action", ""))
            note_event("refused:go_ahead")
            logger.warning("refused a go-ahead nobody gave: {} on skill {}",
                           action, invocation.get("skill"))
            refusal = (f"'{action}' was called as already confirmed, but the person has "
                       f"not been asked. Call it without the confirmation to see what it "
                       f"would do, tell the person, and wait for their yes.")
            _refuse(new_tool_calls, i, tc, refusal, refusals)
            changed = True
            continue

        if getattr(spec, "read_only", False) and not is_read_action(invocation):
            action = str(invocation.get("action", ""))
            note_event("refused:read_only")
            logger.warning("read-only plan step: refused action {} on skill {}",
                           action, invocation.get("skill"))
            if getattr(spec, "refused", None) is not None:
                spec.refused.append(f"{invocation.get('skill')}.{action}")
            refusal = (f"This step only reads, so '{action}' was not run: it changes "
                       f"something. Finish the step with what you found, and say what "
                       f"you would change -- the person is asked first.")
            _refuse(new_tool_calls, i, tc, refusal, refusals)
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
            python_code = await translate_skill_invocation(
                provider, skill_name, invocation, python_guide, model,
            )
            if python_code:
                # The guide defines the API the call needs; prepend it so a
                # translator that emitted only the bare call can't NameError.
                python_code = f"{python_guide}\n\n{python_code}"
        if not python_code:
            note_event(f"skill:no_code:{skill_name}")
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
            note_event(f"skill:bad_code:{skill_name}")
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


async def translate_skill_invocation(
    provider: Any,
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
            provider.chat_with_retry(
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
        note_event(f"skill:timeout:{skill_name}")
        logger.warning("Skill translation timed out for {}", skill_name)
        return None
    except Exception as _e:
        note_event(f"skill:failed:{skill_name}")
        logger.warning("Skill translation LLM call failed: {}", _e)
        return None
