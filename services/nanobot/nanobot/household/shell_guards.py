"""household: the exec tool's preflight guards (fork A4).

Checked on the command as written, before upstream's ExecTool prepares or
runs it (``ExecTool.execute`` calls ``_preflight_error`` -- the seam the
assessors named, moved one step earlier because the bash syntax probe is
async). Each guard returns the text the model reads instead of running the
command:

- a command that does nothing (``true``, ``:``), which models write beside a
  skill block to "make a call";
- a skill-invocation block echoed through the shell;
- a hand-written reimplementation of a skill (its service URL, credential or
  base64 wrapper) that was not emitted by the skill translator;
- a command bash cannot parse, or a ``python -c`` payload with a syntax error.

``ExecTool`` takes these as a mixin, so the fork's tests that call
``ExecTool._noop_command_error`` and friends run unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shlex
import shutil
import sys

from loguru import logger

from nanobot.household.skill_invocation import (
    ECHO_LIKE,
    emit_as_text_hint,
    is_registered_skill_translation,
    is_skill_invocation_json,
)

_IS_WINDOWS = sys.platform == "win32"

# Payload of the base64 wrapper the skill translator emits, and that the model
# imitates by hand when it decides to reimplement a skill.
_B64_PAYLOAD_RE = re.compile(r"b64decode\(\s*b?['\"]([A-Za-z0-9+/=]+)['\"]")
_PY_EXE = frozenset({"python", "python3", "python3.11", "python3.12", "python3.13"})

# Skills run through exec like anything else, so the reimplementation guard
# below has to let their own generated code past. It asks
# is_registered_skill_translation, which the translator writes to as it emits
# the command (see _maybe_translate_skill_calls). Matching the *shape* of that
# command was the previous test and it is not one: the shape is visible to the
# model and the model copied it.

# Services a skill already owns. Reaching one of these by hand means
# reimplementing that skill — badly, and usually with its credential on the
# command line. Observed: asked for a PDF from Paperless, the model wrote its
# own python3 -c against PAPERLESS_URL, saved the file outside the workspace
# where the chat cannot serve it, and reported "I sent them the file" having
# sent nothing.
#
# Order is load-bearing: the first match names the skill the refusal tells the
# model to use, so the *specific* service has to be found before the shared
# base it is built on. grocery, menu, geo and family-message all reach HomeCore
# by rewriting the tasks URL —
#
#     BASE = os.environ.get("TASKS_API_URL", ...).replace("/tasks/api", "/grocery/api")
#
# — so their own generated code carries TASKS_API_URL and /tasks/api too. With
# the tasks entries first, every one of them was refused with "use the `tasks`
# skill", which is advice that cannot be followed when the request is about the
# shopping list. Live on 2026-08-12, "add 2 more of those": fifteen rounds of
# the model trying to comply with an impossible instruction, ~60k prompt tokens
# each, before the turn ran out. The list came out right; nothing else did.
_SKILL_OWNED_SIGNALS: tuple[tuple[str, str], ...] = (
    ("PAPERLESS_URL", "paperless"),
    ("PAPERLESS_API_TOKEN", "paperless"),
    ("HOME_LIGHTS_API_URL", "lights"),
    ("NANOBOT_N8N", "n8n"),
    # Built on the tasks URL — these must come first.
    ("/geo/api", "geo"),
    ("/devices/api", "devices"),
    ("/studio/api", "studio"),
    ("/chat/notifications", "notifications"),
    ("/chat/whatsapp", "whatsapp"),
    ("/chat/dm", "family-message"),
    ("/chat/ask-family", "family-message"),
    ("/menu/api", "menu"),
    ("/grocery/api", "grocery"),
    ("TASKS_API_URL", "tasks"),
    ("/tasks/api", "tasks"),
)


def _host_signals() -> tuple[tuple[str, str], ...]:
    """The `host:port` of each service that owns a skill, read from its URL.

    These used to be three literals ending in `.home`. A household whose
    services answer to any other name -- which is most of them, since the
    suffix is a setting -- had three signals that could never match, so a model
    curling its own paperless box was never redirected to the `paperless`
    skill. The address is in the environment the deployer already supplies, so
    read it there rather than guessing what somebody calls their machines.
    """
    out: list[tuple[str, str]] = []
    for var, skill in (("PAPERLESS_URL", "paperless"),
                       ("CAMERA_API_URL", "camera-feed"),
                       ("NANOBOT_N8N_BASE_URL", "n8n")):
        url = (os.environ.get(var) or "").strip()
        if not url:
            continue
        host = url.split("://", 1)[-1].split("/", 1)[0]
        # Bare loopback is not a signal: it is every other service on the box
        # too, and matching it would redirect unrelated commands into a skill.
        if host and not host.split(":", 1)[0] in ("127.0.0.1", "localhost", ""):
            out.append((host, skill))
    return tuple(out)

_PREFLIGHT_TIMEOUT = 5.0
# What bash actually says when it cannot parse. Anything else from the probe is
# treated as the probe's own problem, not the command's.
_SHELL_SYNTAX_ERROR_RE = re.compile(
    r"syntax error|unexpected EOF|unexpected token|unterminated|"
    r"missing closing|no closing",
    re.IGNORECASE,
)


class HouseholdShellGuards:
    """Mixin for ExecTool: the household's preflight refusals."""

    async def _preflight_error(self, command: str) -> str | None:
        """Reject a command that cannot parse, before running it.

        The model hand-writes these when it decides to reimplement a skill, and
        gets them wrong in two ways worth catching. An unbalanced quote reaches
        the family as bash's ``unexpected EOF while looking for matching `'``,
        which says nothing about which quote; a typo inside a ``python -c``
        payload costs a subprocess and comes back as a traceback. Both are
        cheaper to catch here, and the message can say what to do instead.

        The checker never blocks on its own failure: if bash is missing or the
        probe times out, the command runs as before.
        """
        noop_error = self._noop_command_error(command)
        if noop_error:
            return noop_error
        invocation_error = self._skill_invocation_error(command)
        if invocation_error:
            return invocation_error
        reimplementation_error = self._skill_reimplementation_error(command)
        if reimplementation_error:
            return reimplementation_error
        if not _IS_WINDOWS:
            shell_error = await self._shell_syntax_error(command)
            if shell_error:
                return shell_error
        return self._python_syntax_error(command)

    # Commands whose whole effect is to succeed.
    _NOOP_COMMANDS = frozenset(("true", ":", "/bin/true", "exit", "exit 0"))

    @classmethod
    def _noop_command_error(cls, command: str) -> str | None:
        """Refuse a command that does nothing, and say what to do instead.

        ``true`` is what a model runs when it wants to have acted without
        acting — usually one turn after being told to emit a skill's invocation
        block, because a block is text and text does not feel like a call. It
        used to come back as an empty result and ``Exit code: 0``, which reads
        as success and teaches nothing, so the model does it again: 132 of them
        in five minutes on the evening two prize redemptions took Alfred six
        minutes and a sub-agent to mark as delivered.

        Costing the same round trip as the empty success it replaces, this at
        least spends it on the one sentence that ends the loop. A block written
        *beside* the no-op is no longer lost either — the runner drops the
        placeholder and runs the block (see _resolve_block_beside_calls).
        """
        bare = command.strip().rstrip(";").strip()
        if bare.lower() not in cls._NOOP_COMMANDS:
            return None
        logger.warning("exec: refused a no-op command ({})", bare)
        return (
            f"Error: `{bare}` runs nothing, so this turn did no work. A skill "
            f"does not need a call to go with it: write its invocation block as "
            f"the text of your reply and the runtime executes it. If you already "
            f"have what you need, answer the person instead of calling a tool."
        )

    @classmethod
    def _skill_reimplementation_error(cls, command: str) -> str | None:
        """Refuse a command that does by hand what a skill already does.

        SOUL.md and AGENTS.md both say not to hand-write curl or Python for a
        skill's job, and the model does it anyway. It is not a style
        preference:

        - The skill carries the credential in the environment. Inline code puts
          it on a command line, which is how a Paperless token reached an
          unauthenticated debug log and every token in the house was rotated.
        - Improvised code gets the details wrong in ways nobody sees. Asked for
          a policy PDF, it saved outside the workspace — where the chat cannot
          serve it — and announced the file had been sent.
        - The skill's return shape is what makes files arrive. Hand-written
          code prints a bare path, and a bare path is not a delivery.

        Skills themselves run through exec, so their generated code has to be
        let past — by the ledger the translator vouches into, not by the shape
        of the wrapper. The shape is copyable, and was copied: the wrapper is
        visible in the conversation as a tool call, and a hand-written payload
        wearing it used to skip this check entirely. Signals are therefore
        looked for in the decoded payload as well as the command line, because
        base64 is exactly where they were hiding.

        A model that wants to reach one of these services has exactly one
        supported route, and this points at it rather than only saying no.
        """
        if is_registered_skill_translation(command):
            return None
        haystacks = [command, *cls._python_payloads(command)]
        # Host signals first, for the same reason the tasks entries come
        # last: the specific match has to win over the one built on a
        # shared base URL. Read per call, so a container that is handed
        # its addresses after import still gets them.
        for signal, skill in (*_host_signals(), *_SKILL_OWNED_SIGNALS):
            if any(signal in haystack for haystack in haystacks):
                logger.warning(
                    "exec: refused hand-written access to {} (owned by skill {})",
                    signal, skill,
                )
                return (
                    f"Error: this reaches {signal}, which the `{skill}` skill "
                    f"already handles. Do not write the request yourself — the "
                    f"skill holds the credential safely, returns the fields that "
                    f"make files actually reach the chat, and one line of it "
                    f"replaces this. Emit its invocation block as plain text "
                    f"instead. If `{skill}` is genuinely broken, say so plainly "
                    f"in your reply rather than working around it."
                )
        return None

    @classmethod
    def _skill_invocation_error(cls, command: str) -> str | None:
        """Reject a command whose whole job is to emit a skill-invocation block.

        Seen live as ``echo '{"skill":"file-share","action":"list_files"}'`` and
        ``printf '{"skill":"camera-feed",...}'``. The model reaches for the
        shell to "say" the block, which sends the JSON to stdout instead of to
        the runtime — the skill never runs, and the text can surface to the
        family as a tool result.

        The plain case — one echoed block naming a skill that exists — no longer
        reaches here: the runner reroutes it to that skill (see
        _maybe_translate_skill_calls), because refusing it cost a round trip on
        nearly every skill call. What is left for this to catch is what cannot
        be rerouted: an unknown or misspelled skill, a block among other
        arguments, a redirect. Those still have to be refused rather than run,
        so the net stays wider than the reroute on purpose.

        Matched on the echoed *argument*, not anywhere in the command, so a
        script that legitimately handles JSON containing a "skill" key still
        runs. ``shlex`` is what decides where the argument ends, the same
        splitting the shell will do.
        """
        if "skill" not in command:            # cheap reject for the common case
            return None
        try:
            argv = shlex.split(command)
        except ValueError:
            return None                        # unbalanced quotes; bash -n reports it
        if not argv or os.path.basename(argv[0]) not in ECHO_LIKE:
            return None
        for token in argv[1:]:
            if is_skill_invocation_json(token):
                logger.warning("exec: refused a skill block echoed via {}", argv[0])
                return emit_as_text_hint("running it through a shell")
        return None

    @staticmethod
    async def _shell_syntax_error(command: str) -> str | None:
        """``bash -n`` parses without executing — the same parser that will run it."""
        bash = shutil.which("bash") or "/bin/bash"
        try:
            process = await asyncio.create_subprocess_exec(
                bash, "-n", "-c", command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=_PREFLIGHT_TIMEOUT
            )
        except Exception as exc:
            logger.debug("Shell preflight unavailable ({}); running unchecked", exc)
            return None
        if process.returncode == 0:
            return None
        detail = stderr.decode("utf-8", errors="replace").strip()
        # Reject only on a diagnosis we recognise. A non-zero exit with nothing
        # to show for it means the probe itself went wrong — a shell that is not
        # the one we assumed, a sandbox quirk — and blocking a command that
        # would have run fine is far worse than missing a malformed one.
        if not detail or not _SHELL_SYNTAX_ERROR_RE.search(detail):
            logger.debug(
                "Shell preflight inconclusive (rc={}, stderr={!r}); running unchecked",
                process.returncode, detail[:160],
            )
            return None
        logger.info("Rejected unparseable command: {}", detail.replace("\n", " ")[:160])
        return (
            "Error: this is not valid shell, so it was not run.\n"
            f"{detail}\n\n"
            "[Usually an unbalanced ' or \". If you are calling a skill, emit its "
            "JSON invocation block instead of writing the command yourself — the "
            "runtime builds the command for you and gets the quoting right.]"
        )

    @classmethod
    def _python_syntax_error(cls, command: str) -> str | None:
        for source in cls._python_payloads(command):
            try:
                compile(source, "<exec>", "exec")
            except SyntaxError as exc:
                logger.info("Rejected invalid Python payload: line {}: {}", exc.lineno, exc.msg)
                offending = (exc.text or "").strip()
                return (
                    "Error: the Python in this command has a syntax error, so it "
                    "was not run.\n"
                    f"line {exc.lineno}: {exc.msg}\n"
                    + (f"  {offending}\n" if offending else "")
                    + "\n[Fix the snippet, or emit the skill's JSON invocation block "
                    "instead of writing the Python yourself.]"
                )
            except (ValueError, MemoryError, RecursionError):
                # Null bytes or something pathological — let the interpreter decide.
                return None
        return None

    @staticmethod
    def _python_payloads(command: str) -> list[str]:
        """Python source embedded in *command*, base64-wrapped or inline."""
        payloads: list[str] = []
        for match in _B64_PAYLOAD_RE.finditer(command):
            try:
                payloads.append(base64.b64decode(match.group(1)).decode("utf-8"))
            except Exception:
                continue
        try:
            argv = shlex.split(command)
        except ValueError:
            return payloads  # unbalanced quotes; bash -n already reported it
        for i, token in enumerate(argv[:-2]):
            if os.path.basename(token) in _PY_EXE and argv[i + 1] == "-c":
                payloads.append(argv[i + 2])
        return payloads
