"""Long background tasks on pi (pi.dev), with a thin harness around it.

nanobot's own loop gives a background task Alfred's whole setup: a ~30k-token
system prompt, every tool, and whatever the task pulls in on top. That is what
a hosted model is built for. A model that fits the house's 12 GB cards drowns
in it -- on the benchmark's long tasks the local models scored 5-7 of 10, and
the failures were not about reasoning: they refused to make files, searched and
never read, overflowed the context, or looped.

pi is a minimal agent: a replaceable system prompt, a handful of tools, and
extensions. With the household's abilities offered as four real tools
(pi/alfred.ts) and a 250-word prompt (pi/SYSTEM.md), a turn starts at about
4.6k tokens. Measured on the prototype: gemma4 (8B) searched, read the source,
made the PDF and answered with the link -- the task Ternary Bonsai 27B and
gemma4:e2b both failed inside nanobot.

What this module adds around pi is the harness, and it is small on purpose:

* **The endpoint.** The model the household picked for the sub-agent, reached
  the way nanobot reaches it. OpenCode requests carry `x-opencode-session`,
  one per task. OpenCode Go -- the flat plan CLAUDE.md keeps for a person at
  the keyboard -- only when assistant.harness.allow_go says so.
* **What pi can reach.** pi runs with a minimal environment -- no provider
  keys, no tokens, nothing a page it reads could talk it into sending
  somewhere -- and with no `bash`: nanobot's own exec has an env allowlist, a
  sandbox and deny patterns, and pi's has none of them. The household's skills
  still need their credentials, so those go to the skill subprocess alone,
  through a 0600 file outside the task's directory; the extension confines
  read/write/edit to that directory, so the file is out of reach of the model.
* **The deliverable check.** The prototype's gemma4 said "the guide is
  created" with a placeholder where the link should be, and on other runs
  ended a turn with its plan in the thinking and no call. A task that asks for
  a file is not finished until `make_document` returned a `download:` link;
  until then pi's session is continued with a short nudge.
* **Cleanup.** gemma4's chat template leaks `<channel|>` through Ollama's
  OpenAI layer; it never reaches a person.

Who may run what is SubagentManager's call, not this module's: a task set off
by another member or from WhatsApp never reaches the harness.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from loguru import logger

ASSETS = Path(__file__).parent / "pi"
PI_HOME = Path(os.environ.get("PI_HOME", "/opt/pi"))
PI_BIN = PI_HOME / "node_modules" / ".bin" / "pi"
EXTENSION = PI_HOME / "ext" / "alfred.ts"

# What pi's own process keeps of the environment. Everything else -- every
# provider key, the proxy and broker tokens, the share's password -- stays out.
_PI_ENV_KEEP = ("HOME", "PATH", "LANG", "LC_ALL", "TZ", "TERM", "TMPDIR", "NODE_OPTIONS")
# pi's tools. No bash, ever: see the module docstring.
TOOLS = "read,write,edit,web,make_document,skill,skill_guide"

# `download:<share path>` is how the stack links a file it filed; the portal
# turns it into a real URL. Only this form, and only from make_document: an
# http link to a .html page is also what every search result looks like.
LINK = re.compile(r"download:[^\s\)\]\"'<>\\]+")
# Formats, not topics: "research X and report back" is not a request for a file,
# and nudging it towards make_document would file one nobody asked for.
WANTS_FILE = re.compile(r"(?i)\b(pdf|excel|planilla|spreadsheet|xlsx|docx|pptx|"
                        r"presentaci[oó]n|presentation|diapositivas|slides|"
                        r"p[aá]gina web|web page|html)\b")
_TEMPLATE_TOKENS = re.compile(r"<\|?/?channel\|?>|<\|[a-z_]+\|>|<channel\|>")

NUDGE_FILE = ("Continue. You have not delivered yet: the task asks for a file. Call "
              "`make_document` now with what you already found, then answer with the link.")
NUDGE_TEXT = "Continue: finish the task and write your final answer now."


def _is_opencode(base_url: str) -> bool:
    host = (urlparse(base_url or "").hostname or "").lower()
    return host == "opencode.ai" or host.endswith(".opencode.ai")


def _is_go(base_url: str) -> bool:
    """OpenCode's flat Go plan: `/zen/go/v1`, as opposed to Zen's `/zen/v1`."""
    return _is_opencode(base_url) and "/zen/go" in (urlparse(base_url).path or "")


@dataclass
class Endpoint:
    """An OpenAI-compatible chat endpoint and the model on it."""
    base_url: str               # ends in /v1
    model: str
    api_key: str = "local"
    context_window: int = 40960
    # Whether the server takes `reasoning_effort: "none"` to switch thinking
    # off. Ollama's OpenAI layer does; other servers may reject the value.
    reasoning: bool = True
    # Sent on every request. OpenCode's `x-opencode-session` is added by run()
    # itself, one per task: a session id is never hard-coded (CLAUDE.md).
    headers: dict[str, str] = field(default_factory=dict)
    # OpenCode Go is the flat plan CLAUDE.md reserves for a person at the
    # keyboard; a background task is not that. Refused unless the household
    # switched this on (assistant.harness.allow_go) knowing the account risk.
    allow_go: bool = False

    def check(self) -> str | None:
        """Why this endpoint may not be used, or None."""
        if not self.base_url or not self.model:
            return "no base URL or model"
        if _is_go(self.base_url) and not self.allow_go:
            return ("an OpenCode Go endpoint: Go is for interactive use, and a background task "
                    "on it needs assistant.harness.allow_go")
        return None


@dataclass
class ToolCall:
    name: str
    args: dict
    is_error: bool = False
    result: str = ""


@dataclass
class HarnessResult:
    text: str
    links: list[str]
    tools: list[ToolCall] = field(default_factory=list)
    rounds: int = 0            # pi runs: the task, then one per nudge
    turns: int = 0             # assistant messages, i.e. model calls
    seconds: float = 0.0
    delivered: bool = False
    error: str = ""


def _result_text(result: Any) -> str:
    """The text a tool returned -- matched as text, not as its JSON encoding,
    whose escaping put a backslash on the end of every link."""
    if isinstance(result, dict):
        parts = result.get("content") or []
        texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
        if texts:
            return "\n".join(texts)
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


def available() -> bool:
    return PI_BIN.exists() and EXTENSION.exists()


def clean(text: str) -> str:
    return _TEMPLATE_TOKENS.sub("", text or "").strip()


def wants_file(task: str) -> bool:
    return bool(WANTS_FILE.search(task or ""))


def _delivered_links(call: ToolCall) -> list[str]:
    """The files a tool call filed. Only make_document, or the document skill."""
    if call.is_error:
        return []
    if call.name == "make_document" or (call.name == "skill" and call.args.get("skill") == "document"):
        return LINK.findall(call.result)
    return []


def _agent_dir(workdir: Path, endpoint: Endpoint) -> Path:
    agent = workdir / ".pi-agent"
    agent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ASSETS / "SYSTEM.md", agent / "SYSTEM.md")
    model: dict[str, Any] = {
        "id": endpoint.model, "contextWindow": endpoint.context_window, "maxTokens": 8192,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }
    compat = {"supportsDeveloperRole": False, "supportsReasoningEffort": False}
    if endpoint.reasoning:
        # Thinking off by default: gemma4 planned the tool call inside its
        # thinking and ended the turn without making it, four rounds running.
        model["reasoning"] = True
        model["thinkingLevelMap"] = {"off": "none", "minimal": "low", "low": "low",
                                     "medium": "medium", "high": "high"}
        compat["supportsReasoningEffort"] = True
    provider: dict[str, Any] = {
        "baseUrl": endpoint.base_url.rstrip("/"), "api": "openai-completions",
        "apiKey": "ALFRED_HARNESS_API_KEY", "compat": compat, "models": [model],
    }
    names = header_env_names(endpoint)
    if names:
        # By environment-variable name: pi resolves a header value from the
        # variable of that name, so no value is written into this file.
        provider["headers"] = names
    (agent / "models.json").write_text(json.dumps({"providers": {"house": provider}}, indent=2))
    return agent


def header_env_names(endpoint: Endpoint) -> dict[str, str]:
    """{header: the env var pi reads its value from}, for every header sent."""
    headers = list(endpoint.headers)
    if _is_opencode(endpoint.base_url) and "x-opencode-session" not in headers:
        headers.append("x-opencode-session")
    return {h: f"ALFRED_HARNESS_HEADER_{i}" for i, h in enumerate(headers)}


def endpoint_for_provider(provider: Any, model: str | None, *, context_window: int = 40960,
                          allow_go: bool = False) -> Endpoint | None:
    """The endpoint nanobot itself would call for *model* on *provider*, or None.

    What the harness runs is the model the household picked for the sub-agent,
    reached the way nanobot reaches it: the provider's own base URL, key and
    model-name rule. None for a provider pi cannot speak to -- one that is not
    OpenAI-compatible, or a model OpenCode serves only on /v1/responses.
    """
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider
    if not isinstance(provider, OpenAICompatProvider):
        return None
    base = getattr(provider, "_effective_base", None) or provider.api_base or ""
    name = model or provider.default_model
    spec = getattr(provider, "_spec", None)
    if spec is not None and getattr(spec, "strip_model_prefix", False):
        name = name.split("/")[-1]
    try:
        if provider._should_use_responses_api(name, None):
            return None
    except Exception:                                      # noqa: BLE001
        pass
    local = bool(spec and str(getattr(spec, "name", "")).startswith("ollama"))
    return Endpoint(base_url=base, model=name, api_key=provider.api_key or "local",
                    context_window=context_window if local else max(context_window, 131072),
                    reasoning=local, headers=dict(getattr(provider, "extra_headers", {}) or {}),
                    allow_go=allow_go)


def _skill_env_file() -> str:
    """The skills' environment, for the skill subprocess and nothing else.

    0600, outside the task's directory (the extension keeps read/write/edit
    inside it), and deleted when the run ends. The whole environment, provider
    keys included: a skill is household code and needs what it needs.
    """
    fd, path = tempfile.mkstemp(prefix="alfred-skill-env-", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(dict(os.environ), fh)
    os.chmod(path, 0o600)
    return path


def _env(agent: Path, endpoint: Endpoint, bench: bool, skill_env: str) -> dict[str, str]:
    env = {k: os.environ[k] for k in _PI_ENV_KEEP if k in os.environ}
    env.update(
        PI_CODING_AGENT_DIR=str(agent),
        # No install ping, no update check: nothing about this house leaves it.
        PI_TELEMETRY="0", PI_OFFLINE="1",
        ALFRED_HARNESS_API_KEY=endpoint.api_key or "local",
        ALFRED_HARNESS_BENCH="1" if bench else "0",
        ALFRED_SKILL_ENV_FILE=skill_env,
    )
    session = uuid.uuid4().hex
    for header, name in header_env_names(endpoint).items():
        # One id per task: a background task is one conversation.
        env[name] = session if header == "x-opencode-session" else endpoint.headers.get(header, "")
    return env


def _die_with_parent() -> None:
    """Runs in the child before pi starts: SIGKILL it when its parent dies.

    The `finally` in _pi kills pi on a timeout or a cancel, but not when the
    parent itself is killed -- the benchmark's Stop is a `pkill` of the bench
    script, which dies on the signal before any cleanup runs. pi, whose own
    command line is just `pi`, then went on calling the model with nobody
    reading: measured 2026-09-23, an orphan held the bench Ollama for eight
    minutes and the next run's first reply waited all of them.
    """
    try:
        import ctypes
        PR_SET_PDEATHSIG = 1
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
    except Exception:                                      # noqa: BLE001
        pass           # not Linux, or no libc by that name: the finally still covers the rest


async def _pi(message: str, *, agent: Path, workdir: Path, env: dict[str, str], model: str,
              first: bool, thinking: str, timeout: float,
              on_tool: Callable[[ToolCall], Awaitable[None]] | None,
              on_note: Callable[[str], Awaitable[None]] | None = None,
              on_start: Callable[[ToolCall], Awaitable[None]] | None = None,
              ) -> tuple[str, list[ToolCall], int, str]:
    """One pi run. (final text, tool calls, assistant turns, error)."""
    args = [str(PI_BIN), "-p", "--provider", "house", "--model", model,
            "--thinking", thinking, "--session-dir", str(workdir / ".pi-sessions"),
            "--no-context-files", "--no-skills", "--no-extensions", "-e", str(EXTENSION),
            "--no-prompt-templates", "--tools", TOOLS, "--mode", "json"]
    if not first:
        args.append("--continue")
    args.append(message)
    stderr = (workdir / "stderr.log").open("ab")
    # Line-buffered: it is what someone reads to see what a task is doing
    # *while* it runs, and a block buffer left it empty for minutes.
    log = (workdir / "events.jsonl").open("a", encoding="utf-8", buffering=1)
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(workdir), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=stderr, limit=32 * 1024 * 1024,
        preexec_fn=_die_with_parent)
    tools: list[ToolCall] = []
    pending: dict[str, ToolCall] = {}
    final, turns, failure = "", 0, ""

    async def read() -> None:
        nonlocal final, turns, failure
        assert proc.stdout
        async for raw in proc.stdout:
            # Every `message_update` carries the whole partial message: kept,
            # they grow the log quadratically -- a streamed 20 KB tool call is
            # hundreds of MB -- and are only ever the lead-up to message_end.
            if raw.startswith(b'{"type":"message_update"'):
                continue
            line = raw.decode("utf-8", "replace")
            log.write(line)
            try:
                e = json.loads(line)
            except ValueError:
                continue
            kind = e.get("type")
            if kind == "tool_execution_start":
                call = ToolCall(name=str(e.get("toolName")), args=e.get("args") or {})
                pending[str(e.get("toolCallId"))] = call
                tools.append(call)
                # Announced now, not when it returns: a page fetch or a
                # document render can take a minute, and a panel that shows
                # nothing until then looks like a task that stalled.
                if on_start:
                    try:
                        await on_start(call)
                    except Exception:                      # noqa: BLE001
                        pass
            elif kind == "tool_execution_end":
                call = pending.pop(str(e.get("toolCallId")), None)
                if call is not None:
                    call.is_error = bool(e.get("isError"))
                    call.result = _result_text(e.get("result"))[:20000]
                    if on_tool:
                        try:
                            await on_tool(call)
                        except Exception:                  # noqa: BLE001
                            pass
            elif kind == "message_end" and (e.get("message") or {}).get("role") == "assistant":
                msg = e["message"]
                turns += 1
                parts = msg.get("content") or []
                said = " ".join(c.get("text", "") for c in parts if c.get("type") == "text")
                if said.strip():
                    final = said
                    # What it says before a tool call is what the "background
                    # tasks" panel draws as the task's narration -- the same
                    # `thought` nanobot's own loop publishes.
                    if on_note and any(c.get("type") == "toolCall" for c in parts):
                        try:
                            await on_note(clean(said))
                        except Exception:                  # noqa: BLE001
                            pass
                if msg.get("stopReason") in ("error", "aborted") and msg.get("errorMessage"):
                    failure = str(msg["errorMessage"])[:300]

    try:
        await asyncio.wait_for(asyncio.gather(read(), proc.wait()), timeout=timeout)
    finally:
        # A timeout, a cancelled task (the person pressed stop, the assistant
        # is shutting down), an oversized line: in every case pi is not left
        # running on its own, where it could still file documents.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        log.close()
        stderr.close()
    if proc.returncode not in (0, None) and not failure:
        tail = (workdir / "stderr.log").read_text(errors="replace").strip().splitlines()[-3:]
        failure = f"pi exited {proc.returncode}" + (f": {' / '.join(tail)[:300]}" if tail else "")
    return clean(final), tools, turns, failure


async def run(task: str, endpoint: Endpoint, workdir: Path, *, context: str | None = None,
              bench: bool = False, max_nudges: int = 3, timeout: float = 900.0,
              thinking: str = "off", want_file: bool | None = None,
              on_tool: Callable[[ToolCall], Awaitable[None]] | None = None,
              on_note: Callable[[str], Awaitable[None]] | None = None,
              on_start: Callable[[ToolCall], Awaitable[None]] | None = None,
              on_nudge: Callable[[int, int, str], Awaitable[None]] | None = None) -> HarnessResult:
    """Run *task* on pi, nudging until it delivers or the nudges run out."""
    started = time.monotonic()
    why = endpoint.check()
    if why:
        return HarnessResult(text="", links=[], error=f"harness refused: {why}")
    if not available():
        return HarnessResult(text="", links=[], error=f"pi is not installed at {PI_HOME}")
    workdir.mkdir(parents=True, exist_ok=True)
    agent = _agent_dir(workdir, endpoint)
    skill_env = _skill_env_file()
    env = _env(agent, endpoint, bench, skill_env)
    need_file = wants_file(task) if want_file is None else want_file
    message = task if not context else f"Background context:\n{context.strip()}\n\nTask:\n{task}"
    result = HarnessResult(text="", links=[])
    try:
        for attempt in range(max_nudges + 1):
            left = timeout - (time.monotonic() - started)
            if left <= 5:
                result.error = f"stopped after {timeout:.0f}s"
                break
            try:
                text, tools, turns, failure = await _pi(
                    message, agent=agent, workdir=workdir, env=env, model=endpoint.model,
                    first=attempt == 0 or not any((workdir / ".pi-sessions").glob("*")),
                    thinking=thinking, timeout=left, on_tool=on_tool, on_note=on_note,
                    on_start=on_start)
            except asyncio.TimeoutError:
                result.error = f"stopped after {timeout:.0f}s"
                break
            result.rounds = attempt + 1
            result.turns += turns
            result.tools += tools
            for link in (l for t in tools for l in _delivered_links(t)):
                if link not in result.links:
                    result.links.append(link)
            if text:
                result.text = text
            if failure:
                # The model or the server failed (Ollama down, a crash): a
                # nudge would fail the same way, and the reason is the answer.
                result.error = failure
                break
            # Delivered: a file that came back from make_document, not one the
            # model named.
            result.delivered = bool(result.text) and (not need_file or bool(result.links))
            if result.delivered:
                break
            if attempt == 0 and not any((workdir / ".pi-sessions").glob("*")):
                # Nothing to continue: a nudge would open a new session holding
                # only "continue", with no task in it.
                result.error = "pi wrote no session"
                break
            short = "no file" if need_file and not result.links else "no answer"
            message = NUDGE_FILE if short == "no file" else NUDGE_TEXT
            logger.info("harness: round {} ended short ({}); nudging", attempt, short)
            if on_nudge:
                # Otherwise the nudge is invisible, and the pause while the
                # model starts over reads as a stall.
                try:
                    await on_nudge(attempt + 1, max_nudges, short)
                except Exception:                          # noqa: BLE001
                    pass
    finally:
        try:
            os.unlink(skill_env)
        except OSError:
            pass
    missing = [l for l in result.links if l not in result.text]
    if missing:
        # The model answered without the link it got, or with a made-up one:
        # the real one is appended, never invented.
        result.text = (result.text + "\n\n" if result.text else "") + "\n".join(missing)
    result.seconds = round(time.monotonic() - started, 1)
    return result


def new_workdir(root: Path) -> Path:
    return root / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def prune_workdirs(root: Path, keep: int = 10) -> None:
    """Keep the last *keep* task directories. They are for debugging a recent
    run, and nothing else ever cleans the workspace up."""
    if not root.is_dir():
        return
    dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name)
    for old in dirs[:-keep] if keep else dirs:
        shutil.rmtree(old, ignore_errors=True)
