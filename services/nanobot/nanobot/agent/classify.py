"""Which tier a turn starts on: a guess before the fact.

The house runs two chat models -- a cheap one somebody is waiting on, and a
strong one for hard turns somebody is also waiting on -- and has the everyday
one plan its way through work of several steps, or hands work of hours to a
sub-agent (``long`` and ``background``, see LABELS). Until 2026-09-21 the choice was
the caller's: HomeCore marks the turns inside a Profesión, the main model marks
a sub-agent it spawns with ``complex=true``, and everything else runs cheap.
Nothing looked at the request itself, so *buscá las luces azules y verificá
que el nombre coincida en WiZ, lights y HA* -- three systems and a comparison
-- ran on the model that answers *prendé la luz del living* in prose without
invoking anything (measured: 5/7 tool cases against the strong model's 7/7).

This module is the look. A small local model reads the request and answers
with one of three labels; the label picks the tier, the reasoning effort and
the iteration budget. It is a *hint*: the turn never waits more than
``timeout_s`` for it, a parse failure means ``action`` (the cheap tier with
escalation armed), and the caller's own flags always win. What the classifier
gets wrong, escalation catches after the fact -- see ``AgentLoop`` -- and the
two together are what makes either of them safe to trust.

The model is `gemma4:e4b` on the local Ollama by default: already resident as
`titles`/`notifications`/`heartbeat`, it triages notifications at effort
``none`` today, and a title costs it a median 0.29 s. Nothing here needs an
embedding API, a second GPU model, or a router trained on somebody else's
chat -- the labels are the house's own.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable

from loguru import logger

# Five kinds, three places (2026-09-24). `complex` is a hard question somebody
# is waiting on -- the powerful model, in the chat, the only turns it gets.
# `long` is work of several steps done now: the everyday model in the chat,
# with a plan it ticks off (tools/plan.py), able to ask as it goes.
# `background` is work of hours, or anything asked for in the background: a
# sub-agent, which answers in the same conversation when it is done.
LABELS = ("chat", "action", "complex", "long", "background")
TIER_FOR_LABEL = {"chat": "everyday", "action": "everyday", "complex": "powerful",
                  "long": "everyday", "background": "subagent"}

# The runner's own end-of-turn states that mean "the cheap attempt did not
# finish the job". `bad_invocation` is the one this work added: a skill block
# the model wrote that resolved to no call used to end the turn as "completed",
# which is exactly the failure nobody could see.
DEFAULT_ESCALATE_ON = (
    "bad_invocation",
    "error",
    "empty_final_response",
    "repeated_tool_calls",
    "max_iterations",
)

_PROMPT = """Classify the household's request to its assistant. Answer with ONE JSON line and nothing else:
{{"label": "chat" | "action" | "complex" | "long" | "background", "reason": "<a few words>"}}

chat    - conversation, a fact, a translation, a joke, a quick question, thanks
action  - one or two steps in ONE system: turn something on or off, take a photo, add to a list, set a reminder, ask what a sensor says
complex - a hard question the person is waiting on, answered in one go: reasoning, a calculation, a decision, advice, explaining something in depth, a short piece of code
long    - work of several steps to do now: more than one system, checking or comparing or cross-referencing one thing against another, reviewing or fixing something, going through a list
background - work of hours, research to read later, a report or document to prepare, or anything the person asks to be done "in the background" / "en segundo plano"

Examples:
"prendé la luz del living" -> action
"¿qué hora es?" -> chat
"sacá una foto del patio" -> action
"agregá leche a la lista" -> action
"¿me conviene pagar la tarjeta en cuotas o al contado con este interés?" -> complex
"explicame bien la diferencia entre un fondo mutuo y un depósito a plazo" -> complex
"buscá las luces azules y verificá que el nombre coincida en wiz, lights y HA" -> long
"hacé una referencia cruzada de las luces con Home Assistant" -> long
"investigá esto durante un par de horas" -> background
"revisá por qué el backup de anoche falló y arreglalo" -> long
"compará los precios de estas tres cámaras" -> long
"en segundo plano, armame un resumen de los gastos del mes" -> background
"armame un informe con todo lo que pasó con la calefacción este mes" -> background
"gracias!" -> chat
"sí, dale" (after the assistant proposed a long task) -> long
"sí, dale" (after the assistant proposed something simple) -> action

