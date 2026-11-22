"""Recognising a skill-invocation block where one does not belong.

A skill whose description starts with ``Invoke with JSON`` is used by writing
its block as **plain text in the reply**. The runner watches that one path and
consumes the block before anyone sees it.

The model does not always use that path. Measured across 46 live probes on one
instance, it pushed the block out through two side doors that nothing was
watching:

    MessageTool   content={"skill":"geo","action":"list_places"}
    ExecTool      echo '{"skill":"file-share","action":"list_files"}'

Neither is intercepted, so the raw JSON reached the family verbatim — nine
times out of forty-six, twice ending in no answer at all because the block went
out the side door and the turn never produced a reply. Alfred sometimes noticed
and apologised in the chat: "that was my invocation block, not a message".

Stripping at each exit was the other option and it is the weaker one: it hides
the mistake, the model learns nothing, and the skill still does not run. These
helpers let the tool *refuse* and say what to write instead, so the turn
recovers on the next step.

``cron`` uses the same detection for a different reason — a job body containing
a block means nothing when it fires days later.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from collections import OrderedDict

# Two sets, because the two callers face opposite risks.
#
# ECHO_LIKE is the *refusal* set: a skill block handed to any of these is the
# model trying to say it through the shell, and refusing costs nothing even
# when the guess is wrong. `cat` earns its place here — `cat '{"skill":…}'` is
# a mistake worth naming — but it does NOT print its argument, it opens it as a
# filename.
#
# That difference is why the reroute cannot share the set. Rerouting *executes*
# the block, so a command that was never going to print anything becomes a real
# call: `cat '{"skill":"tasks","action":"delete_chore","task_id":9,"confirm":true}'`
# would delete chore 9, with delete_chore's confirmation guard satisfied by a
# literal the model only meant to display. Over-matching costs a round trip on
# one side and a write on the other, so the execute side gets the strictly
# narrower set.
ECHO_LIKE = frozenset({"echo", "printf", "cat"})
REROUTABLE_ECHO_LIKE = frozenset({"echo", "printf"})

# Commands the translator generated, by digest. The exec tool lets a skill's
# own code past the reimplementation guard; this is how it knows the code is
# the skill's rather than the model's.
#
# Shape used to be the whole test — anything starting `python -c "import
# base64; exec(base64.b64decode(` was taken as the translator's. But the
# translator's output lands in the conversation as a tool call, so the model
# reads that shape and writes it back, and a hand-rolled payload wearing the
# wrapper inherited a free pass. Observed on 2026-08-11: asked whose turn it
# was to wash the dishes, the model base64'd a sixteen-line copy of the tasks
# skill's internals and called its private `_curl` helper with a path it made
# up. TASKS_API_URL and /tasks/api were both in there, both on the owned list,
# and neither was ever looked at.
#
# Registration is not consumed on use. A retry, or the model replaying a
# translated command verbatim, re-runs the skill's own code — which is what
# would have happened anyway. What the ledger has to catch is a *changed*
# payload, and any change is a different digest.
_TRANSLATION_LEDGER: OrderedDict[str, None] = OrderedDict()
_LEDGER_CAP = 128


def _translation_digest(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def register_skill_translation(command: str) -> None:
    """Record *command* as translator-generated, evicting the oldest entries.

    Bounded because a translated call that is cancelled before it runs — the
    stop button, a turn that ends first — never comes back to be checked.
    """
    digest = _translation_digest(command)
    _TRANSLATION_LEDGER[digest] = None
    _TRANSLATION_LEDGER.move_to_end(digest)
    while len(_TRANSLATION_LEDGER) > _LEDGER_CAP:
        _TRANSLATION_LEDGER.popitem(last=False)


def is_registered_skill_translation(command: str) -> bool:
    """True when *command* is one the translator emitted, byte for byte."""
    return _translation_digest(command) in _TRANSLATION_LEDGER


# Fenced (```json { "skill": ... } ```) or bare ({ "skill": ... }).
# "skill" must be a JSON key, not the word in a sentence, so a message like
# "use the cameras skill" is untouched.
SKILL_BLOCK_RE = re.compile(
    r'```(?:json)?\s*\{[\s\S]*?"skill"[\s\S]*?\}\s*```'
    r'|\{\s*"skill"\s*:\s*"[^"]*"[\s\S]*?\}',
    re.IGNORECASE,
)

# Wording that describes the plumbing rather than a task, e.g. the observed
# "Recordatorio skill JSON invocation".
INVOCATION_DEBRIS_RE = re.compile(
    r"(?:recordatorio\s+)?skill\s+(?:json\s+)?invocation\s*(?:block)?",
    re.IGNORECASE,
)


def contains_skill_invocation(text: str | None) -> bool:
    """True when *text* carries a skill-invocation block anywhere in it.

    Accepts None because callers hold optional strings, and a blank one has
    once turned into a TypeError from a regex that took down a whole turn.
    """
    if not text:
        return False
    return bool(SKILL_BLOCK_RE.search(text))


def is_skill_invocation_json(text: str | None) -> bool:
    """True when *text* is, in its entirety, one invocation object.

    Stricter than :func:`contains_skill_invocation` — it parses. Used where a
    false positive is expensive, such as deciding whether a shell command's
    payload is a block being echoed rather than data that merely looks like one.
    """
    if not text:
        return False
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return False
    try:
        data = json.loads(stripped)
    except Exception:
        return False
    return isinstance(data, dict) and "skill" in data


def echoed_skill_invocation(command: str | None) -> dict | None:
    """The invocation *command* does nothing but echo, if that is all it does.

    ``echo '{"skill":"grocery","action":"add_grocery","name":"cafe"}'`` — the
    model reaching for the shell to "say" the block, which the runner reroutes
    to the skill rather than spending a refusal on.

    Only REROUTABLE_ECHO_LIKE, because this answer gets *executed*: see the note
    on those sets above for why `cat` is refusable but not reroutable.

    Matched on the echoed *argument*, not anywhere in the command, so a script
    that legitimately handles JSON containing a "skill" key still runs. Exactly
    one argument, so ``echo '{...}' > file`` is not mistaken for an invocation.
    ``shlex`` decides where the argument ends, the same splitting the shell does.

    Returns None for anything that is not a well-formed invocation naming a
    skill as a string. The caller reroutes on a dict and leaves everything else
    to ExecTool, so a None here is a refusal, never a crash: this used to hand
    back ``{"skill": None}`` and take the whole turn down with it.
    """
    if not isinstance(command, str) or "skill" not in command:
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None                        # unbalanced quotes; bash -n reports it
    if len(argv) != 2 or os.path.basename(argv[0]) not in REROUTABLE_ECHO_LIKE:
        return None
    if not is_skill_invocation_json(argv[1]):
        return None
    invocation = json.loads(argv[1].strip())
    if not isinstance(invocation.get("skill"), str):
        return None
    return invocation


def emit_as_text_hint(where: str) -> str:
    """The correction, phrased the same way wherever it is refused.

    Says what to do rather than only what went wrong: a refusal the model
    cannot act on costs the same turn twice.
    """
    return (
        f"Error: that is a skill-invocation block, and {where} is not how one is "
        "used. Write the block as plain text in your reply — on its own, with no "
        "surrounding words — and the runtime will execute it and hand you the "
        "result. Do not echo it through a shell, and do not send it as a message: "
        "either way the family just sees the JSON."
    )
