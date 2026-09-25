"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import mimetypes
import os
import re
import time
from contextlib import AsyncExitStack, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.autocompact import AutoCompact
from nanobot.agent.context import ContextBuilder
from nanobot.agent.hook import AgentHook, AgentHookContext, CompositeHook
from nanobot.utils.debug_log import log_error as _log_error
from nanobot.utils.homeweb_chat_id import is_space_session
from nanobot.agent.memory import Consolidator, Dream
from nanobot.agent.usage_report import report_usage
from nanobot.utils.profiling import PROFILER
from nanobot.agent.classify import (
    TurnClass,
    TurnClassifier,
    _text_of,
    continuation_messages,
    previous_assistant_text,
)
from nanobot.agent.runner import (
    _MAX_INJECTIONS_PER_TURN,
    AgentRunner,
    AgentRunResult,
    AgentRunSpec,
    _strip_skill_invocation_text,
)
from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.agent.subagent import SubagentManager
from nanobot.agent import delegate
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.notebook import NotebookEditTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.search import GlobTool, GrepTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.self import MyTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools import plan as plan_tool
from nanobot.agent.tools.vision import DescribeImageTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.command import CommandContext, CommandRouter, register_builtin_commands
from nanobot.config.schema import AgentDefaults, RoutingConfig
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session, SessionManager
from nanobot.utils import standing_context
from nanobot.utils.document import extract_documents
from nanobot.utils.helpers import detect_image_mime
from nanobot.utils.helpers import image_placeholder_text
from nanobot.utils.helpers import truncate_text as truncate_text_fn
from nanobot.utils.progress_events import (
    build_tool_event_finish_payloads,
    build_tool_event_start_payload,
    invoke_on_progress,
    on_progress_accepts_tool_events,
)
from nanobot.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

if TYPE_CHECKING:
    from nanobot.config.schema import (ChannelsConfig, ExecToolConfig, HarnessConfig, ToolsConfig,
                                       WebToolsConfig)
    from nanobot.cron.service import CronService


UNIFIED_SESSION_KEY = "unified:default"


class _LoopHook(AgentHook):
    """Core hook for the main loop."""

    def __init__(
        self,
        agent_loop: AgentLoop,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        *,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
    ) -> None:
        super().__init__(reraise=True)
        self._loop = agent_loop
        self._on_progress = on_progress
        self._on_stream = on_stream
        self._on_stream_end = on_stream_end
        self._channel = channel
        self._chat_id = chat_id
        self._message_id = message_id
        # Filled in once the turn has picked its model, which happens after
        # this hook is built. They ride out on the `usage` step so the chat can
        # say which model actually answered and how hard it was asked to think
        # -- neither is guessable from the reply, and `serving_model` can differ
        # from the configured one while a model is inside its outage window.
        self.turn_model: str | None = None
        self.turn_effort: str | None = None
        self._stream_buf = ""
        self._last_alive = 0.0

    # A thinking model emits hundreds of reasoning deltas a second. What the
    # caller needs from them is one bit — still working — so they are collapsed
    # to a tick at this interval and the rest are dropped on the floor.
    _ALIVE_INTERVAL_S = 3.0

    def wants_streaming(self) -> bool:
        return self._on_stream is not None

    async def on_reasoning(self, context: AgentHookContext, delta: str) -> None:
        if not self._on_progress:
            return
        now = time.monotonic()
        if now - self._last_alive < self._ALIVE_INTERVAL_S:
            return
        self._last_alive = now
        # Deliberately without the thinking text. The caller is being told the
        # turn is alive, and chain-of-thought is not something this house shows
        # anyone mid-answer — `before_execute_tools` already sends a scrubbed
        # snippet when there is a reason to.
        await invoke_on_progress(self._on_progress, "", alive=True)

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        from nanobot.utils.helpers import strip_think

        prev_clean = strip_think(self._stream_buf)
        self._stream_buf += delta
        new_clean = strip_think(self._stream_buf)
        incremental = new_clean[len(prev_clean) :]
        if incremental and self._on_stream:
            await self._on_stream(incremental)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        if self._on_stream_end:
            trim_to: str | None = None
            # What was streamed is the model's raw text; this is the version that
            # gets persisted and the one the reader should end up with. Sent on
            # the final segment too, not just mid-turn ones: a model that
            # restates a call it already made writes the block into its closing
            # answer, and that segment never resumes.
            #
            # Stripped again here rather than trusted. The runner trims content
            # at the end of the JSON *object*, which cuts a fenced block in half:
            # the closing ``` is gone, so the fenced-block pattern no longer
            # matches and what reaches the reader is a dangling "```json".
            # Observed live on every camera turn.
            if context.response is not None and context.response.content is not None:
                # _strip_think returns None for empty input, and a response that
                # is only tool calls has content == "" — which passes the guard
                # above and then reached the regex as None. Empty means "the
                # client should end up with nothing", which is "" and not None:
                # None tells it to keep whatever it painted.
                thought = self._loop._strip_think(context.response.content)
                trim_to = _strip_skill_invocation_text(thought) if thought else ""
            await self._on_stream_end(resuming=resuming, trim_to=trim_to)
        self._stream_buf = ""

    async def before_iteration(self, context: AgentHookContext) -> None:
        self._loop._current_iteration = context.iteration

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        if self._on_progress:
            if not self._on_stream:
                thought = self._loop._strip_think(
                    context.response.content if context.response else None
                )
                if thought:
                    await self._on_progress(thought)
            tool_hint = self._loop._strip_think(self._loop._tool_hint(context.tool_calls))
            tool_events = [build_tool_event_start_payload(tc) for tc in context.tool_calls]
            thinking: str | None = None
            if context.response:
                thinking = getattr(context.response, "reasoning_content", None) or ""
                if not thinking:
                    blocks = getattr(context.response, "thinking_blocks", None) or []
                    thinking = " ".join(
                        b.get("thinking", "") for b in blocks if isinstance(b, dict)
                    ).strip()
                thinking = thinking or None
            await invoke_on_progress(
                self._on_progress,
                tool_hint,
                tool_hint=True,
                tool_events=tool_events,
                thinking=thinking,
            )
        for tc in context.tool_calls:
            args_str = json.dumps(tc.arguments, ensure_ascii=False)
            logger.info("Tool call: {}({})", tc.name, args_str[:200])
        self._loop._set_tool_context(self._channel, self._chat_id, self._message_id)

    async def after_iteration(self, context: AgentHookContext) -> None:
        if (
            self._on_progress
            and context.tool_calls
            and context.tool_events
            and on_progress_accepts_tool_events(self._on_progress)
        ):
            tool_events = build_tool_event_finish_payloads(context)
            if tool_events:
                await invoke_on_progress(
                    self._on_progress,
                    "",
                    tool_hint=False,
                    tool_events=tool_events,
                )
        u = context.usage or {}
        logger.debug(
            "LLM usage: prompt={} completion={} cached={}",
            u.get("prompt_tokens", 0),
            u.get("completion_tokens", 0),
            u.get("cached_tokens", 0),
        )
        if self._on_progress and u:
            # Copied rather than mutated: `context.usage` belongs to the runner
            # and is read again after this.
            await invoke_on_progress(
                self._on_progress, "", tool_hint=False,
                usage={**u, "model": self.turn_model, "effort": self.turn_effort},
            )

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        clean = self._loop._strip_think(content)
        # A reply consisting of nothing but a skill-invocation block is the
        # plumbing leaking into the chat — the interceptor did not consume it,
        # so the family gets {"skill": "menu", "action": "list_menu"} where an
        # answer should be. Returning empty hands it to the runner's blank-reply
        # path, which asks the model again rather than publishing the block.
        #
        # Deliberately only when the block is the *entire* message. Someone can
        # legitimately ask to be shown an invocation, and that answer has prose
        # around it; a bare block never does. This is a net, not a cure — the
        # underlying miss is intermittent and not yet reproduced on demand.
        if clean and not _strip_skill_invocation_text(clean):
            logger.warning(
                "Dropping a reply that was only a skill-invocation block: {}",
                clean.replace("\n", " ")[:120],
            )
            return ""
        return clean


def _messages_have_image(messages: list) -> bool:
    """True if any message carries a live image_url content block.

    Only the current turn's freshly-built user content can contain one; persisted
    history is sanitized to text placeholders before saving.
    """
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "image_url" for b in content
        ):
            return True
    return False


def _swap_images_for_paths(messages: list) -> bool:
    """Replace live image blocks with ``[image: path]`` text, in place.

    Lets the main model keep the turn and reach the picture through
    ``describe_image`` instead of handing the whole conversation to the vision
    model. That matters because the vision model here is an 8B running locally
    with a 4096-token context: the system prompt and tool definitions alone are
    6282 tokens, so an image turn failed outright with

        request (6282 tokens) exceeds the available context size (4096 tokens)

    describe_image never hit that because it sends a few hundred tokens and no
    tools. This routes every image down that same narrow path.

    Returns False and changes nothing unless *every* image block carries a
    usable path — swapping only some would silently drop a picture, which is
    worse than falling back to the old routing.
    """
    targets: list[tuple[dict, int, str]] = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for index, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "image_url":
                continue
            path = (block.get("_meta") or {}).get("path")
            if not path:
                return False
            targets.append((message, index, str(path)))

    if not targets:
        return False
    for message, index, path in targets:
        message["content"][index] = {
            "type": "text",
            "text": image_placeholder_text(path),
        }
    logger.info(
        "Image turn kept on the main model; {} image(s) handed to describe_image",
        len(targets),
    )
    return True


# A file a skill produced for the user to see. Skills only build one of these
# for something meant to be delivered, so its presence in a tool result is the
# signal — no skill-specific knowledge needed here.
#
# Spaces are inside the path, not a terminator: files are named in Spanish and
# "Sheet 1 — Cover.png" is the ordinary case. With `[^)\s]+` a delivered link
# whose filename had a space read as no link at all, and the rescue below sent
# the file a second time.
_DOWNLOAD_LINK_RE = re.compile(
    r"\[[^\]\n]*\]\((?:<download:[^>\n]+>|download:[^)\n]+)\)")


# A skill saying this result is a side effect, not something handed to the
# reader. Matched in the serialised result rather than parsed, like everything
# else here: the tool content is text by the time it reaches this function.
_NOT_A_DELIVERY_RE = re.compile(r'"deliver"\s*:\s*(?:false|False)')


def _link_target(link: str) -> str:
    """The path out of a rescued link, in either markdown form."""
    inside = link.rsplit("](", 1)[-1].rstrip(")").strip()
    if inside.startswith("<") and inside.endswith(">"):
        inside = inside[1:-1]
    return inside.split("download:", 1)[-1]