Previous assistant turn: {previous}
Attachments: {attachments}
Message: {message}
"""

_JSON_RE = re.compile(r"\{[^{}]*\}")
_PREVIOUS_CHARS = 400
_MESSAGE_CHARS = 1500


@dataclass(slots=True)
class TurnClass:
    """What the router decided for one turn, and why.

    ``source`` says who decided: ``forced`` (the caller's flag), ``sticky`` (a
    recent escalation in this session), ``fast_path`` (attachments or length,
    no model asked), ``model`` (the classifier answered), ``default`` (it did
    not, in time or at all), ``off`` (routing disabled). ``ms`` is what the
    decision cost in wall time, because a hint that takes longer than the
    turn it helps is not a hint.
    """

    label: str
    tier: str
    reason: str = ""
    source: str = "default"
    ms: float = 0.0
    escalated_from: str | None = None

    def as_record(self) -> dict[str, Any]:
        """The shape the usage report carries; small on purpose."""
        return {
            "tier": self.tier,
            "label": self.label,
            "source": self.source,
            "classifier_ms": round(self.ms, 1),
            "escalated": bool(self.escalated_from),
            "escalated_from": self.escalated_from or "",
        }


def parse_label(text: str | None) -> tuple[str, str] | None:
    """The (label, reason) in a classifier answer, or None if there is none.

    Tolerant on purpose: a small model at effort none writes the JSON line it
    was asked for most of the time, occasionally inside a fence or after a
    word. The first ``{...}`` with a known label wins; a bare label word on
    its own line is accepted too.
    """
    if not text:
        return None
    for m in _JSON_RE.finditer(text):
        try:
            obj = json.loads(m.group(0))
        except (ValueError, TypeError):
            continue
        label = str(obj.get("label") or "").strip().lower()
        if label in LABELS:
            return label, str(obj.get("reason") or "")[:120]
    lowered = text.strip().lower()
    for label in LABELS:
        if re.fullmatch(rf"[\"'`\s]*{label}[\"'`\s.]*", lowered):
            return label, ""
    return None


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(b.get("text") or "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def previous_assistant_text(messages: Iterable[dict[str, Any]]) -> str:
    """The last assistant message before the current one, as plain text."""
    last = ""
    for m in messages:
        if m.get("role") == "assistant":
            t = _text_of(m.get("content"))
            if t.strip():
                last = t
    return last


class TurnClassifier:
    """Ask a small model which of three kinds of request this is."""

    def __init__(
        self,
        provider: Any,
        model: str,
        *,
        timeout_s: float = 1.5,
        long_message_chars: int = 600,
        reasoning_effort: str | None = "none",
    ) -> None:
        self.provider = provider
        self.model = model
        self.timeout_s = timeout_s
        self.long_message_chars = long_message_chars
        self.reasoning_effort = reasoning_effort

    def fast_path(self, text: str, *, attachments: int = 0) -> TurnClass | None:
        """The cases that need no model, and the ones a dead model must not lose."""
        if attachments > 0:
            return TurnClass("complex", "powerful", "attachment", "fast_path")
        if len(text or "") > self.long_message_chars:
            # A long message is usually work handed over -- a pasted document,
            # a list of things to do -- not a question to answer fast.
            return TurnClass("long", "everyday", f"{len(text)} chars", "fast_path")
        if not (text or "").strip():
            return TurnClass("chat", "everyday", "empty", "fast_path")
        return None

    async def classify(
        self,
        text: str,
        *,
        previous: str = "",
        attachments: int = 0,
    ) -> TurnClass:
        started = time.monotonic()
        fast = self.fast_path(text, attachments=attachments)
        if fast is not None:
            fast.ms = (time.monotonic() - started) * 1000
            return fast
        prompt = _PROMPT.format(
            previous=json.dumps((previous or "")[-_PREVIOUS_CHARS:], ensure_ascii=False) if previous else "(none)",
            attachments=attachments,
            message=json.dumps(text[:_MESSAGE_CHARS], ensure_ascii=False),
        )
        # Where the classifier's model is served right now: an outage or a
        # detour moves it, and chat() alone would not follow (base.routed).
        from nanobot.providers.base import LLMProvider
        provider, model = (self.provider.routed(self.model) if isinstance(self.provider, LLMProvider)
                           else (self.provider, self.model))
        try:
            response = await asyncio.wait_for(
                provider.chat(
                    messages=[{"role": "user", "content": prompt}],
                    model=model,
                    max_tokens=80,
                    temperature=0.0,
                    reasoning_effort=self.reasoning_effort,
                ),
                timeout=self.timeout_s,
            )
        except asyncio.TimeoutError:
            ms = (time.monotonic() - started) * 1000
            logger.warning("Turn classifier timed out after {:.0f}ms; defaulting to action", ms)
            return TurnClass("action", "everyday", "classifier timeout", "default", ms)
        except Exception as e:  # the classifier is instrumentation, never a failure
            ms = (time.monotonic() - started) * 1000
            logger.warning("Turn classifier failed ({}); defaulting to action", e)
            return TurnClass("action", "everyday", f"classifier error: {e}"[:120], "default", ms)
        ms = (time.monotonic() - started) * 1000
        parsed = parse_label(getattr(response, "content", None))
        if parsed is None:
            logger.info("Turn classifier answered nothing usable ({!r}); defaulting to action",
                        (getattr(response, "content", "") or "")[:80])
            return TurnClass("action", "everyday", "unparseable", "default", ms)
        label, reason = parsed
        return TurnClass(label, TIER_FOR_LABEL[label], reason, "model", ms)


def continuation_messages(
    messages: list[dict[str, Any]],
    stop_reason: str,
    *,
    tools_ran: bool,
) -> list[dict[str, Any]]:
    """The conversation a stronger model takes over from a failed attempt.

    Two shapes. When nothing executed (``bad_invocation``, an empty or errored
    answer) the attempt left no trace worth keeping, so the trailing assistant
    message -- the note, the placeholder, the error string -- is dropped and
    the strong model answers the same conversation fresh. When tools *did* run
    (a loop cut short, an iteration budget spent) their results stay: the
    strong model continues from where the cheap one stalled rather than
    re-running side effects, and one bracketed line tells it why it is here.
    Re-running `add_grocery` because the model looped afterwards is the
    mistake that rule exists to stop.
    """
    out = list(messages)
    if out and out[-1].get("role") == "assistant" and not out[-1].get("tool_calls"):
        out.pop()
    if not tools_ran:
        return out
    out.append({
        "role": "user",
        "content": (
            f"[The previous attempt on a smaller model ended with `{stop_reason}`. "
            "Continue from the tool results above and finish the task. "
            "Do not repeat calls that already ran; if they answered the "
            "question, write the answer.]"
        ),
    })
    return out
