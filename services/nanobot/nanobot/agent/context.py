"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
import re
from importlib.resources import files as pkg_files
from pathlib import Path
from typing import Any

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader
from nanobot.utils.helpers import build_assistant_message, current_time_str, detect_image_mime, truncate_text
from nanobot.utils.prompt_templates import render_template


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    # Ordered most-shared first, and that order is load-bearing rather than
    # cosmetic. A serving engine with a prefix cache (FreeToken's radix cache,
    # here) can only reuse a prompt up to the first byte that differs, so
    # anything identical across the house must come before anything that is
    # not. Measured 2026-09-10: AGENTS.md, SOUL.md, FAMILY.md and TOOLS.md are
    # byte-identical across user1/user2/user3; USER.md is the only one that
    # differs. It used to sit third, which stranded FAMILY.md and TOOLS.md --
    # ~955 tokens of text that is the same for everyone -- behind the split,
    # where no assistant could share them with any other.
    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "FAMILY.md", "TOOLS.md", "USER.md"]
    # The ones that are not the same for everybody. Everything before the first
    # of these is what `shared_prompt_prefix` can hand to a prewarm.
    PER_USER_BOOTSTRAP_FILES = frozenset({"USER.md"})
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
    _MAX_RECENT_HISTORY = 50
    _MAX_HISTORY_CHARS = 32_000  # hard cap on recent history section size
    _RUNTIME_CONTEXT_END = "[/Runtime Context]"

    def __init__(self, workspace: Path, timezone: str | None = None, disabled_skills: list[str] | None = None):
        self.workspace = workspace
        self.timezone = timezone
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace, disabled_skills=set(disabled_skills) if disabled_skills else None)

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        channel: str | None = None,
        session_key: str | None = None,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills.

        *session_key* scopes the Recent History section to entries consolidated
        from this same session. Without it, summaries of every session — other
        chats, the MQTT camera feed, heartbeats — were injected into every
        prompt, which read as the agent mixing conversations. ``None`` keeps
        the old include-everything behavior for callers with no session.
        """
        parts = [self._get_identity(channel=channel)]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        memory = self.memory.get_memory_context()
        if memory and not self._is_template_content(self.memory.read_memory(), "memory/MEMORY.md"):
            parts.append(f"# Memory\n\n{memory}")

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")
                # Always-skills are fully loaded above, but the skill-invocation
                # interceptor still needs their name→SKILL.md path mapping (it
                # parses the on-demand summary format below). Emit those path
                # lines so an always-skill's {"skill": ...} block still routes.
                always_paths = self.skills.build_skills_summary(only=set(always_skills))
                if always_paths:
                    parts.append(
                        "# Active Skill Paths\n\n"
                        "(Reference only — these skills are already loaded above; "
                        "do not re-read them.)\n\n" + always_paths
                    )

        skills_summary = self.skills.build_skills_summary(exclude=set(always_skills))
        if skills_summary:
            parts.append(render_template("agent/skills_section.md", skills_summary=skills_summary))

        # Recent History is deliberately NOT here any more -- see
        # `recent_history_block`, which the caller puts in the final message.

        return "\n\n---\n\n".join(parts)

    def build_step_system_prompt(self, relevant: str, channel: str | None = None) -> str:
        """The system prompt for one step of a plan (AgentLoop._step_runner).

        The full prompt is ~22k tokens -- persona, household rules, memory, nine
        skills loaded whole -- and a step needs almost none of it: the planner
        holds the conversation and writes the answer. On a local model that
        prompt is the step's cost: Qwen3.8-27B on one 3060 read it at ~500
        tokens/s, two minutes before the first word of every step (2026-09-24),
        and memory is where a step found "facts" nobody had checked. So a step
        gets the identity, the tool notes, every skill's line in the catalogue,
        and in full only the always-loaded skills that *relevant* (the request
        and the plan) names.
        """
        parts = [self._get_identity(channel=channel)]
        tools_md = self.workspace / "TOOLS.md"
        if tools_md.is_file():
            parts.append(f"## TOOLS.md\n\n{tools_md.read_text(encoding='utf-8')}")
        words = set(re.findall(r"[a-z0-9]+", (relevant or "").lower()))
        named = [s for s in self.skills.get_always_skills()
                 if s.lower() in words or set(s.lower().split("-")) <= words]
        # And any skill a step calls by name -- "Call skill lights", "with the
        # chores skill". Offered only its catalogue line, Bonsai 2 read the
        # lights SKILL.md seven times in one step and never called it
        # (2026-09-25); the planner already chose it, so the step gets it whole.
        low = (relevant or "").lower()
        for entry in self.skills.list_skills():
            name = entry["name"]
            if name not in named and re.search(
                    rf"\bskill:? {re.escape(name.lower())}\b|\b{re.escape(name.lower())} skill\b", low):
                named.append(name)
        if named:
            content = self.skills.load_skills_for_context(named)
            if content:
                parts.append(f"# Active Skills\n\n{content}")
                # The path lines too, as build_system_prompt does: the
                # invocation interceptor maps {"skill": ...} to SKILL.md through
                # them, and without one a named skill's block resolved to
                # nothing -- every chores step "bad_invocation" (2026-09-25).
                paths = self.skills.build_skills_summary(only=set(named))
                if paths:
                    parts.append("# Active Skill Paths\n\n(Reference only -- these skills are "
                                 "already loaded above; do not re-read them.)\n\n" + paths)
        summary = self.skills.build_skills_summary(exclude=set(named))
        if summary:
            parts.append(render_template("agent/skills_section.md", skills_summary=summary))
        return "\n\n---\n\n".join(parts)

    def _get_identity(self, channel: str | None = None) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return render_template(
            "agent/identity.md",
            workspace_path=workspace_path,
            runtime=runtime,
            platform_policy=render_template("agent/platform_policy.md", system=system),
            channel=channel or "",
        )

    @staticmethod
    def _build_runtime_context(
        channel: str | None, chat_id: str | None, timezone: str | None = None,
        session_summary: str | None = None,
    ) -> str:
        """Build untrusted runtime metadata block for injection before the user message."""
        lines = [f"Current Time: {current_time_str(timezone)}"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        if session_summary:
            lines += ["", "[Resumed Session]", session_summary]
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines) + "\n" + ContextBuilder._RUNTIME_CONTEXT_END

    @staticmethod
    def annotate_runtime_line(messages: list[dict[str, Any]], line: str) -> None:
        """Add *line* at the end of the last Runtime Context block.

        For what the runtime knows about this one turn -- that it is work of
        several steps and should be planned (AgentLoop, tools/plan.py). Inside
        the block, so it is metadata the model reads and not words the person
        said, and it is not kept in history the way their message is.
        """
        end_tag = ContextBuilder._RUNTIME_CONTEXT_END
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str) and end_tag in content:
                cut = content.rfind(end_tag)
                msg["content"] = f"{content[:cut]}{line}\n{content[cut:]}"
                return
            if isinstance(content, list):
                for block in content:
                    text = block.get("text") if isinstance(block, dict) else None
                    if isinstance(text, str) and end_tag in text:
                        cut = text.rfind(end_tag)
                        block["text"] = f"{text[:cut]}{line}\n{text[cut:]}"
                        return
            return

    @staticmethod
    def annotate_runtime_model(
        messages: list[dict[str, Any]], model: str, displaced: str | None = None
    ) -> None:
        """Name the model serving this turn inside the Runtime Context block.

        The block is built before the model is known — vision routing reads the
        images out of the built messages, so the order cannot be swapped — and
        the model is chosen a few lines before the call. This writes the answer
        back into the block that is already there rather than adding a second
        one, so `Current Time` and `Model` are read from the same place.

        Without it the agent had no way to know: asked which model it was, it
        answered from the roster in AGENTS.md, which is the configuration and
        not the routing. It says `gpt-5.6-luna` while a photo is being answered
        by `qwen3-vl:8b`, while a Profesion turn is on `kimi-k3`, and — the one
        that matters — for the whole hour after an outage moves the house onto
        the fallback.

        In place, on the last message carrying a block — the one this turn is
        being sent with. History keeps no block at all (`_save_turn` strips it),
        so the only other candidates are blocks this same turn built.

        The line goes directly under `Current Time`, inside the *first* block
        in the message, and both halves of that matter. First, because the
        block is followed by the family's own text and that text is not ours:
        a message quoting a runtime block — a pasted log, a forwarded
        transcript, a question about the prompt format — carries a second
        closing marker, and anchoring on the last one would write the model
        into somebody's prose and leave the real block without the line the
        prompt calls its only source. `_save_turn` cuts the block at the first
        marker for the same reason; these two now agree. (What first-match does
        not survive is a quoted block that reached *history* — `_save_turn`
        strips only the leading one — and was then merged ahead of this turn's.
        Two rare things at once, and it costs a missing line rather than a
        forged one: the label lands in the quoted text and the agent is back to
        reading the roster, which is where it started.) Under `Current Time`
        rather than at the end of the block because `[Resumed Session]` and its
        summary are appended last, and a `Model:` line after that prose reads
        as a fact about the session that ended, not this one.

        A message list with no block is left exactly as it is. That is not the
        same as saying no caller has one: a subagent's block lives in its
        *system* prompt (SubagentManager._build_subagent_prompt), so pointing
        this at a subagent run would write the model into instructions, on the
        wrong side of the line the tag is drawn to mark. Callers with a block
        of that shape are out of scope for this, deliberately.
        """
        line = f"Model: {model}"
        if displaced:
            line += f" (outage fallback — {displaced} is not answering)"
        tag = ContextBuilder._RUNTIME_CONTEXT_TAG

        def _annotated(text: str) -> str | None:
            start = text.find(tag)
            if start < 0:
                return None
            end = text.find(ContextBuilder._RUNTIME_CONTEXT_END, start)
            if end < 0:
                return None
            # After the tag's own line, then after the line below it — which is
            # `Current Time`, the block's first entry and the one this joins.
            cut = text.find("\n", start + len(tag))
            if cut < 0:
                return None
            nxt = text.find("\n", cut + 1)
            if 0 <= nxt < end:
                cut = nxt
            return f"{text[:cut]}\n{line}{text[cut:]}"

        for i in range(len(messages) - 1, -1, -1):
            content = messages[i].get("content")
            if isinstance(content, str):
                updated = _annotated(content)
                if updated is not None:
                    messages[i] = {**messages[i], "content": updated}
                    return
            elif isinstance(content, list):
                for j, block in enumerate(content):
                    if not isinstance(block, dict) or block.get("type") != "text":
                        continue
                    updated = _annotated(block.get("text") or "")
                    if updated is not None:
                        blocks = list(content)
                        blocks[j] = {**block, "text": updated}
                        messages[i] = {**messages[i], "content": blocks}
                        return

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [item if isinstance(item, dict) else {"type": "text", "text": str(item)} for item in value]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    def recent_history_block(self, session_key: str | None = None) -> str:
        """This session's un-consolidated history summaries, or "".

        Lives in the *last* message rather than the system prompt, and that
        placement is the whole point. It gains an entry on most turns, so while
        it sat at the end of the system prompt it changed the very first message
        every turn -- and a prefix cache matches from the front, so ~22k tokens
        of otherwise append-only conversation history behind it were re-prefilled
        each time. Volatile content belongs after the stable bulk, not before it.
        """
        entries = self.memory.read_unprocessed_history(
            since_cursor=self.memory.get_last_dream_cursor()
        )
        if session_key is not None:
            # Only this session's own summaries. Entries without a "session"
            # field (written before tagging existed) are excluded too — better
            # to lose a little context once than to keep leaking cross-session
            # content until Dream drains the backlog.
            entries = [e for e in entries if e.get("session") == session_key]
        if not entries:
            return ""
        capped = entries[-self._MAX_RECENT_HISTORY:]
        history_text = "\n".join(
            f"- [{e['timestamp']}] {e['content']}" for e in capped
        )
        return "# Recent History\n\n" + truncate_text(history_text, self._MAX_HISTORY_CHARS)

    def shared_prompt_prefix(self, channel: str | None = None) -> str:
        """The head of the system prompt that every assistant in the house sends.

        Byte-identical across instances, and a true prefix of what
        `build_system_prompt` produces — the next thing in the real prompt is
        "\n\n## USER.md", which is where instances start to differ.

        It exists to be *prewarmed*. FreeToken's prefix cache will happily
        append to a sequence it has already seen ending at a given point, but it
        will not re-enter one from the middle: measured 2026-09-10, two requests
        sharing a 12.6k-token head but differing after it took 18.7s and 16.4s,
        i.e. no reuse at all. Send this text as a request of its own first and
        the boundary exists, after which the same two requests cost 4.3s. So the
        prewarm is not an optimisation of the cache, it is the thing that makes
        the cache usable across instances at all.

        Returns "" when no shared file is present, which callers treat as
        "nothing to prewarm" rather than an error.
        """
        parts = [self._get_identity(channel=channel)]
        blocks = []
        for filename in self.BOOTSTRAP_FILES:
            if filename in self.PER_USER_BOOTSTRAP_FILES:
                break  # stop at the first divergence, not just skip it
            file_path = self.workspace / filename
            if file_path.is_file():
                blocks.append(f"## {filename}\n\n{file_path.read_text(encoding='utf-8')}")
        if not blocks:
            return ""
        parts.append("\n\n".join(blocks))
        return "\n\n".join(parts)

    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.is_file():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _is_template_content(content: str, template_path: str) -> bool:
        """Check if *content* is identical to the bundled template (user hasn't customized it)."""
        try:
            tpl = pkg_files("nanobot") / "templates" / template_path
            if tpl.is_file():
                return content.strip() == tpl.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        return False

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        current_role: str = "user",
        session_summary: str | None = None,
        session_key: str | None = None,
        standing_block: str = "",
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call.

        *standing_block* is a caller's per-turn instruction for turns that did
        not bring one — a finished background task being phrased, a scheduled
        job firing. Those start with no user message to prepend it to (and, for
        a subagent follow-up, no user *role* either), so it rides the system
        prompt instead. Ordinary turns carry their own copy and pass nothing
        here; see `nanobot/utils/standing_context.py`.
        """
        runtime_ctx = self._build_runtime_context(channel, chat_id, self.timezone, session_summary=session_summary)
        user_content = self._build_user_content(current_message, media)

        # The session key scopes Recent History (see recent_history_block).
        # Callers with a Session pass session.key; otherwise reconstruct it the
        # way InboundMessage.session_key does.
        if session_key is None and channel and chat_id:
            session_key = f"{channel}:{chat_id}"

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        #
        # Recent History joins them here rather than riding at the end of the
        # system prompt. Both are volatile -- they change on most turns -- and
        # everything volatile has to sit *after* the stable bulk, or the prefix
        # cache matches nothing behind it.
        recent = self.recent_history_block(session_key)
        preamble = f"{recent}\n\n{runtime_ctx}" if recent else runtime_ctx
        if isinstance(user_content, str):
            merged = f"{preamble}\n\n{user_content}"
        else:
            merged = [{"type": "text", "text": preamble}] + user_content
        system_prompt = self.build_system_prompt(skill_names, channel=channel, session_key=session_key)
        if standing_block:
            # Last, so it wins over the general instructions above it — that is
            # what a per-turn standing instruction is for.
            system_prompt = f"{system_prompt}\n\n{standing_block}"
        messages = [
            {"role": "system", "content": system_prompt},
            *self._strip_stale_reasoning(history),
        ]
        if messages[-1].get("role") == current_role:
            last = dict(messages[-1])
            last["content"] = self._merge_message_content(last.get("content"), merged)
            messages[-1] = last
            return messages
        messages.append({"role": current_role, "content": merged})
        return messages

    @staticmethod
    def _strip_stale_reasoning(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Empty `reasoning_content` on *previous* turns, keeping the field.

        A model's chain of thought is scratch work for the turn that produced
        it. It is stored on the assistant message and sits inside
        `_ALLOWED_MSG_KEYS`, so without this it is sent back on every later
        message — billed as input each time, and ignored by the DeepSeek-style
        APIs that emitted it in the first place. On a chatty turn that is
        thousands of tokens of deliberation re-sent forever.

        **The value goes; the key stays.** Deleting it outright cost a whole
        turn on 2026-08-13. A thinking model rejects a request whose last
        assistant message has no `reasoning_content` at all, and a subagent
        follow-up is exactly the shape that leaves an *older* message in that
        position: the injected result is an assistant message, the provider
        pops trailing assistant messages before sending, and what is left last
        is the previous turn's tool call — whose field this had just removed.
        The turn died with the raw provider error in Alex's finanzas chat.

        An empty string reads the same to the API as a turn where nothing was
        thought, costs nothing (`estimate_prompt_tokens` counts only non-empty
        values), and cannot leave any message in the invalid state, whichever
        one ends up last. Verified against the live gateway on 2026-08-12:
        absent → 400, `None` → 400, `""` → 200.

        Two things are deliberately left alone:
        - `thinking_blocks`, which Anthropic's extended thinking requires to
          stay attached across a tool-use exchange; dropping those breaks the
          request rather than merely costing tokens.
        - The current turn. These messages are added later via
          `add_assistant_message`, so reasoning still flows between a tool call
          and its result inside one turn, which is where it is actually useful.
        """
        out: list[dict[str, Any]] = []
        for msg in history:
            if msg.get("reasoning_content"):
                msg = {**msg, "reasoning_content": ""}
            out.append(msg)
        return out

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_meta": {"path": str(p)},
            })

        if not images:
            return text
        blocks = list(images)
        if text:
            blocks.append({"type": "text", "text": text})
        return blocks

    def add_tool_result(
        self, messages: list[dict[str, Any]],
        tool_call_id: str, tool_name: str, result: Any,
    ) -> list[dict[str, Any]]:
        """Add a tool result to the message list."""
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result})
        return messages

    def add_assistant_message(
        self, messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        thinking_blocks: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
        """Add an assistant message to the message list."""
        messages.append(build_assistant_message(
            content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
            thinking_blocks=thinking_blocks,
        ))
        return messages