# Fallback signal: the file itself. A skill that improvises its own code — and
# the model does improvise, badly — often prints just the path it wrote, with no
# link to lift. workspace/media exists solely to hold things the chat will show,
# so a file landing there is the same intent expressed one layer down.
#
# Documents, not only images. Asked for the insurance policy, the model wrote
# its own Python instead of calling the skill, saved a real PDF into media/ and
# announced "I sent them the file" having sent nothing — and this rescue skipped
# it because a .pdf is not a .jpg. The reader cannot tell a document that never
# arrived from a photo that never arrived.
# The name, which is the part that was wrong. It was `[\w.\-]+`, and files
# here can carry spaces and accents: "media/Sheet 1 — Cover.png" matched nothing, so
# a picture the model announced and never attached went unrescued. An accented
# name *without* a space did match (`\w` is Unicode), which is what kept the
# gap hidden.
#
# Spaces are allowed, but never two together and never one just before the
# extension — a filename has neither, and that is exactly what stops this from
# reading "media/a.txt es un .png" out of a sentence as one path.
_MEDIA_NAME = r"(?:[^\s/\n\"'<>|]|[ ](?![ .]))+?"
_MEDIA_IMAGE_RE = re.compile(
    r"\bmedia/" + _MEDIA_NAME + r"\.(?:jpg|jpeg|png|gif|webp"
    r"|pdf|docx|doc|xlsx|xls|pptx|csv|txt|md|zip"
    r"|mp3|m4a|ogg|wav|mp4|mov)\b",
    re.IGNORECASE,
)
# These enumerate the workspace instead of producing something for the reader;
# a listing that happens to mention media/ is not an undelivered photo.
_LISTING_TOOLS = frozenset({"list_dir", "glob", "grep", "read_file"})
_MAX_RESCUED_LINKS = 4
# Trailing epoch stamp skills append to keep filenames unique, e.g.
# cam_front_1785263359.jpg. Stripping it is what makes two captures of the same
# camera recognisable as the same subject.
_TIMESTAMP_SUFFIX_RE = re.compile(r"[_-]\d{9,}$")


def _undelivered_download_links(
    messages: list, final_content: str | None, turn_start: int = 0,
) -> list[str]:
    """Links a tool produced for the user that never made it into the reply.

    Models announce an attachment and then omit it — "here you go 👇" with
    nothing after the arrow. Camera snapshots were captured, saved and served
    correctly fifteen times running without a single one reaching the chat.
    Two prompt-shaped fixes failed: SKILL.md said to include the link verbatim,
    in bold, with a worked example, and the skill was changed to hand back a
    finished sentence with the link already in it. Neither survived contact with
    the model, so this stops asking and checks.

    Returns nothing when the reply already carries a link — the normal case,
    where the model did its job and this must not duplicate it.

    *turn_start* is where this turn's messages begin; everything before it is
    conversation history. Scanning history was a real bug in both directions: a
    request for the patio delivered a Front snapshot captured hours earlier,
    because that morning's tool output was still in the list and looked
    undelivered. In the other direction, an old assistant message carrying a
    link would suppress a rescue that this turn genuinely needed. Only what
    happened in this turn can be undelivered now.
    """
    if _DOWNLOAD_LINK_RE.search(final_content or ""):
        return []

    messages = messages[turn_start:]

    # The reply is not the only thing the reader sees. A streaming turn shows
    # every assistant segment, and the model often delivers the photo in one of
    # those and then signs off with a line that carries no link — which read as
    # undelivered here and sent the picture a second time. If any assistant
    # message in this turn already carried a link, the file arrived.
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and _DOWNLOAD_LINK_RE.search(content):
            return []

    tool_messages = [
        m for m in messages
        if m.get("role") == "tool" and isinstance(m.get("content"), str)
    ]

    seen: set[str] = set()
    links: list[str] = []

    def _add(link: str) -> None:
        if link not in seen:
            seen.add(link)
            links.append(link)

    for message in tool_messages:
        # A result that says it is not a delivery keeps its link out of this.
        # Saving, copying and moving all hand back a ready-made link now, so
        # the model can offer the file without writing a path from memory —
        # but "ordena mi carpeta, mueve los 6 PDF" is not six announcements,
        # and the rescue was posting a download button for each of them under
        # a reply that correctly said only "listo".
        if _NOT_A_DELIVERY_RE.search(message["content"]):
            continue
        for link in _DOWNLOAD_LINK_RE.findall(message["content"]):
            _add(link)

    # Only when nothing handed us a ready-made link. A skill that prints the
    # path it wrote and nothing else still produced a file for the reader; a
    # tool that merely lists the directory did not.
    if not links:
        for message in tool_messages:
            if message.get("name") in _LISTING_TOOLS:
                continue
            for path in _MEDIA_IMAGE_RE.findall(message["content"]):
                _add(f"[{Path(path).stem}](download:{path})")

    links = _newest_per_subject(links)

    if len(links) > _MAX_RESCUED_LINKS:
        logger.warning(
            "Delivering {} of {} undelivered files; a turn producing this many "
            "is usually a skill being called in a loop",
            _MAX_RESCUED_LINKS, len(links),
        )
        links = links[-_MAX_RESCUED_LINKS:]
    return links


def _newest_per_subject(links: list[str]) -> list[str]:
    """Collapse repeated captures of the same thing down to the latest one.

    The model calls snapshot more than once in a turn — listing cameras, second
    guessing the name, retrying — and each call writes a new timestamped file.
    Delivering all of them sent the family three identical photos of the front
    door. They are not three answers; they are one answer taken three times.

    Files are grouped by name with the trailing timestamp removed, so
    cam_front_1785263359.jpg and cam_front_1785263400.jpg collapse while
    cam_patio_… stays separate: asking for two different cameras still returns
    two pictures. Order follows the last capture of each subject, which is the
    freshest view.
    """
    newest: dict[str, str] = {}
    for link in links:
        target = _link_target(link)
        # The folder is part of the subject. Grouping on the bare name was
        # collision-free while every rescued file lived in the one flat media/
        # directory; with share paths, `user1/alfred/lista.md` and
        # `familia/lista.md` are two different files that both stem to "lista",
        # and one of them was silently dropped.
        stem = _TIMESTAMP_SUFFIX_RE.sub("", Path(target).stem) or target
        subject = f"{Path(target).parent}/{stem}"
        newest[subject] = link          # later capture wins
    if len(newest) < len(links):
        logger.info(
            "Collapsed {} captures into {} (same subject, newer file wins)",
            len(links), len(newest),
        )
    return list(newest.values())


# An earlier image is re-attached to a text turn only when the message actually
# refers to it (keeps vision routing "only when required").
_IMAGE_PLACEHOLDER_RE = re.compile(r"\[image:\s*(.+?)\]")
_IMAGE_REFERENCE_RE = re.compile(
    r"\b(?:foto|fotos|fotograf[ií]a|imagen|im[aá]genes|image|images|picture|pictures|"
    r"photo|photos|screenshot|captura|pantallazo|dibujo|gr[aá]fico|meme)\b",
    re.IGNORECASE | re.UNICODE,
)


def _content_to_text(content) -> str:
    """Flatten a message's content (string or block list) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _text_references_image(text: str) -> bool:
    return bool(_IMAGE_REFERENCE_RE.search(text or ""))


def _recent_image_paths(messages: list) -> list[str]:
    """Paths from the most recent message that carried image placeholders."""
    for m in reversed(messages):
        content = m.get("content") if isinstance(m, dict) else None
        text = _content_to_text(content)
        if not text:
            continue
        paths = _IMAGE_PLACEHOLDER_RE.findall(text)
        if paths:
            return paths
    return []


def _load_image_block(path: str) -> dict | None:
    """Re-encode an on-disk image as an image_url block, or None if unreadable."""
    try:
        data = Path(path).read_bytes()
    except Exception:
        return None
    if not data:
        return None
    mime = detect_image_mime(data) or mimetypes.guess_type(path)[0] or "image/png"
    b64 = base64.b64encode(data).decode()
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{b64}"},
        "_meta": {"path": path},
    }


def _reattach_referenced_image(messages: list) -> bool:
    """If the current (text-only) user turn refers to an image, reload the most
    recent one from disk and append it to that message. Returns True if attached.
    Mutates `messages` in place."""
    last_user = None
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            last_user = m
            break
    if last_user is None:
        return False
    if not _text_references_image(_content_to_text(last_user.get("content"))):
        return False
    paths = _recent_image_paths(messages)
    if not paths:
        return False
    blocks = [b for b in (_load_image_block(p) for p in paths) if b]
    if not blocks:
        return False
    content = last_user.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}] if content else []
    elif isinstance(content, list):
        content = list(content)
    else:
        content = []
    content.extend(blocks)
    last_user["content"] = content
    return True


def _prompt_shape(messages: list[dict], tools: Any) -> dict[str, int]:
    """What the prompt is *made of*, once per turn.

    The profiler counts a turn's prompt tokens, which is the bill but not the
    reason for it. Measured 2026-08-24, the notification triage turn sent
    30,303 prompt tokens to answer in ~100 — and the session behind it held
    five messages and 3.5 KB, so the history everyone assumed was the problem
    was ~3% of it — 3.5 KB against the ~121 KB those tokens come to. The rest
    is the system prompt and the tool schemas, and
    neither was visible from anywhere.

    Characters rather than tokens: tokenising the prompt a second time to
    profile it would cost more than the thing being profiled, and the ratio
    between the parts is what the question needs. Once per turn, not per call
    — the tool definitions do not change between a turn's iterations.

    `history` is everything after the system prompt, so it carries this turn's
    own user message and the Runtime Context block merged into it as well as
    the session's — the parts have to add up to the prompt, and that message
    is part of the prompt. It counts the fields a provider actually sends, not
    just `content`: `tool_calls`, `reasoning_content`, `name` and
    `tool_call_id` are all in `_ALLOWED_MSG_KEYS`, and an assistant message
    that called `write_file` carries an empty `content` beside kilobytes of
    argument JSON. The field list mirrors `helpers.estimate_message_tokens`,
    which is the same measurement in tokens. Image blocks stay out: a data:
    URI is tens of thousands of characters and no tokens like the ones being
    counted here, and including it would drown the number it explains.

    Three things it is *not*, before the panel invites the comparison. It is
    iteration 0's input, while the span's `prompt_tokens` is the sum over every
    call the turn made, so on a five-iteration tool turn the parts describe one
    fifth of the bill. It is the list this loop built, which the runner
    microcompacts, budgets and snips per request before sending. And `system`
    is not the static half: `build_system_prompt` folds `# Memory`,
    `# Active Skills` and `# Recent History` — this session's own consolidated
    summaries, capped at 32 KB — into `messages[0]`, so conversation-derived
    text lands under `system_chars` and `history_chars` is only the raw tail.
    Splitting those out means splitting `build_system_prompt`; until then,
    read a large `sys` as "prompt", not as "SOUL.md".
    """
    system = messages[0].get("content") if messages else ""
    history = messages[1:]

    def _chars(content: Any) -> int:
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            return sum(len(b.get("text") or "") for b in content if isinstance(b, dict))
        return 0

    def _msg_chars(message: dict) -> int:
        total = _chars(message.get("content"))
        calls = message.get("tool_calls")
        if calls:
            total += len(json.dumps(calls, ensure_ascii=False))
        for key in ("reasoning_content", "name", "tool_call_id"):
            value = message.get(key)
            if isinstance(value, str):
                total += len(value)
        return total

    tool_count, tool_chars = 0, 0
    try:
        definitions = tools.get_definitions() or []
        # `ensure_ascii=False` for the same reason `estimate_prompt_tokens`
        # uses it: with the default every accented character in a tool
        # description becomes six, and this number is only useful next to the
        # raw character counts above it.
        tool_count, tool_chars = len(definitions), len(json.dumps(definitions, ensure_ascii=False))
    except Exception as exc:
        # A registry that cannot describe itself is not worth failing a turn
        # over; the rest of the shape is still true. But a silent zero here
        # reads in the panel as "this turn sent no tools", which is the
        # opposite of the answer the column exists to give, so say so once.
        logger.debug("Prompt shape: tool definitions not measurable: {}", exc)

    return {
        "system_chars": _chars(system),
        "history_msgs": len(history),
        "history_chars": sum(_msg_chars(m) for m in history),
        "tool_count": tool_count,
        "tool_chars": tool_chars,
    }



# Tools that change something whatever their arguments. A read-only step that
# had `cron` scheduled a retry the person had asked the planner for, and the
# planner, reading "scheduled", scheduled a second one (2026-09-24, jobs
# ab6fc41b and 1efe1809).
_ACTING_TOOLS = {"cron", "message", "write_file", "edit_file", "spawn", "plan"}


def _changes_things(tool_name: str) -> bool:
    """A tool that acts rather than reads: the ones above, and Home Assistant's
    intents, which are `Hass<Verb>` -- TurnOn, LightSet, SetVolume, Broadcast --
    where only the `HassGet...` ones read; GetLiveContext and GetDateTime carry
    no prefix."""
    if tool_name in _ACTING_TOOLS:
        return True
    if not tool_name.startswith("mcp_homeassistant"):
        return False
    verb = tool_name.rsplit("__", 1)[-1].rsplit("_", 1)[-1]
    return verb.startswith("Hass") and not verb.startswith("HassGet")


# What a step's words have to say for a family of tools to be offered.
_STEP_TOOL_WORDS: tuple[tuple[re.Pattern, tuple[str, ...]], ...] = (
    (re.compile(r"home ?assistant|\bha\b|\bhass", re.I), ("mcp_homeassistant",)),
    (re.compile(r"\bweb\b|internet|online|search|busca|http|url|p[aá]gina|sitio|site\b", re.I),
     ("web_search", "web_fetch", "mcp_crawl4ai")),
    (re.compile(r"cron|schedule|remind|agend|recordatorio|in \d+ min|en \d+ min", re.I), ("cron",)),
    (re.compile(r"image|imagen|photo|foto|picture", re.I), ("describe_image",)),
)
# Always there: a skill's SKILL.md is read before it is called.
_STEP_BASE_TOOLS = ("read_file",)


def tools_for_step(step: str, names: list[str]) -> list[str]:
    """The tools one plan step is offered: the ones its text names, the
    families its words call for, and reading a skill's instructions.

    A step on a small local model reaches for whatever is in its list. With
    every one of Alfred's ~60 tools there, Bonsai 2 answered "flash each light"
    by scraping example.com through Bright Data nine times, and a chores step
    wrote seven files instead of calling the chores skill (2026-09-25). A
    skill is not a tool here -- it is a `{"skill": ...}` block the runtime
    runs -- so a step that only calls skills is offered almost nothing, and
    that is the point. The planner writes the step, so the planner decides:
    naming a tool in a step is how it hands one over.
    """
    text = step.lower()
    words = set(re.findall(r"[a-z0-9_]+", text))
    chosen = [n for n in names if n in _STEP_BASE_TOOLS]
    for n in names:
        if n in chosen or n in ("plan", "spawn", "exec"):
            continue
        # An MCP tool is named by what follows its server: GetLiveContext for
        # mcp_homeassistant_GetLiveContext (or ..._homeassistant__GetLiveContext).
        short = n.rsplit("__", 1)[-1] if "__" in n else (
            n.split("_", 2)[-1] if n.startswith("mcp_") else n)
        if n.lower() in words or short.lower() in words:
            chosen.append(n)
            continue
        if any(rx.search(text) and n.startswith(prefixes) for rx, prefixes in _STEP_TOOL_WORDS):
            chosen.append(n)
    return chosen

class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    _RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
    _PENDING_USER_TURN_KEY = "pending_user_turn"

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int | None = None,
        context_window_tokens: int | None = None,
        context_block_limit: int | None = None,
        max_tool_result_chars: int | None = None,
        provider_retry_mode: str = "standard",
        web_config: WebToolsConfig | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        timezone: str | None = None,
        session_ttl_minutes: int = 0,
        hooks: list[AgentHook] | None = None,
        unified_session: bool = False,
        disabled_skills: list[str] | None = None,
        tools_config: ToolsConfig | None = None,
        subagent_provider: "LLMProvider | None" = None,
        subagent_model: str | None = None,
        harness_config: "HarnessConfig | None" = None,
        subagent_powerful_provider: "LLMProvider | None" = None,
        subagent_powerful_model: str | None = None,
        powerful_provider: "LLMProvider | None" = None,
        powerful_model: str | None = None,
        model_profiles: dict | None = None,
        reasoning_effort_profiles: dict | None = None,
        reasoning_effort_powerful: str | None = None,
        reasoning_effort_default: str | None = None,
        vision_provider: "LLMProvider | None" = None,
        vision_model: str | None = None,
        classifier_provider: "LLMProvider | None" = None,
        classifier_model: str | None = None,
        classifier_reasoning_effort: str | None = "none",
        routing: "RoutingConfig | None" = None,
        plan_provider: "LLMProvider | None" = None,
        plan_model: str | None = None,
        plan_step_provider: "LLMProvider | None" = None,
        plan_step_model: str | None = None,
    ):
        from nanobot.config.schema import ExecToolConfig, ToolsConfig, WebToolsConfig

        _tc = tools_config or ToolsConfig()
        defaults = AgentDefaults()
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = (
            max_iterations if max_iterations is not None else defaults.max_tool_iterations
        )
        self.context_window_tokens = (
            context_window_tokens
            if context_window_tokens is not None
            else defaults.context_window_tokens
        )
        self.context_block_limit = context_block_limit
        self.max_tool_result_chars = (
            max_tool_result_chars
            if max_tool_result_chars is not None
            else defaults.max_tool_result_chars
        )
        self.provider_retry_mode = provider_retry_mode
        self.web_config = web_config or WebToolsConfig()
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}
        self._extra_hooks: list[AgentHook] = hooks or []

        self.context = ContextBuilder(workspace, timezone=timezone, disabled_skills=disabled_skills)
        self._detached_plans: set[asyncio.Future] = set()
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        self.runner = AgentRunner(provider)
        # Per-turn escalation to a stronger model, requested by the caller (see
        # `powerful=` on process_direct). The main model is chosen for latency —
        # the family asks about the weather far more often than it asks for an
        # architecture review — so the harder contexts opt in per turn rather
        # than everyone paying for the slower model all day. Falls back to the
        # main model when unconfigured, so asking for it is never an error.
        self.powerful_model = powerful_model or self.model
        self._powerful_runner = (
            AgentRunner(powerful_provider) if powerful_provider else self.runner
        )
        # role -> (runner, model). A caller that knows what *kind* of turn this
        # is names the role; which model that means is configuration, so the
        # roster stays here and no client has to be redeployed to change it.
        self._profiles: dict[str, tuple] = {
            role: (AgentRunner(prov) if prov else self.runner, model)
            for role, (prov, model) in (model_profiles or {}).items() if model
        }
        # Thinking, per role. Kept beside the roster rather than folded into it
        # because it is a different question with a different default: an
        # unset model means "use the main one", an unset effort means "leave
        # the provider's setting alone", and collapsing them would make one
        # look like the other.
        self._profile_efforts: dict[str, str] = dict(reasoning_effort_profiles or {})
        self._powerful_effort = reasoning_effort_powerful
        # The default turn's effort. Kept apart from the two above rather than
        # folded into `_profile_efforts` under a made-up key: this turn has no
        # profile, and giving it one would make it selectable as a Profession.
        self._default_effort = reasoning_effort_default
        # The turn classifier and the routing rules -- see classify.py. Off
        # when no model is named: the ladder below then reads only the
        # caller's flags, which is what it did before 2026-09-21.
        self._routing = routing or RoutingConfig()
        self._classifier: TurnClassifier | None = (
            TurnClassifier(
                classifier_provider, classifier_model,
                timeout_s=self._routing.timeout_s,
                long_message_chars=self._routing.long_message_chars,
                reasoning_effort=classifier_reasoning_effort,
            ) if classifier_provider is not None and classifier_model else None
        )
        self._classifier_warmed = False
        # Per-turn vision routing: use a separate provider/model for turns that
        # carry images (the default model is text-only). Off when unset.
        self._vision_runner = AgentRunner(vision_provider) if vision_provider else None
        self._vision_model = vision_model or self.model
        # Kept as well as the runner: routing only covers images present when the
        # turn starts, while describe_image needs the provider for images a tool
        # produces mid-turn.
        self._vision_provider = vision_provider
        # Work of several steps (tools/plan.py): the planner, and the model
        # each step runs on. Unset, the planner is the main model and the
        # steps follow `routing.plan_executor`.
        self._plan_runner = AgentRunner(plan_provider) if plan_provider else self.runner
        self.plan_model = plan_model or self.model
        self._plan_step_provider = plan_step_provider
        self._plan_step_model = plan_step_model
        self.subagents = SubagentManager(
            provider=subagent_provider or provider,
            workspace=workspace,
            bus=bus,
            model=subagent_model or self.model,
            web_config=self.web_config,
            max_tool_result_chars=self.max_tool_result_chars,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
            disabled_skills=disabled_skills,
            classifier=self._classifier,
            routing=self._routing,
            harness=harness_config,
            main_model=self.model,
            main_provider=provider,
        )
        self._unified_session = unified_session
        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stacks: dict[str, AsyncExitStack] = {}
        self._mcp_connected = False
        self._mcp_connecting = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: list[asyncio.Task] = []
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Per-session pending queues for mid-turn message injection.
        # When a session has an active task, new messages for that session
        # are routed here instead of creating a new task.
        self._pending_queues: dict[str, asyncio.Queue] = {}
        # NANOBOT_MAX_CONCURRENT_REQUESTS: <=0 means unlimited; default 3.
        _max = int(os.environ.get("NANOBOT_MAX_CONCURRENT_REQUESTS", "3"))
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max) if _max > 0 else None
        )
        self.consolidator = Consolidator(
            store=self.context.memory,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=self.context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=provider.generation.max_tokens,
        )
        self.auto_compact = AutoCompact(
            sessions=self.sessions,
            consolidator=self.consolidator,
            session_ttl_minutes=session_ttl_minutes,
        )
        self.dream = Dream(
            store=self.context.memory,
            provider=provider,
            model=self.model,
        )
        self._register_default_tools()
        # `tools.disabled`: built-in tools this instance does not offer. Taken
        # out after registration rather than skipped inside it, so one list
        # covers every tool whatever registers it.
        for _name in (_tc.disabled or []):
            if self.tools.has(_name):
                self.tools.unregister(_name)
        if _tc.my.enable:
            self.tools.register(MyTool(loop=self, modify_allowed=_tc.my.allow_set))
        self._runtime_vars: dict[str, Any] = {}
        self._current_iteration: int = 0
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = (
            self.workspace if (self.restrict_to_workspace or self.exec_config.sandbox) else None
        )
        extra_read = [BUILTIN_SKILLS_DIR] if allowed_dir else None
        self.tools.register(
            ReadFileTool(
                workspace=self.workspace, allowed_dir=allowed_dir, extra_allowed_dirs=extra_read
            )
        )
        for cls in (WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        for cls in (GlobTool, GrepTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(NotebookEditTool(workspace=self.workspace, allowed_dir=allowed_dir))
        if self.exec_config.enable:
            self.tools.register(
                ExecTool(
                    working_dir=str(self.workspace),
                    timeout=self.exec_config.timeout,
                    restrict_to_workspace=self.restrict_to_workspace,
                    sandbox=self.exec_config.sandbox,
                    path_append=self.exec_config.path_append,
                    allowed_env_keys=self.exec_config.allowed_env_keys,
                )
            )
        if self.web_config.enable:
            self.tools.register(
                WebSearchTool(config=self.web_config.search, proxy=self.web_config.proxy)
            )
            self.tools.register(WebFetchTool(proxy=self.web_config.proxy))
        if self._vision_provider is not None:
            self.tools.register(
                DescribeImageTool(
                    provider=self._vision_provider,
                    model=self._vision_model,
                    workspace=self.workspace,
                    allowed_dir=allowed_dir,
                    extra_allowed_dirs=extra_read,
                )
            )
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        self.tools.register(plan_tool.PlanTool())
        if self.cron_service:
            self.tools.register(
                CronTool(self.cron_service, default_timezone=self.context.timezone or "UTC")
            )

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobot.agent.tools.mcp import connect_mcp_servers

        try:
            self._mcp_stacks = await connect_mcp_servers(self._mcp_servers, self.tools)
            if self._mcp_stacks:
                self._mcp_connected = True
            else:
                logger.warning("No MCP servers connected successfully (will retry next message)")
        except asyncio.CancelledError:
            logger.warning("MCP connection cancelled (will retry next message)")
            self._mcp_stacks.clear()
        except BaseException as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            self._mcp_stacks.clear()
        finally:
            self._mcp_connecting = False

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        # Compute the effective session key (accounts for unified sessions)
        # so that subagent results route to the correct pending queue.
        effective_key = UNIFIED_SESSION_KEY if self._unified_session else f"{channel}:{chat_id}"
        for name in ("message", "spawn", "cron", "my"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    if name == "spawn":
                        tool.set_context(channel, chat_id, effective_key=effective_key)
                    else:
                        tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        from nanobot.utils.helpers import strip_think

        return strip_think(text) or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hints with smart abbreviation."""
        from nanobot.utils.tool_hints import format_tool_hints

        return format_tool_hints(tool_calls)

    async def _dispatch_command_inline(
        self,
        msg: InboundMessage,
        key: str,
        raw: str,
        dispatch_fn: Callable[[CommandContext], Awaitable[OutboundMessage | None]],
    ) -> None:
        """Dispatch a command directly from the run() loop and publish the result."""
        ctx = CommandContext(msg=msg, session=None, key=key, raw=raw, loop=self)
        result = await dispatch_fn(ctx)
        if result:
            await self.bus.publish_outbound(result)
        else:
            logger.warning("Command '{}' matched but dispatch returned None", raw)

    async def _cancel_active_tasks(self, key: str) -> int:
        """Cancel and await all active tasks and subagents for *key*.

        Returns the total number of cancelled tasks + subagents.
        """
        tasks = self._active_tasks.pop(key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(key)
        return cancelled + sub_cancelled

    def _effective_session_key(self, msg: InboundMessage) -> str:
        """Return the session key used for task routing and mid-turn injections."""
        if self._unified_session and not msg.session_key_override:
            return UNIFIED_SESSION_KEY
        return msg.session_key

    async def warm_up(self) -> None:
        """Ask the classifier once, in the background, so the first real turn is not the cold one.

        Measured 2026-09-21: the real prompt costs gemma4:e4b 600-700 ms warm,
        and the first call after a restart crossed the timeout -- which sent the
        request that started this work to the cheap model. Fire-and-forget: a
        warm-up that fails or takes a while costs nobody a turn.
        """
        # getattr: tests build a loop without __init__ and call process_direct.
        if getattr(self, "_classifier", None) is None or getattr(self, "_classifier_warmed", False):
            return
        self._classifier_warmed = True
        try:
            asyncio.get_running_loop().create_task(self._classifier.classify("hola"))
        except RuntimeError:
            self._classifier_warmed = False

    async def _route_turn(
        self,
        initial_messages: list[dict],
        *,
        session: "Session | None",
        chat_id: str,
        powerful: bool,
        profile: str | None,
        use_vision: bool,
    ) -> TurnClass:
        """Which tier this turn starts on, and who decided.

        Order: the caller's flags (a Profesión, a `powerful` request, an image
        turn, a named profile) are `forced` and skip the classifier; a session
        that escalated recently starts strong for `sticky_turns` (`sticky`);
        otherwise the classifier's label decides -- in `active` mode. In
        `shadow` mode the label is recorded and every such turn runs cheap;
        `off` records nothing.
        """
        if use_vision or profile or powerful or is_space_session(chat_id):
            return TurnClass("forced", "powerful", "caller", "forced")
        mode = self._routing.mode
        if mode == "off" or self._classifier is None:
            return TurnClass("action", "everyday", "", "off")
        if session is not None and mode == "active":
            left = int(session.metadata.get("sticky_powerful") or 0)
            if left > 0:
                session.metadata["sticky_powerful"] = left - 1
                return TurnClass("sticky", "powerful", f"{left} sticky turns left", "sticky")
        last = initial_messages[-1] if initial_messages else {}
        text = _text_of(last.get("content")) if last.get("role") == "user" else ""
        attachments = sum(
            1 for b in (last.get("content") if isinstance(last.get("content"), list) else [])
            if isinstance(b, dict) and b.get("type") in ("image_url", "image", "file")
        )
        route = await self._classifier.classify(
            text, previous=previous_assistant_text(initial_messages[:-1]),
            attachments=attachments,
        )
        if mode != "active":
            # Shadow: keep the label, run cheap. The record is the point.
            return TurnClass(route.label, "everyday", route.reason, f"shadow:{route.source}", route.ms)
        return route

    def _should_escalate_any(self, result: "AgentRunResult", route: TurnClass, runner: AgentRunner) -> bool:
        """A cheap attempt that ended badly, in active mode, once -- whatever
        there is to escalate to (a sub-agent needs no stronger model)."""
        return (
            self._routing.mode == "active"
            and self._routing.max_escalations_per_turn > 0
            and (runner is self.runner or runner is self._plan_runner)
            and route.tier == "everyday"
            and route.escalated_from is None
            and result.stop_reason in set(self._routing.escalate_on)
        )

    def _step_runner(self, plan: "plan_tool.TurnPlan", initial_messages: list[dict],
                     session: "Session | None", fallback: tuple | None = None):
        """What carries out one step of a plan (tools/plan.py `run`).

        The everyday model plans and writes the answer; a step runs on the
        sub-agent's model -- the house's local one -- when `plan_executor` says
        so, with all of Alfred's tools but planning and spawning, and only what
        the step needs: the request, the plan, what earlier steps found. A step
        that dead-ends there runs again on the everyday model, with what it
        already did, so one weak step does not sink the plan.
        """
        last = initial_messages[-1] if initial_messages else {}
        raw = last.get("content") if last.get("role") == "user" else ""
        raw = raw if isinstance(raw, str) else delegate.task_text(raw)
        cut = raw.rfind(delegate.RUNTIME_CONTEXT_END)
        preamble = raw[:cut + len(delegate.RUNTIME_CONTEXT_END)] if cut >= 0 else ""
        request = delegate.task_text(raw)
        def registry(names: list[str], read_only: bool) -> ToolRegistry:
            # A step names what to call; the shell runs only the runtime's
            # skill calls (ToolRegistry.shell_for_skills_only), so it is
            # registered but never offered.
            reg = ToolRegistry()
            reg.shell_for_skills_only = True
            for name in names + ["exec"]:
                if name in ("plan", "spawn") or (read_only and _changes_things(name)):
                    continue
                if (tool := self.tools.get(name)) is not None:
                    reg.register(tool)
            return reg
        # The slim prompt (ContextBuilder.build_step_system_prompt): what a
        # step needs is the request, the plan and the skills they name -- not
        # the persona, the house rules or memory, which the planner holds.
        channel = session.key.split(":", 1)[0] if session and session.key else None
        # Built per step: the runner exists before the plan has any steps.
        def system() -> list[dict]:
            return [{"role": "system", "content": self.context.build_step_system_prompt(
                request + "\n" + "\n".join(plan.steps), channel=channel)}]
        # Who runs a step: the plan-steps model when one is set, else the
        # sub-agent's when `plan_executor` says so. A step it cannot finish
        # runs again on the planner's model (`fallback`, the turn's own).
        back_runner, back_model = fallback or (self.runner, self.model)
        if self._plan_step_provider is not None and self._plan_step_model:
            step_provider, step_model = self._plan_step_provider, self._plan_step_model
        else:
            step_provider, step_model = self.subagents.provider, self.subagents.model
        local = ((self._plan_step_provider is not None and bool(self._plan_step_model))
                 or self._routing.plan_executor == "subagent") \
            and (step_model != back_model or step_provider is not back_runner.provider)

        async def run_step(k: int) -> dict:
            earlier = "\n\n".join(f"Step {i} ({plan.steps[i - 1]}) found:\n{plan.results[i]}"
                                   for i in sorted(plan.results) if i != k)
            prompt = (f"{preamble}\n\nYou are carrying out ONE step of a plan for this request:\n"
                      f"{request}\n\nThe plan:\n{plan.summary()}\n\n"
                      + (f"What earlier steps found:\n{earlier}\n\n" if earlier else "")
                      + f"Do step {k} now, and only step {k}: {plan.steps[k - 1]}\n"
                      f"Use the tools and skills you need. When it is done, answer with what "
                      f"you found or did -- facts and names, briefly. Do not do other steps.\n"
                      # A step with no tool call once matched the lights to
                      # Home Assistant by guessing: four WiZ bulbs "were Zigbee
                      # repeaters", and the answer called that tested
                      # (2026-09-24). And a skill that wants the person's yes
                      # first (find_in_ha switches lamps) cannot get it here.
                      # One call per reply made a step over eight lights cost
                      # nine rounds of the whole prompt (2026-09-24); the
                      # runner runs a reply's calls together.
                      f"When the same action applies to several items, make all those calls "
                      f"in one reply rather than one reply each.\n"
                      f"Every fact in your answer must come from a tool result, in this step "
                      f"or an earlier one. Never fill a gap with a likely guess: say what could "
                      f"not be found. If a skill asks for the person's confirmation before "
                      f"acting, do not act and do not ask: end the step saying what needs "
                      f"their yes and what it would do.")
            read_only = k not in plan.acts
            # Said before the step starts, not only when a call is refused:
            # told "Call skill lights, action turn_on" and nothing else, every
            # local model called it, was stopped, and reported the lamp on
            # (steps bench, 2026-09-25).
            if read_only:
                prompt += ("\nThis step may only READ. Do not call any action that changes "
                           "something (turn on or off, set, add, send, schedule, rename, delete), "
                           "even if the step names one: it would be refused. Instead, end the step "
                           "saying which change it asks for and that it needs the person's go-ahead.")
            msgs = system() + [{"role": "user", "content": prompt}]
            # The step model gets the tools this step names (tools_for_step);
            # the planner's model, running a step the step model could not
            # finish, gets all of them -- it is the one that wrote the plan.
            everything = list(self.tools.tool_names)
            narrow = registry(tools_for_step(plan.steps[k - 1], everything), read_only)
            wide = registry(everything, read_only)
            refused: list[str] = []

            async def attempt(runner: AgentRunner, model: str, messages: list[dict],
                              effort: str | None = self._default_effort,
                              tools: ToolRegistry = wide):
                return await runner.run(AgentRunSpec(
                    initial_messages=messages, tools=tools,
                    model=model, read_only=read_only, refused=refused,
                    max_iterations=self._routing.plan_step_calls,
                    max_tool_result_chars=self.max_tool_result_chars,
                    concurrent_tools=True, workspace=self.workspace,
                    session_key=session.key if session else None,
                    context_window_tokens=self.context_window_tokens,
                    context_block_limit=self.context_block_limit,
                    provider_retry_mode=self.provider_retry_mode,
                    reasoning_effort=effort))

            by, res = back_model, None
            if local:
                by = step_model
                try:
                    res = await attempt(AgentRunner(step_provider), step_model, msgs,
                                        effort=self._routing.plan_step_effort, tools=narrow)
                except Exception as e:                      # noqa: BLE001 -- fall back below
                    logger.warning("Plan step {} on {} failed: {}", k, step_model, e)
                    res = None
                if res is None or res.stop_reason in set(self._routing.escalate_on) \
                        or not (res.final_content or "").strip():
                    done = delegate.already_done(res.messages) if res is not None else None
                    logger.info("Plan step {} ended with {} on {}; again on {}", k,
                                res.stop_reason if res else "an error", step_model, back_model)
                    msgs = msgs + ([{"role": "user", "content": done}] if done else [])
                    by, res = f"{back_model} (after {step_model})", None
            if res is None:
                res = await attempt(back_runner, back_model, msgs)
                by = by if by.startswith(back_model) else back_model
            report_usage(session.key if session else None, by.split(" ")[0], res.usage,
                         res.tools_used or [], route={"tier": "plan-step", "label": f"step {k}",
                                                      "source": "plan", "classifier_ms": 0,
                                                      "escalated": by.startswith(back_model) and local,
                                                      "escalated_from": ""})
            text = res.final_content or ""
            if refused:
                # Said to the planner, not left to the step's own words: a
                # step that was stopped still tends to report the change as
                # done.
                text += (f"\n\n(Not done: this step only reads, and it tried to change "
                         f"something -- {', '.join(dict.fromkeys(refused))}. If the person "
                         f"asked for that, set the plan again with step {k} in acts; if not, "
                         f"ask them.)")
            return {"text": text, "by": by, "tools": list(res.tools_used or [])}

        return run_step

    async def _delegate_turn(self, initial_messages: list[dict], session: "Session | None",
                             channel: str, chat_id: str, kind: str, route: TurnClass,
                             done: str | None = None,
                             lead: str | None = None) -> tuple[str | None, list[str], list[dict], str, bool]:
        """Hand this turn to a sub-agent and answer with one line.

        The task is what the person wrote; the context is what was being said
        (and, after a dead end, the calls already made). The same request while
        its task is still running is not started twice.
        """
        last = initial_messages[-1] if initial_messages else {}
        text = delegate.task_text(last.get("content")) if last.get("role") == "user" else ""
        key = session.key if session else f"{channel}:{chat_id}"
        if kind == "long" and text and self.subagents.is_running_task(key, text):
            reply = delegate.ack("already")
        else:
            context = "\n\n".join(p for p in (delegate.conversation_context(session), done) if p)
            await self.subagents.spawn(task=text, context=context or None, complex=False,
                                       origin_channel=channel, origin_chat_id=chat_id,
                                       session_key=key)
            reply = delegate.ack(kind)
        if lead and lead.strip():
            reply = f"{lead.strip()}\n\n{reply}"
        route.tier = "subagent"
        logger.info("Turn delegated to a sub-agent ({}, {}) for {}", kind, route.label, key)
        return reply, [], list(initial_messages) + [{"role": "assistant", "content": reply}], \
            "delegated", False

    async def _detach_plan(self, run_task: "asyncio.Future", plan: "plan_tool.TurnPlan",
                           initial_messages: list[dict], session: "Session | None",
                           channel: str, chat_id: str,
                           route: TurnClass) -> tuple[str | None, list[str], list[dict], str, bool]:
        """Let a plan that outgrew the chat turn finish in the background.

        Before, the API server's request timeout cut the turn mid-step and
        started the request over on a sub-agent: every step done was lost, and
        the sub-agent (pi, on the house's local model) was the weakest runner
        in the house -- on 2026-09-24 it came back with 0 turns in 900 s. Now
        the same run goes on -- the planner, the step model, the steps done --
        and its answer is written into the conversation when it is ready.
        """
        plan.limit_s = float("inf")          # nothing left to hand off to
        await plan_tool.say(plan, "handoff")
        key = session.key if session else f"{channel}:{chat_id}"

        async def finish() -> None:
            try:
                result = await run_task
                text = (result.final_content or "").strip()
            except Exception as e:                               # noqa: BLE001
                logger.warning("Detached plan for {} failed: {}", key, e)
                text = ""
            if not text:
                text = delegate.ack("failed")
            try:
                target = session or self.sessions.get_or_create(key)
                target.add_message("assistant", text, reasoning_content="")
                self.sessions.save(target)
            except Exception:                                    # noqa: BLE001
                logger.exception("Detached plan for {}: could not save the answer", key)
            await self.bus.publish_outbound(OutboundMessage(channel=channel, chat_id=chat_id,
                                                            content=text))
            logger.info("Detached plan for {} delivered ({} chars)", key, len(text))

        task = asyncio.ensure_future(finish())
        self._detached_plans.add(task)
        task.add_done_callback(self._detached_plans.discard)
        route.tier = "plan-background"
        reply = delegate.ack("continue")
        logger.info("Plan for {} detached after {}s: {}/{} steps done", key,
                    self._routing.plan_detach_seconds, len(plan.done), len(plan.steps))
        return reply, [], list(initial_messages) + [{"role": "assistant", "content": reply}], \
            "delegated", False

    def _should_escalate(self, result: "AgentRunResult", route: TurnClass, runner: AgentRunner) -> bool:
        """Only a cheap attempt that ended badly, only in active mode, only once."""
        return (
            self._routing.mode == "active"
            and self._routing.max_escalations_per_turn > 0
            and runner is self.runner
            and self._powerful_runner is not self.runner
            and route.tier == "everyday"
            and route.escalated_from is None
            and result.stop_reason in set(self._routing.escalate_on)
        )

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
        *,
        session: Session | None = None,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
        pending_queue: asyncio.Queue | None = None,
        powerful: bool = False,
        profile: str | None = None,
    ) -> tuple[str | None, list[str], list[dict], str, bool]:
        """Run the agent iteration loop.

        *on_stream*: called with each content delta during streaming.
        *on_stream_end(resuming)*: called when a streaming session finishes.
        ``resuming=True`` means tool calls follow (spinner should restart);
        ``resuming=False`` means this is the final response.

        Returns (final_content, tools_used, messages, stop_reason, had_injections).
        """
        loop_hook = _LoopHook(
            self,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
        )
        hook: AgentHook = (
            CompositeHook([loop_hook] + self._extra_hooks) if self._extra_hooks else loop_hook
        )

        # Filled in below, once the routing has picked this turn's model. Read
        # from inside _drain_pending, which cannot run before then.
        turn_route: tuple[str | None, str | None] = (None, None)

        async def _checkpoint(payload: dict[str, Any]) -> None:
            if session is None:
                return
            self._set_runtime_checkpoint(session, payload)

        async def _drain_pending(*, limit: int = _MAX_INJECTIONS_PER_TURN) -> list[dict[str, Any]]:
            """Drain follow-up messages from the pending queue.

            When no messages are immediately available but sub-agents
            spawned in this dispatch are still running, blocks until at
            least one result arrives (or timeout).  This keeps the runner
            loop alive so subsequent sub-agent completions are consumed
            in-order rather than dispatched separately.
            """
            if pending_queue is None:
                return []

            def _to_user_message(pending_msg: InboundMessage) -> dict[str, Any]:
                content = pending_msg.content
                media = pending_msg.media if pending_msg.media else None
                if media:
                    content, media = extract_documents(content, media)
                    media = media or None
                user_content = self.context._build_user_content(content, media)
                runtime_ctx = self.context._build_runtime_context(
                    pending_msg.channel,
                    pending_msg.chat_id,
                    self.context.timezone,
                )
                if isinstance(user_content, str):
                    merged: str | list[dict[str, Any]] = f"{runtime_ctx}\n\n{user_content}"
                else:
                    merged = [{"type": "text", "text": runtime_ctx}] + user_content
                # This block is built fresh and lands *after* the turn's own, so
                # it is the last one the model reads. Somebody asking "¿qué
                # modelo sos?" while a turn is already running is drained into
                # it, and an unlabelled block right under the question is the
                # one place the prompt's "read the Model line" finds nothing.
                # Same turn, same route, so the same label — `turn_route` is
                # assigned by the time anything can be drained. The one-element
                # list is how the annotator hands the message back: it replaces
                # entries rather than mutating the dicts it was given.
                one = [{"role": "user", "content": merged}]
                if turn_route[0]:
                    self.context.annotate_runtime_model(one, *turn_route)
                return one[0]

            items: list[dict[str, Any]] = []
            while len(items) < limit:
                try:
                    items.append(_to_user_message(pending_queue.get_nowait()))
                except asyncio.QueueEmpty:
                    break

            # Block if nothing drained but sub-agents spawned in this dispatch
            # are still running.  Keeps the runner loop alive so subsequent
            # completions are injected in-order rather than dispatched separately.
            if (not items
                    and session is not None
                    and self.subagents.get_running_count_by_session(session.key) > 0):
                try:
                    msg = await asyncio.wait_for(pending_queue.get(), timeout=300)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Timeout waiting for sub-agent completion in session {}",
                        session.key,
                    )
                    return items
                items.append(_to_user_message(msg))
                while len(items) < limit:
                    try:
                        items.append(_to_user_message(pending_queue.get_nowait()))
                    except asyncio.QueueEmpty:
                        break

            return items

        # An image turn prefers the main model plus describe_image over moving the
        # whole conversation onto the vision model. The vision model is small and
        # local; handing it the full system prompt and every tool definition is
        # what made image turns fail, while the same model answers accurately
        # when asked one focused question about one picture.
        #
        # Falling back to the old routing when describe_image is unavailable, or
        # when an image arrived without a path to point it at.
        use_vision = False
        if self._vision_runner is not None:
            has_image = _messages_have_image(initial_messages)
            if self.tools.has("describe_image"):
                if has_image and _swap_images_for_paths(initial_messages):
                    use_vision = False
                elif has_image:
                    use_vision = True
                # A text-only turn referring back to an earlier picture needs no
                # special handling here: history already carries the
                # `[image: …]` placeholder, which describe_image reads directly.
            elif has_image or _reattach_referenced_image(initial_messages):
                use_vision = True
        # Vision wins over the powerful route when both apply: the image is the
        # thing that cannot be answered by the other model at all, while
        # "powerful" is only a preference for a better one.
        # `turn_effort` of None means "say nothing about thinking and let the
        # provider's own setting stand" -- which is what every route did before
        # any of these were configurable.
        turn_effort = None
        # Which tier this turn *starts* on -- the caller's flags, a recent
        # escalation in this session, or the classifier's label, in that
        # order. `forced` and `sticky` never ask the model. See classify.py.
        route = await self._route_turn(
            initial_messages, session=session, chat_id=chat_id,
            powerful=powerful, profile=profile, use_vision=use_vision,
        )
        # Work that takes minutes goes to a sub-agent, and this turn only says
        # so (delegate.py). Not an image turn, a profession or a caller's
        # `powerful`, and not a channel where a later answer is no answer --
        # those run here, on the everyday model.
        if (route.tier == "subagent" and self._routing.mode == "active"
                and not (use_vision or profile or powerful)
                and delegate.may_delegate(channel, chat_id, session.key if session else None)):
            return await self._delegate_turn(initial_messages, session, channel, chat_id,
                                             "long", route)
        if use_vision:
            runner, turn_model = self._vision_runner, self._vision_model
        elif profile and profile in self._profiles:
            runner, turn_model = self._profiles[profile]
            turn_effort = self._profile_efforts.get(profile)
        elif route.label == "long" and self._plan_runner is not self.runner:
            # Work of several steps is planned by the planner's model.
            runner, turn_model = self._plan_runner, self.plan_model
            turn_effort = self._default_effort
        elif powerful or is_space_session(chat_id) or route.tier == "powerful":
            # A turn inside one of HomeCore's Profesiones is never the fast
            # model. Interactive turns already say so — HomeCore sends both
            # `profile` and `powerful` — but a turn nanobot starts for itself
            # does not: a reminder set while talking to the Profesor fires from
            # cron with no profile at all, and answered on the quick model in
            # the Profesor's own chat it is visibly a different assistant.
            #
            # Read off the session id rather than a list of professions, which
            # lives in HomeCore and is deliberately not tracked here (see
            # `homeweb_chat_id`). That is enough to say "not the fast one",
            # which is the property being protected; which *particular* model a
            # profession prefers is still HomeCore's to name.
            runner, turn_model = self._powerful_runner, self.powerful_model
            turn_effort = self._powerful_effort
        else:
            runner, turn_model = self.runner, self.model
            # Ordinary chat and background subagent work. Before this existed
            # the branch set no effort at all, so the two highest-volume
            # interactive turns in the house were the only ones that could not
            # turn thinking down -- see `reasoning_effort_default`.
            turn_effort = self._default_effort
        # Which model is answering is a fact about this turn, so it goes in the
        # turn's own Runtime Context rather than in the prompt roster, which is
        # the configuration and is wrong the moment any of the routing above
        # picks something else. `serving_model` is asked rather than assumed:
        # while a model is inside its outage window every call for it is
        # answered by the fallback, and the agent should say so.
        #
        # The one case this cannot cover is the fallback firing *during* this
        # turn: the block is already sent by then, and the answer being written
        # is the fallback's own. That turn is labelled with the model that was
        # asked for; the window it opens makes every turn after it correct.
        serving, displaced = runner.provider.serving_model(turn_model)
        turn_route = (serving, displaced)
        loop_hook.turn_model = serving
        loop_hook.turn_effort = turn_effort
        self.context.annotate_runtime_model(initial_messages, serving, displaced)
        # Everything the turn spends from here is attributed to this span:
        # the LLM calls the provider makes, the tools the runner runs, and
        # whatever wall time is left over, which is ours. See utils/profiling.
        #
        # Measured before the span opens, and only when there is somewhere to
        # put it: every other collection point in the profiler returns early
        # under `enabled`, but `**_prompt_shape(...)` inside `note()` would be
        # evaluated whatever the switch says — and a disabled span discards it.
        # Taking it here also keeps its own cost out of `other_ms`, the number
        # the panel presents as ours to fix.
        # A plan for this turn (tools/plan.py). Every turn gets a fresh one, so
        # a plan never leaks into the next; a `long` turn is told to use it.
        # The time limit applies only where the steps left can go on in the
        # background -- elsewhere (voice) a plan just runs to its end.
        can_hand_off = delegate.may_delegate(channel, chat_id, session.key if session else None)
        turn_plan = plan_tool.TurnPlan(
            limit_s=float(self._routing.plan_chat_seconds) if can_hand_off else float("inf"),
            per_step=self._routing.plan_step_calls,
            max_budget=max(self.max_iterations, self._routing.complex_iterations),
            progress=(lambda state: invoke_on_progress(on_progress, "", plan=state)) if on_progress else None,
        )
        plain_turn = runner is self.runner or runner is self._plan_runner
        if plain_turn:
            turn_plan.executor = self._step_runner(turn_plan, initial_messages, session,
                                                   fallback=(runner, turn_model))
        plan_tool.begin(turn_plan)
        if route.label == "long" and plain_turn:
            self.context.annotate_runtime_line(
                initial_messages,
                "Mode: this request takes several steps. First call the plan tool with "
                "action=set and 3-8 steps, then call it with action=run for each "
                "step in order and read the result before the next. If a result calls "
                "for it, change the plan; if a step needs the person's decision, ask. "
                # A smaller model runs each step and sees only that step: it
                # follows an instruction well and works one out badly (Bonsai
                # 2 27B, 2026-09-24: HA called six times where chores.list was
                # meant). So the planner, which reads the whole request, says
                # exactly what to call.
                "Each step is run by a smaller model that sees only that step, so write "
                "it as an exact instruction: the tool or skill action to call, with its "
                "arguments (names, ids, values from earlier steps written out), and what "
                "to report back. One action per step, or one action repeated over a list "
                "you spell out. Example: 'Call skill lights, action flash_light, for each "
                "of: Oficina Tomi, Luz Paula. Report which calls succeeded.'")
        prompt_shape = _prompt_shape(initial_messages, self.tools) if PROFILER.enabled else {}
        with PROFILER.span(
            "turn",
            session.key if session else f"{channel}:{chat_id}",
            label=profile or ("powerful" if powerful else ""),
            channel=channel,
            chat_id=chat_id,
            model=serving,
        ) as span:
            # A turn the classifier sent to the cheap tier gets the cheap
            # tier's budget; running out of it is `max_iterations`, which
            # escalates below. Forced and profile turns keep the configured one.
            turn_budget = self.max_iterations
            if (self._routing.mode == "active" and plain_turn
                    and route.tier == "everyday" and route.source in ("model", "default", "fast_path")):
                turn_budget = min(self.max_iterations, self._routing.everyday_iterations)
            spec = AgentRunSpec(
                initial_messages=initial_messages,
                tools=self.tools,
                model=turn_model,
                max_iterations=turn_budget,
                max_tool_result_chars=self.max_tool_result_chars,
                hook=hook,
                error_message="Sorry, I encountered an error calling the AI model.",
                concurrent_tools=True,
                workspace=self.workspace,
                session_key=session.key if session else None,
                context_window_tokens=self.context_window_tokens,
                context_block_limit=self.context_block_limit,
                provider_retry_mode=self.provider_retry_mode,
                reasoning_effort=turn_effort,
                progress_callback=on_progress,
                retry_wait_callback=on_retry_wait,
                checkpoint_callback=_checkpoint,
                injection_callback=_drain_pending,
            )
            turn_plan.spec = spec
            if can_hand_off and plain_turn and route.label == "long":
                # Its own task, so a plan that outgrows the chat can be left
                # to finish (see _detach_plan)...
                run_task = asyncio.ensure_future(runner.run(spec))
                done_, _ = await asyncio.wait({run_task}, timeout=self._routing.plan_detach_seconds)
                if not done_ and turn_plan.steps:
                    return await self._detach_plan(run_task, turn_plan, initial_messages, session,
                                                   channel, chat_id, route)
                result = await run_task
                # ...and what it set in its context comes back to this one, as
                # though it had run here: the message tool's "already sent"
                # lives in a ContextVar, and losing it delivered answers twice.
                for var, value in run_task.get_context().items():
                    var.set(value)
            else:
                result = await runner.run(spec)
            span.note(
                stop_reason=result.stop_reason,
                served_by=result.served_by_model,
                had_injections=result.had_injections,
                displaced=displaced,
                route=route.label,
                route_source=route.source,
                **prompt_shape,
            )
            # The cheap attempt did not finish -- a skill block that resolved
            # to nothing, a loop, an empty answer, a budget spent. The strong
            # model continues the same turn from where it stalled, once. What
            # the classifier guessed wrong before the turn, this catches after
            # it; the two together are the routing. See classify.py.
            # A cheap attempt that dead-ended goes to a sub-agent where the
            # conversation allows one -- with what it already did, so nothing
            # is done twice. Elsewhere (voice, a system turn) the stronger
            # model continues it, as before.
            plan_note = (f"The plan so far (✓ done, · still to do):\n{turn_plan.summary()}"
                         if turn_plan.steps else None)
            if turn_plan.handoff and turn_plan.remaining() and can_hand_off:
                # Past the time limit mid-plan: the model has said what is done;
                # the steps left continue in the background.
                report_usage(session.key if session else None,
                             result.served_by_model or serving, result.usage,
                             result.tools_used or [], route=route.as_record())
                await plan_tool.say(turn_plan, "handoff")
                return await self._delegate_turn(
                    initial_messages, session, channel, chat_id, "continue", route,
                    done="\n\n".join(x for x in (plan_note, delegate.already_done(result.messages)) if x),
                    lead=result.final_content)
            if (self._should_escalate_any(result, route, runner) and can_hand_off):
                route.escalated_from = result.stop_reason
                report_usage(session.key if session else None,
                             result.served_by_model or serving, result.usage,
                             result.tools_used or [], route=route.as_record())
                return await self._delegate_turn(
                    initial_messages, session, channel, chat_id, "dead_end", route,
                    done="\n\n".join(x for x in (plan_note, delegate.already_done(result.messages)) if x))
            if self._should_escalate(result, route, runner):
                first_attempt = result
                reason = result.stop_reason
                route.escalated_from = reason
                logger.info("Turn ended with {} on {}; continuing on {}",
                            reason, serving, self.powerful_model)
                report_usage(session.key if session else None,
                             first_attempt.served_by_model or serving,
                             first_attempt.usage, first_attempt.tools_used or [],
                             route=route.as_record())
                serving, displaced = self._powerful_runner.provider.serving_model(self.powerful_model)
                loop_hook.turn_model = serving
                loop_hook.turn_effort = self._powerful_effort
                cont = continuation_messages(
                    first_attempt.messages, reason,
                    tools_ran=bool(first_attempt.tools_used),
                )
                self.context.annotate_runtime_model(cont, serving, displaced)
                result = await self._powerful_runner.run(AgentRunSpec(
                    initial_messages=cont,
                    tools=self.tools,
                    model=self.powerful_model,
                    max_iterations=max(self.max_iterations, self._routing.complex_iterations),
                    max_tool_result_chars=self.max_tool_result_chars,
                    hook=hook,
                    error_message="Sorry, I encountered an error calling the AI model.",
                    concurrent_tools=True,
                    workspace=self.workspace,
                    session_key=session.key if session else None,
                    context_window_tokens=self.context_window_tokens,
                    context_block_limit=self.context_block_limit,
                    provider_retry_mode=self.provider_retry_mode,
                    reasoning_effort=self._powerful_effort,
                    progress_callback=on_progress,
                    retry_wait_callback=on_retry_wait,
                    checkpoint_callback=_checkpoint,
                    injection_callback=_drain_pending,
                ))
                route = TurnClass(route.label, "powerful", route.reason, "escalation",
                                  route.ms, escalated_from=reason)
                span.note(escalated_from=reason, stop_reason=result.stop_reason,
                          served_by=result.served_by_model)
                if session is not None and self._routing.sticky_turns > 0:
                    session.metadata["sticky_powerful"] = self._routing.sticky_turns
        self._last_usage = result.usage
        # What this turn cost, filed where the family can look at it later. The
        # session key carries which kind of turn it was; HomeCore reads it.
        #
        # What answered, in order of how much it knows: `served_by_model` is
        # reported by the provider after the fact and is the only thing that
        # sees a fallback firing mid-turn; `serving` is the prediction the
        # prompt was labelled with, right for the whole outage window after the
        # first turn; `turn_model` is merely what was asked for and is the one
        # thing this must not be, or an outage hour bills a dead model at its
        # own per-token price and shows the one doing the work at zero.
        billed = result.served_by_model or serving
        if result.served_by_model and result.served_by_model != serving:
            # The turn the label cannot catch: the model died while this turn
            # was already running, so the prompt named the model that was asked
            # for. The window this opened makes every turn after it correct;
            # this line is how the one in between is not silent.
            logger.info(
                "Turn was labelled {} but answered by {} (fallback fired mid-turn)",
                serving, result.served_by_model,
            )
        report_usage(session.key if session else None, billed,
                     result.usage, result.tools_used or [], route=route.as_record())
        if result.stop_reason == "max_iterations":
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            # Push final content through stream so streaming channels (e.g. Feishu)
            # update the card instead of leaving it empty.
            if on_stream and on_stream_end:
                await on_stream(result.final_content or "")
                await on_stream_end(resuming=False)
        elif result.stop_reason == "error":
            logger.error("LLM returned error: {}", (result.final_content or "")[:200])
        return result.final_content, result.tools_used, result.messages, result.stop_reason, result.had_injections

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        await self.warm_up()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                self.auto_compact.check_expired(
                    self._schedule_background,
                    active_session_keys=self._pending_queues.keys(),
                )
                continue
            except asyncio.CancelledError:
                # Preserve real task cancellation so shutdown can complete cleanly.
                # Only ignore non-task CancelledError signals that may leak from integrations.
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            # Standing context off first, same as in _process_message: a
            # command has to be recognisable under whatever a caller wrapped
            # around it.
            raw = standing_context.for_history(msg.content).strip() \
                if isinstance(msg.content, str) else ""
            if self.commands.is_priority(raw):
                await self._dispatch_command_inline(
                    msg, msg.session_key, raw,
                    self.commands.dispatch_priority,
                )
                continue
            effective_key = self._effective_session_key(msg)
            # If this session already has an active pending queue (i.e. a task
            # is processing this session), route the message there for mid-turn
            # injection instead of creating a competing task.
            if effective_key in self._pending_queues:
                # Non-priority commands must not be queued for injection;
                # dispatch them directly (same pattern as priority commands).
                if self.commands.is_dispatchable_command(raw):
                    await self._dispatch_command_inline(
                        msg, effective_key, raw,
                        self.commands.dispatch,
                    )
                    continue
                pending_msg = msg
                if effective_key != msg.session_key:
                    pending_msg = dataclasses.replace(
                        msg,
                        session_key_override=effective_key,
                    )
                try:
                    self._pending_queues[effective_key].put_nowait(pending_msg)
                except asyncio.QueueFull:
                    logger.warning(
                        "Pending queue full for session {}, falling back to queued task",
                        effective_key,
                    )
                else:
                    logger.info(
                        "Routed follow-up message to pending queue for session {}",
                        effective_key,
                    )
                    continue
            # Compute the effective session key before dispatching
            # This ensures /stop command can find tasks correctly when unified session is enabled
            task = asyncio.create_task(self._dispatch(msg))
            self._active_tasks.setdefault(effective_key, []).append(task)
            task.add_done_callback(
                lambda t, k=effective_key: self._active_tasks.get(k, [])
                and self._active_tasks[k].remove(t)
                if t in self._active_tasks.get(k, [])
                else None
            )

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message: per-session serial, cross-session concurrent."""
        session_key = self._effective_session_key(msg)
        if session_key != msg.session_key:
            msg = dataclasses.replace(msg, session_key_override=session_key)
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        gate = self._concurrency_gate or nullcontext()

        # Register a pending queue so follow-up messages for this session are
        # routed here (mid-turn injection) instead of spawning a new task.
        pending = asyncio.Queue(maxsize=20)
        self._pending_queues[session_key] = pending

        try:
            async with lock, gate:
                try:
                    on_stream = on_stream_end = None
                    if msg.metadata.get("_wants_stream"):
                        # Split one answer into distinct stream segments.
                        stream_base_id = f"{msg.session_key}:{time.time_ns()}"
                        stream_segment = 0

                        def _current_stream_id() -> str:
                            return f"{stream_base_id}:{stream_segment}"

                        async def on_stream(delta: str) -> None:
                            meta = dict(msg.metadata or {})
                            meta["_stream_delta"] = True
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content=delta,
                                metadata=meta,
                            ))

                        async def on_stream_end(
                            *,
                            resuming: bool = False,
                            trim_to: str | None = None,
                        ) -> None:
                            nonlocal stream_segment
                            meta = dict(msg.metadata or {})
                            meta["_stream_end"] = True
                            meta["_resuming"] = resuming
                            if trim_to is not None:
                                meta["_trim_to"] = trim_to
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="",
                                metadata=meta,
                            ))
                            stream_segment += 1

                    _process_coro = self._process_message(
                        msg, on_stream=on_stream, on_stream_end=on_stream_end,
                        pending_queue=pending,
                    )
                    # For sub-agent results, cap summarisation at 20s so a
                    # stalled Together AI call doesn't lose the result entirely.
                    _is_subagent_result = (
                        msg.channel == "system"
                        and msg.sender_id == "subagent"
                        and "subagent_raw_result" in (msg.metadata or {})
                    )
                    if _is_subagent_result:
                        try:
                            response = await asyncio.wait_for(_process_coro, timeout=20.0)
                        except asyncio.TimeoutError:
                            logger.warning(
                                "Subagent summarisation timed out for session {}; "
                                "publishing raw result directly",
                                session_key,
                            )
                            channel_fb, chat_id_fb = (
                                msg.chat_id.split(":", 1)
                                if ":" in msg.chat_id
                                else ("cli", msg.chat_id)
                            )
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=channel_fb,
                                chat_id=chat_id_fb,
                                content=msg.metadata["subagent_raw_result"],
                            ))
                            response = None
                    else:
                        response = await _process_coro
                    # `_already_sent` means MessageTool put this on the bus
                    # itself; the response object is the copy process_direct
                    # callers need, not a second delivery.
                    if response is not None and not (response.metadata or {}).get("_already_sent"):
                        await self.bus.publish_outbound(response)
                    elif msg.channel == "cli":
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content="", metadata=msg.metadata or {},
                        ))
                except asyncio.CancelledError:
                    logger.info("Task cancelled for session {}", session_key)
                    # Preserve partial context from the interrupted turn so
                    # the user does not lose tool results and assistant
                    # messages accumulated before /stop.  The checkpoint was
                    # already persisted to session metadata by
                    # _emit_checkpoint during tool execution; materializing
                    # it into session history now makes it visible in the
                    # next conversation turn.
                    try:
                        key = self._effective_session_key(msg)
                        session = self.sessions.get_or_create(key)
                        if self._restore_runtime_checkpoint(session):
                            self._clear_pending_user_turn(session)
                            self.sessions.save(session)
                            logger.info(
                                "Restored partial context for cancelled session {}",
                                key,
                            )
                    except Exception:
                        logger.debug(
                            "Could not restore checkpoint for cancelled session {}",
                            session_key,
                            exc_info=True,
                        )
                    raise
                except Exception as _loop_exc:
                    logger.exception("Error processing message for session {}", session_key)
                    _log_error("loop.process_message", _loop_exc, session_key=session_key)
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content=f"Sorry, I encountered an error: {_loop_exc!r}",
                    ))
        finally:
            # Drain any messages still in the pending queue and re-publish
            # them to the bus so they are processed as fresh inbound messages
            # rather than silently lost.
            queue = self._pending_queues.pop(session_key, None)
            if queue is not None:
                leftover = 0
                while True:
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await self.bus.publish_inbound(item)
                    leftover += 1
                if leftover:
                    logger.info(
                        "Re-published {} leftover message(s) to bus for session {}",
                        leftover, session_key,
                    )

    async def close_mcp(self) -> None:
        """Drain pending background archives, then close MCP connections."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        for name, stack in self._mcp_stacks.items():
            try:
                await stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                logger.debug("MCP server '{}' cleanup error (can be ignored)", name)
        self._mcp_stacks.clear()

    def _schedule_background(self, coro) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._background_tasks.remove)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    # Key under which a session remembers the last standing block it was sent.
    _STANDING_KEY = "standing_context"

    def _remembered_standing(self, session: Session, content: Any) -> str:
        """The standing block for a turn that did not bring one.

        Sessions remember the last block a caller sent them (see
        `_remember_standing`), so a turn nobody prepended one to still runs in
        the right voice. A turn that *does* carry its own gets "" — re-attaching
        would duplicate it.

        The remembered copy can be one deploy stale, because it is refreshed by
        every ordinary turn: at worst one background answer is phrased with
        yesterday's wording. Sessions are per-day for a space, so it also clears
        itself without anything having to expire it.
        """
        if isinstance(content, str) and standing_context.extract(content):
            return ""
        # Stored wrapped so the session file says what it is; stripped here,
        # because the markers are plumbing and must never reach the model.
        return standing_context.for_prompt(session.metadata.get(self._STANDING_KEY, ""))

    def _remember_standing(self, session: Session, content: Any) -> bool:
        """Record this turn's standing block on the session. True if it changed."""
        if not isinstance(content, str):
            return False
        block = standing_context.extract(content)
        if not block or session.metadata.get(self._STANDING_KEY) == block:
            return False
        session.metadata[self._STANDING_KEY] = block
        return True

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
        powerful: bool = False,
        profile: str | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (
                msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
            )
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            if self._restore_runtime_checkpoint(session):
                self.sessions.save(session)
            if self._restore_pending_user_turn(session):
                self.sessions.save(session)

            session, pending = self.auto_compact.prepare_session(session, key)

            await self.consolidator.maybe_consolidate_by_tokens(
                session,
                session_summary=pending,
            )
            # Persist subagent follow-ups into durable history BEFORE prompt
            # assembly. ContextBuilder merges adjacent same-role messages for
            # provider compatibility, which previously caused the follow-up to
            # disappear from session.messages while still being visible to the
            # LLM via the merged prompt. See _persist_subagent_followup.
            is_subagent = msg.sender_id == "subagent"
            if is_subagent and self._persist_subagent_followup(session, msg):
                self.sessions.save(session)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            history = session.get_history(max_messages=0)
            current_role = "assistant" if is_subagent else "user"

            # Subagent content is already in `history` above; passing it again
            # as current_message would double-project it into the prompt.
            messages = self.context.build_messages(
                history=history,
                current_message="" if is_subagent
                else standing_context.for_prompt(msg.content),
                channel=channel,
                chat_id=chat_id,
                session_summary=pending,
                current_role=current_role,
                session_key=session.key,
                # This turn was started by something other than the person — a
                # background task announcing its result, a job firing — so it
                # carries no standing block of its own, and since the block is
                # no longer stored it can no longer inherit one from history.
                # Without this, a task started in a profession came back phrased
                # by the ordinary Alfred.
                standing_block=self._remembered_standing(session, msg.content),
            )
            final_content, _, all_msgs, _, _ = await self._run_agent_loop(
                messages, session=session, channel=channel, chat_id=chat_id,
                message_id=msg.metadata.get("message_id"),
                pending_queue=pending_queue,
            )
            self._save_turn(session, all_msgs, 1 + len(history))
            self._clear_runtime_checkpoint(session)
            self.sessions.save(session)
            self._schedule_background(self.consolidator.maybe_consolidate_by_tokens(session))
            return OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=final_content or "Background task completed.",
            )

        # Extract document text from media at the processing boundary so all
        # channels benefit without format-specific logic in ContextBuilder.
        if msg.media:
            new_content, image_only = extract_documents(msg.content, msg.media)
            msg = dataclasses.replace(msg, content=new_content, media=image_only)

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)
        if self._restore_runtime_checkpoint(session):
            self.sessions.save(session)
        if self._restore_pending_user_turn(session):
            self.sessions.save(session)

        session, pending = self.auto_compact.prepare_session(session, key)

        # Slash commands. Dispatched on what the *person* wrote: a caller that
        # prepends standing context (HomeCore does, on every turn) would
        # otherwise make `/new` arrive as "[[[standing-context]]]…\n\n/new",
        # match nothing, and be answered by the model as prose — silently
        # removing the one way out of a session that has gone bad.
        raw = standing_context.for_history(msg.content).strip() \
            if isinstance(msg.content, str) else ""
        ctx = CommandContext(msg=msg, session=session, key=key, raw=raw, loop=self)
        if result := await self.commands.dispatch(ctx):
            return result

        await self.consolidator.maybe_consolidate_by_tokens(
            session,
            session_summary=pending,
        )

        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=0)

        # Standing context (a caller's per-turn instruction — see
        # utils/standing_context.py) reaches the model in full on every turn and
        # is stored on none of them. Without the split, a persona re-sent for
        # thirty turns is thirty copies in the session, not one.
        #
        # The session keeps the newest one so the turns nobody prepends a block
        # to — a background task announcing its result — can still be answered
        # in the same voice. Saved with the turn below; a crash before that
        # costs one refresh, not the block.
        self._remember_standing(session, msg.content)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=standing_context.for_prompt(msg.content)
            if isinstance(msg.content, str) else msg.content,
            session_summary=pending,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
            session_key=session.key,
        )

        async def _bus_progress(
            content: str,
            *,
            tool_hint: bool = False,
            tool_events: list[dict[str, Any]] | None = None,
        ) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            if tool_events:
                meta["_tool_events"] = tool_events
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        async def _on_retry_wait(content: str) -> None:
            meta = dict(msg.metadata or {})
            meta["_retry_wait"] = True
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        # Persist the triggering user message up front so a mid-turn crash
        # doesn't silently lose the prompt on recovery. ``media`` rides along
        # as raw on-disk paths — sanitized image blocks are stripped from
        # JSONL, and webui replay needs the paths to mint signed URLs.
        user_persisted_early = False
        media_paths = [p for p in (msg.media or []) if isinstance(p, str) and p]
        stored_text = (standing_context.for_history(msg.content)
                       if isinstance(msg.content, str) else "")
        # `has_text` still asks about what the *user* said, not about the block
        # wrapped around it: a turn whose only content was standing context has
        # nothing to remember, and must not be persisted as an empty user turn.
        has_text = bool(stored_text.strip())
        if has_text or media_paths:
            extra: dict[str, Any] = {"media": list(media_paths)} if media_paths else {}
            session.add_message("user", stored_text, **extra)
            self._mark_pending_user_turn(session)
            self.sessions.save(session)
            user_persisted_early = True

        final_content, _, all_msgs, stop_reason, had_injections = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress or _bus_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            on_retry_wait=_on_retry_wait,
            session=session,
            channel=msg.channel,
            chat_id=msg.chat_id,
            message_id=msg.metadata.get("message_id"),
            pending_queue=pending_queue,
            powerful=powerful,
            profile=profile,
        )

        if final_content is None or not final_content.strip():
            final_content = EMPTY_FINAL_RESPONSE_MESSAGE

        # Skip the already-persisted user message when saving the turn
        save_skip = 1 + len(history) + (1 if user_persisted_early else 0)
        self._save_turn(session, all_msgs, save_skip)
        self._clear_pending_user_turn(session)
        self._clear_runtime_checkpoint(session)
        self.sessions.save(session)
        self._schedule_background(self.consolidator.maybe_consolidate_by_tokens(session))

        # When follow-up messages were injected mid-turn, a later natural
        # language reply may address those follow-ups and should not be
        # suppressed just because MessageTool was used earlier in the turn.
        # However, if the turn falls back to the empty-final-response
        # placeholder, suppress it when the real user-visible output already
        # came from MessageTool.
        mt = self.tools.get("message")
        used_message_tool = isinstance(mt, MessageTool) and mt._sent_in_turn
        sent_via_message_tool = mt.last_sent_content if used_message_tool else None

        # Deliver anything the reply promised and then dropped. Runs before the
        # MessageTool early-return below, because that return was skipping it
        # entirely: the model answers a camera request through MessageTool with
        # the skill's own sentence minus its link — "Front, ahora mismo:" and
        # nothing after the colon — and the file never reached the chat.
        #
        # What the reader actually saw is what counts, so the check looks at the
        # MessageTool text when there is one and the final reply otherwise.
        # It goes out as its own message rather than being appended: a streaming
        # turn is already finalized on the client from _trim_to, and the manager
        # skips re-sending a _streamed message, so text added to the reply would
        # reach the history and never the screen.
        # 1 system message + the history prefix; the same boundary _save_turn
        # uses to know which messages this turn actually produced.
        for link in _undelivered_download_links(
            all_msgs, sent_via_message_tool or final_content,
            turn_start=1 + len(history),
        ):
            logger.info("Delivering file the reply left out: {}", link)
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=link,
                metadata={},
            ))

        if used_message_tool:
            if not had_injections or stop_reason == "empty_final_response":
                # For direct/API callers that can't read from the bus, surface
                # the last content the MessageTool sent so they get a response.
                if sent_via_message_tool:
                    echo_meta = dict(msg.metadata or {})
                    # MessageTool's own send_callback IS bus.publish_outbound,
                    # so this content is already on its way to the channel. The
                    # return value exists only for process_direct callers, who
                    # never read the bus. Without this marker the run loop
                    # publishes it a second time and the reader sees the same
                    # message twice — measured: "Patio, ahora mismo:" delivered
                    # twice for one camera request.
                    echo_meta["_already_sent"] = True
                    return OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=sent_via_message_tool,
                        metadata=echo_meta,
                    )
                return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        meta = dict(msg.metadata or {})
        if on_stream is not None and stop_reason != "error":
            meta["_streamed"] = True
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=meta,
        )

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        should_truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """Strip volatile multimodal payloads before writing session history."""
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                filtered.append(block)
                continue

            if (
                drop_runtime
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            ):
                continue

            if block.get("type") == "image_url" and block.get("image_url", {}).get(
                "url", ""
            ).startswith("data:image/"):
                path = (block.get("_meta") or {}).get("path", "")
                filtered.append({"type": "text", "text": image_placeholder_text(path)})
                continue

            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if should_truncate_text and len(text) > self.max_tool_result_chars:
                    text = truncate_text_fn(text, self.max_tool_result_chars)
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime

        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool":
                if isinstance(content, str) and len(content) > self.max_tool_result_chars:
                    entry["content"] = truncate_text_fn(content, self.max_tool_result_chars)
                elif isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, should_truncate_text=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    # Strip the entire runtime-context block (including any session summary).
                    # The block is bounded by _RUNTIME_CONTEXT_TAG and _RUNTIME_CONTEXT_END.
                    end_marker = ContextBuilder._RUNTIME_CONTEXT_END
                    end_pos = content.find(end_marker)
                    if end_pos >= 0:
                        after = content[end_pos + len(end_marker):].lstrip("\n")
                        if after:
                            entry["content"] = after
                        else:
                            continue
                    else:
                        # Fallback: no end marker found, strip the tag prefix
                        after_tag = content[len(ContextBuilder._RUNTIME_CONTEXT_TAG):].lstrip("\n")
                        if after_tag.strip():
                            entry["content"] = after_tag
                        else:
                            continue
                if isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, drop_runtime=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    def _persist_subagent_followup(self, session: Session, msg: InboundMessage) -> bool:
        """Persist subagent follow-ups before prompt assembly so history stays durable.

        Returns True if a new entry was appended; False if the follow-up was
        deduped (same ``subagent_task_id`` already in session) or carries no
        content worth persisting.
        """
        if not msg.content:
            return False
        task_id = msg.metadata.get("subagent_task_id") if isinstance(msg.metadata, dict) else None
        if task_id and any(
            m.get("injected_event") == "subagent_result" and m.get("subagent_task_id") == task_id
            for m in session.messages
        ):
            return False
        session.add_message(
            "assistant",
            msg.content,
            sender_id=msg.sender_id,
            injected_event="subagent_result",
            subagent_task_id=task_id,
            # A subagent's result is an assistant message this process wrote,
            # not one the model produced, so it has no thinking behind it —
            # and a thinking model rejects the *next* request outright when the
            # last assistant message carries no `reasoning_content` at all:
            # "The `reasoning_content` in the thinking mode must be passed back
            # to the API" (400). That is a whole turn lost, and what the person
            # sees is the raw provider error where the answer should be. It is
            # the same reason build_assistant_message sets it for tool calls;
            # this path does not go through it.
            reasoning_content="",
        )
        return True

    def _set_runtime_checkpoint(self, session: Session, payload: dict[str, Any]) -> None:
        """Persist the latest in-flight turn state into session metadata."""
        session.metadata[self._RUNTIME_CHECKPOINT_KEY] = payload
        self.sessions.save(session)

    def _mark_pending_user_turn(self, session: Session) -> None:
        session.metadata[self._PENDING_USER_TURN_KEY] = True

    def _clear_pending_user_turn(self, session: Session) -> None:
        session.metadata.pop(self._PENDING_USER_TURN_KEY, None)

    def _clear_runtime_checkpoint(self, session: Session) -> None:
        if self._RUNTIME_CHECKPOINT_KEY in session.metadata:
            session.metadata.pop(self._RUNTIME_CHECKPOINT_KEY, None)

    @staticmethod
    def _checkpoint_message_key(message: dict[str, Any]) -> tuple[Any, ...]:
        return (
            message.get("role"),
            message.get("content"),
            message.get("tool_call_id"),
            message.get("name"),
            message.get("tool_calls"),
            message.get("reasoning_content"),
            message.get("thinking_blocks"),
        )

    def _restore_runtime_checkpoint(self, session: Session) -> bool:
        """Materialize an unfinished turn into session history before a new request."""
        from datetime import datetime

        checkpoint = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
        if not isinstance(checkpoint, dict):
            return False

        assistant_message = checkpoint.get("assistant_message")
        completed_tool_results = checkpoint.get("completed_tool_results") or []
        pending_tool_calls = checkpoint.get("pending_tool_calls") or []

        restored_messages: list[dict[str, Any]] = []
        if isinstance(assistant_message, dict):
            restored = dict(assistant_message)
            restored.setdefault("timestamp", datetime.now().isoformat())
            restored_messages.append(restored)
        for message in completed_tool_results:
            if isinstance(message, dict):
                restored = dict(message)
                restored.setdefault("timestamp", datetime.now().isoformat())
                restored_messages.append(restored)
        for tool_call in pending_tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_id = tool_call.get("id")
            name = ((tool_call.get("function") or {}).get("name")) or "tool"
            restored_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": name,
                    "content": "Error: Task interrupted before this tool finished.",
                    "timestamp": datetime.now().isoformat(),
                }
            )

        overlap = 0
        max_overlap = min(len(session.messages), len(restored_messages))
        for size in range(max_overlap, 0, -1):
            existing = session.messages[-size:]
            restored = restored_messages[:size]
            if all(
                self._checkpoint_message_key(left) == self._checkpoint_message_key(right)
                for left, right in zip(existing, restored)
            ):
                overlap = size
                break
        session.messages.extend(restored_messages[overlap:])

        self._clear_pending_user_turn(session)
        self._clear_runtime_checkpoint(session)
        return True

    def _restore_pending_user_turn(self, session: Session) -> bool:
        """Close a turn that only persisted the user message before crashing."""
        from datetime import datetime

        if not session.metadata.get(self._PENDING_USER_TURN_KEY):
            return False

        if session.messages and session.messages[-1].get("role") == "user":
            session.messages.append(
                {
                    "role": "assistant",
                    "content": "Error: Task interrupted before a response was generated.",
                    "timestamp": datetime.now().isoformat(),
                    # Same reason as the subagent follow-up above: an assistant
                    # message this process wrote has no thinking behind it, and
                    # a thinking model refuses the next request when the last
                    # one carries no `reasoning_content` at all. A crash
                    # recovery that makes the following turn fail is not a
                    # recovery.
                    "reasoning_content": "",
                }
            )
            session.updated_at = datetime.now()

        self._clear_pending_user_turn(session)
        return True

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        media: list[str] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        powerful: bool = False,
        profile: str | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload.

        *powerful* routes this one turn to the stronger model (see
        `powerful_model`). It is per-turn and not sticky: the caller decides
        each time, because the same session can hold both a hard question and
        "gracias".
        """
        await self._connect_mcp()
        await self.warm_up()
        msg = InboundMessage(
            channel=channel, sender_id="user", chat_id=chat_id,
            content=content, media=media or [],
        )
        return await self._process_message(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            powerful=powerful,
            profile=profile,
        )
