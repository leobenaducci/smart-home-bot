"""OpenAI-compatible HTTP API server for a fixed nanobot session.

Provides /v1/chat/completions and /v1/models endpoints.
All requests route to a single persistent API session.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hmac
import json as _json
import os
import re as _re
import time
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web
from loguru import logger

from nanobot.agent import delegate

from nanobot.config.paths import get_media_dir
from nanobot.utils.helpers import find_legal_message_end, safe_filename
from nanobot.utils.media_decode import (
    FileSizeExceeded as _FileSizeExceeded,
    MAX_FILE_SIZE,
    save_base64_data_url as _save_base64_data_url,
)
from nanobot.utils.debug_log import get_errors, clear_errors, log_error
from nanobot.utils.profiling import PROFILER
from nanobot.utils.profile_panel import PANEL_HTML as _PROFILE_PANEL_HTML
from nanobot.utils.shell_log import get_execs, clear_execs
from nanobot.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

__all__ = (
    "MAX_FILE_SIZE",
    "_FileSizeExceeded",
    "_save_base64_data_url",
    "create_app",
    "handle_chat_completions",
)


API_SESSION_KEY = "api:default"
API_CHAT_ID = "default"


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _error_json(status: int, message: str, err_type: str = "invalid_request_error") -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": err_type, "code": status}},
        status=status,
    )


def _chat_completion_response(content: str, model: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _response_text(value: Any) -> str:
    """Normalize process_direct output to plain assistant text."""
    if value is None:
        return ""
    if hasattr(value, "content"):
        return str(getattr(value, "content") or "")
    return str(value)

# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _sse_chunk(delta: str, model: str, chunk_id: str, finish_reason: str | None = None) -> bytes:
    """Format a single OpenAI-compatible SSE chunk."""
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": delta} if delta else {},
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {_json.dumps(payload)}\n\n".encode()


def _sse_hint(hint: str, chunk_id: str) -> bytes:
    """Format a tool-hint SSE event."""
    return f"data: {_json.dumps({'id': chunk_id, 'hint': hint})}\n\n".encode()


def _sse_step(step: dict[str, Any], chunk_id: str) -> bytes:
    """Format a structured step SSE event for the steps panel."""
    return f"data: {_json.dumps({'id': chunk_id, 'step': step})}\n\n".encode()


def _scrub_creds(text: str) -> str:
    """Replace -u user:pass patterns with -u [credentials]."""
    return _re.sub(
        r'(-u\s+)(["\']?\S*:\S*["\']?)',
        r'\1[credentials]',
        text,
    )


def _format_tool_detail(name: str, args: dict[str, Any]) -> str:
    """Extract a readable summary of tool arguments."""
    if name == "exec":
        return _scrub_creds(str(args.get("command", "")))
    if name in ("read_file", "write_file", "edit_file", "delete_file"):
        return str(args.get("path", ""))
    if name == "web_fetch":
        return str(args.get("url", ""))
    if name == "web_search":
        return str(args.get("query", ""))
    if name == "grep":
        pattern = str(args.get("pattern", "") or args.get("query", ""))
        path = str(args.get("path", ""))
        return f"{pattern} in {path}" if path else pattern
    if name == "glob":
        return str(args.get("pattern", ""))
    if name == "message":
        return str(args.get("content", ""))
    for v in args.values():
        if isinstance(v, str) and v:
            return v
    return ""


_SSE_DONE = b"data: [DONE]\n\n"

# How long a streaming turn may go completely silent — no token, no tool, no
# sign of thinking — before it is taken to be dead and handed to a sub-agent.
# Up here rather than inside the handler so the numbers can be read, changed
# and driven by a test without going spelunking for them.
_FIRST_TOKEN_BUDGET_S = 60.0
_FIRST_TOKEN_BUDGET_POWERFUL_S = 120.0
_IDLE_AFTER_START_BUDGET_S = 60.0

# Output pacing: how fast an answer is allowed to appear.
#
# The first version of this had one rule — never add more than 1.5 s of latency
# — and that rule ate the feature. `rate = max(base, backlog / 1.5)` means every
# answer drains inside a second and a half however long it is, so a 1500-char
# reply arrived at 167 words a second. Reading is about four. Short answers got
# paced and long ones, the only ones anybody needs paced, were dumped.
#
# So the rate is a reading rate now, and the catch-up is bounded rather than the
# latency. A backlog makes it speed up, never past _PACE_MAX_SPEEDUP, so a long
# answer takes longer than a short one — which is the entire point and what the
# old rule forbade.
#
# Words per minute because that is the unit this is tuned in by ear. Spanish
# averages ~6 characters a word including the space. 400 wpm is roughly 1.5×
# reading speed: quick enough not to feel slow, slow enough to follow.
#
# Per-instance: every family member has their own container, so this is already
# per-person and nobody needs a second knob invented for it.
_PACE_TICK_S = 0.05

# Below this many characters, what was streamed is preamble rather than an
# answer, and a stall should escalate to a sub-agent instead of ending the turn
# on it. Deliberately generous: being wrong in this direction costs one
# sub-agent run, while being wrong the other way costs the household an
# announcement of work that never happened.
_STALL_PREAMBLE_CHARS = 200
# How long the end of a streamed answer waits for its final usage report. The
# runner closes the stream and then reports that iteration's usage, normally in
# the same millisecond; this only bounds a provider that never reports any.
_USAGE_GRACE_S = 2.0
_PACE_WORDS_PER_MIN = float(os.environ.get("NANOBOT_PACE_WPM", "400"))
_PACE_CHARS_PER_WORD = 6.0
_PACE_CHARS_PER_S = _PACE_WORDS_PER_MIN * _PACE_CHARS_PER_WORD / 60.0
# How much a backlog may accelerate it. The pacer is meant to sit just behind
# the model, smoothing bursts; this is what stops "just behind" becoming "all at
# once" when a turn finishes faster than it can be read.
_PACE_MAX_SPEEDUP = 2.5
# Backlog at which it reaches that ceiling, in characters.
_PACE_BACKLOG_FULL = 600.0
# Longer than any word, shorter than any URL. A spaceless buffer under this
# is the last word of an answer and waits to go out whole; over it, it is a
# link or a blob and gets paced by characters instead.
_PACE_LONGEST_WORD = 40

# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------


def _parse_json_content(body: dict) -> tuple[str, list[str]]:
    """Parse JSON request body. Returns (text, media_paths)."""
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        raise ValueError("Only a single user message is supported")
    message = messages[0]
    if not isinstance(message, dict) or message.get("role") != "user":
        raise ValueError("Only a single user message is supported")

    user_content = message.get("content", "")
    media_dir = get_media_dir("api")
    media_paths: list[str] = []

    if isinstance(user_content, list):
        text_parts: list[str] = []
        for part in user_content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                url = part.get("image_url", {}).get("url", "")
                if url.startswith("data:"):
                    saved = _save_base64_data_url(url, media_dir)
                    if saved:
                        media_paths.append(saved)
                elif url:
                    raise ValueError(
                        "Remote image URLs are not supported. "
                        "Use base64 data URLs or upload files via multipart/form-data."
                    )
        text = " ".join(text_parts)
    elif isinstance(user_content, str):
        text = user_content
    else:
        raise ValueError("Invalid content format")

    return text, media_paths


async def _parse_multipart(request: web.Request) -> tuple[str, list[str], str | None, str | None]:
    """Parse multipart/form-data. Returns (text, media_paths, session_id, model)."""
    media_dir = get_media_dir("api")
    reader = await request.multipart()
    text = ""
    session_id = None
    model = None
    media_paths: list[str] = []

    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "message":
            text = (await part.read()).decode("utf-8")
        elif part.name == "session_id":
            session_id = (await part.read()).decode("utf-8").strip()
        elif part.name == "model":
            model = (await part.read()).decode("utf-8").strip()
        elif part.name == "files":
            raw = await part.read()
            if len(raw) > MAX_FILE_SIZE:
                raise _FileSizeExceeded(
                    f"File '{part.filename}' exceeds {MAX_FILE_SIZE // (1024 * 1024)}MB limit"
                )
            base = safe_filename(part.filename or "upload.bin")
            filename = f"{uuid.uuid4().hex[:12]}_{base}"
            dest = media_dir / filename
            dest.write_bytes(raw)
            media_paths.append(str(dest))

    if not text:
        text = "请分析上传的文件"

    return text, media_paths, session_id, model


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


ESCALATION_CONTEXT_TURNS = 8
ESCALATION_CONTEXT_CHARS = 4000


def _escalation_context(agent_loop, session_key: str) -> str | None:
    """The last few things said, for a turn that is being handed to a subagent
    (delegate.conversation_context, which the chat loop's own hand-off shares).
    "what is the price per image?" once arrived at a subagent that had never
    heard of an image and answered, correctly and uselessly, that it had no
    idea what we were looking at."""
    try:
        session = agent_loop.sessions.get_or_create(session_key)
    except Exception:
        return None
    return delegate.conversation_context(session, turns=ESCALATION_CONTEXT_TURNS,
                                         chars=ESCALATION_CONTEXT_CHARS)


def _session_key_for(channel: str, chat_id: str, session_id: str | None) -> str:
    """The session a request addresses.

    `channel:chat_id` when the caller named one, so that a turn and the
    subagent results that follow it share one history. Written once and used by
    both the completion and the stop below: two derivations of the same key are
    two chances for a stop to be aimed at a session nobody is running in.
    """
    if channel != "api" or chat_id != API_CHAT_ID:
        return f"{channel}:{chat_id}"
    if session_id:
        return f"api:{session_id}"
    return API_SESSION_KEY


def _message_text(msg: dict[str, Any]) -> str:
    """A message's text, whatever shape the content is in."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") in (None, "text"))
    return ""


async def handle_subagent_cancel(request: web.Request) -> web.Response:
    """POST /v1/subagents/cancel — stop one background task.

    `/v1/stop` cancels the turn a session is in and deliberately leaves
    subagents alone, because a background task is hours of work with its own
    record and must not evaporate because somebody stopped the chat turn that
    happened to start it. This is the other half of that sentence: the family
    looking at the «En segundo plano» panel and deciding *this* one is not worth
    finishing.

    One task, never a session's worth. Cancelling a row's neighbours is a bug
    that reads as a feature right up until four hours of research go with it.
    """
    if denied := _api_auth_error(request):
        return denied
    try:
        body = await request.json()
    except Exception:
        return _error_json(400, "Invalid JSON body")
    task_id = str(body.get("task_id") or "").strip()[:64]
    if not task_id:
        return _error_json(400, "task_id is required")
    subagents = getattr(request.app["agent_loop"], "subagents", None)
    if subagents is None:
        return _error_json(503, "no subagent manager in this instance")
    cancelled = await subagents.cancel(task_id)
    logger.info("api: cancel subagent {} -> {}", task_id,
                "stopped" if cancelled else "not running")
    # 200 either way: "it had already finished" is the same outcome the caller
    # wanted and not a failure to report. `cancelled` says which happened.
    return web.json_response({"ok": True, "task_id": task_id, "cancelled": cancelled})


async def handle_session_fork(request: web.Request) -> web.Response:
    """POST /v1/sessions/fork — branch a conversation at one message.

    "Take this somewhere else from here" needs the context up to that point and
    nothing after it. Replaying the earlier messages would mean paying for every
    turn again and getting different answers on the way; quoting the one message
    would hand the model a sentence and call it context. Sessions are files, so
    the honest version is to copy the prefix.

    The anchor is matched by *text*, not by index, because the two sides count
    differently and always will: HomeCore stores what the family said, this
    stores that plus tool calls, tool results, and the occasional system turn
    injected into the same conversation. Text is the one thing both have.
    Contained rather than equal — a message can pick up a location prefix on the
    way in — and the last match wins, since somebody who says "listo" twice
    means the second one.

    Where the cut lands depends on what was tapped, and the difference is the
    whole feature:

    - a **user** message → cut *before* it, so the branch ends on the previous
      answer and the caller re-asks. That is "ask this again, differently".
    - an **assistant** message → cut *after* it, so the branch ends on that
      answer and the caller says what comes next. That is "carry on from here".

    Then `find_legal_message_end` pulls the cut back to a boundary, because a
    fork landing mid-turn would copy an assistant message whose tool calls have
    no results, and every provider rejects that with a 400.

    Refuses to write into a session that already has messages. A fork is a new
    conversation by definition, and the failure it prevents — a mistyped target
    quietly overwriting a live chat — is not recoverable.
    """
    if denied := _api_auth_error(request):
        return denied
    try:
        body = await request.json()
    except Exception:
        return _error_json(400, "Invalid JSON body")

    channel = body.get("channel") or "websocket"
    source_key = _session_key_for(channel, body.get("source_chat_id") or "", None)
    target_key = _session_key_for(channel, body.get("target_chat_id") or "", None)
    anchor = str(body.get("anchor_text") or "").strip()
    role = "assistant" if str(body.get("anchor_role") or "user") == "assistant" else "user"
    if not (body.get("source_chat_id") and body.get("target_chat_id") and anchor):
        return _error_json(400, "source_chat_id, target_chat_id and anchor_text are required")
    if source_key == target_key:
        return _error_json(400, "a session cannot fork into itself")

    sessions = request.app["agent_loop"].sessions
    source = sessions.get_or_create(source_key)
    if not source.messages:
        return _error_json(404, f"nothing to fork: {source_key} has no messages")

    needle = " ".join(anchor.split())[:400]
    cut = None
    for i in range(len(source.messages) - 1, -1, -1):
        msg = source.messages[i]
        if msg.get("role") != role:
            continue
        if needle and needle in " ".join(_message_text(msg).split()):
            cut = i if role == "user" else i + 1
            break
    if cut is None:
        return _error_json(404, "that message is not in this conversation any more")

    kept = find_legal_message_end(source.messages, cut)
    target = sessions.get_or_create(target_key)
    if target.messages:
        return _error_json(409, f"{target_key} already has a conversation in it")

    target.messages = copy.deepcopy(source.messages[:kept])
    # Consolidation counts messages already summarised into files, and those
    # files belong to the source. Reset it: the copy has never been consolidated,
    # and inheriting a count would make the branch hide its own first messages.
    target.last_consolidated = 0
    target.metadata = dict(target.metadata or {})
    target.metadata.update({"forked_from": source_key, "forked_messages": kept})
    sessions.save(target, fsync=True)
    logger.info("api: forked {} -> {} at {} message(s) of {}",
                source_key, target_key, kept, len(source.messages))
    return web.json_response({
        "ok": True, "source_key": source_key, "target_key": target_key,
        "kept": kept, "source_messages": len(source.messages),
        # How far back the boundary pulled the cut. Zero almost always; when it
        # is not, the fork sits before a tool turn rather than inside it, and a
        # caller reporting "from here" should know it means slightly earlier.
        "trimmed": max(0, cut - kept),
    })


async def handle_stop(request: web.Request) -> web.Response:
    """POST /v1/stop — cancel the turn a session is in the middle of.

    Closing the HTTP response already cancels the run, but only when the
    process next tries to write to it: a turn thinking for two minutes inside a
    tool call notices nothing, and "Detener" that takes two minutes is not a
    stop. This says so out of band, and the answer is immediate.

    Deliberately not subagents. `/stop` on a channel cancels those too, but a
    background task here is the "En segundo plano" panel — hours of work, its
    own record, its own delivery — and it must not evaporate because somebody
    stopped the chat turn that happened to spawn it. `cancel_by_session` is the
    call to add if that is ever wanted; it is not wanted by accident.
    """
    if denied := _api_auth_error(request):
        return denied
    try:
        body = await request.json()
    except Exception:
        body = {}
    session_key = _session_key_for(
        body.get("channel") or "api",
        body.get("chat_id") or API_CHAT_ID,
        body.get("session_id"),
    )
    runs = request.app["active_runs"].get(session_key) or set()
    stopped = sum(1 for t in list(runs) if not t.done() and t.cancel())
    logger.info("api: stop session_key={} cancelled={}", session_key, stopped)
    return web.json_response({"stopped": stopped, "session_key": session_key})


# How long "Posponer" pushes a reminder out. One value, named, because it is
# the answer to "how long is later" and somebody will want to change it.
REMINDER_SNOOZE_MINUTES = 15


async def handle_cron_action(request: web.Request) -> web.Response:
    """POST /v1/cron/action — answer a reminder from the notification shade.

    HomeCore puts three buttons on a fired reminder and calls this with
    whichever was pressed. The three mean genuinely different things to the
    job behind the reminder, which is why this is not one "dismiss":

    - **done** — the thing was done. A one-time job has already deleted
      itself, so this changes nothing; a recurring one is deliberately left
      alone, because having taken today's pills is not a reason to stop being
      reminded tomorrow.
    - **discard** — stop reminding me. That one *is* about the job, so the job
      goes, recurring or not.
    - **snooze** — ask again in a quarter of an hour, as a one-time job that
      deletes itself after it fires. The original is untouched: snoozing
      today's 8am does not move tomorrow's.
    """
    if denied := _api_auth_error(request):
        return denied
    try:
        body = await request.json()
    except Exception:
        body = {}
    job_id = (body.get("job") or "").strip()
    do = (body.get("do") or "").strip().lower()
    if not job_id or do not in ("done", "discard", "snooze"):
        return web.json_response(
            {"error": "job y do (done|discard|snooze) son obligatorios"}, status=400)

    cron = getattr(request.app["agent_loop"], "cron_service", None)
    if cron is None:
        return web.json_response({"error": "cron is not configured"}, status=503)

    job = cron.get_job(job_id)
    if job is None:
        # Already gone is the expected case for a one-time reminder, not a
        # failure: the job deleted itself when it fired. Saying "not found"
        # would make a button that worked look broken.
        return web.json_response({"ok": True, "action": do, "job": None})

    if do == "discard":
        result = cron.remove_job(job_id)
        return web.json_response({"ok": result != "protected", "action": do,
                                  "result": result})

    if do == "snooze":
        from nanobot.cron.types import CronSchedule
        when_ms = int(time.time() * 1000) + REMINDER_SNOOZE_MINUTES * 60 * 1000
        new = cron.add_job(
            name=f"{job.name} (pospuesto)",
            schedule=CronSchedule(kind="at", at_ms=when_ms),
            message=job.payload.message,
            deliver=job.payload.deliver,
            channel=job.payload.channel,
            to=job.payload.to,
            delete_after_run=True,
        )
        return web.json_response({"ok": True, "action": do, "job": new.id,
                                  "minutes": REMINDER_SNOOZE_MINUTES})

    return web.json_response({"ok": True, "action": do, "job": job_id})


async def handle_chat_completions(request: web.Request) -> web.Response:
    """POST /v1/chat/completions — supports JSON and multipart/form-data."""
    if denied := _api_auth_error(request):
        return denied
    content_type = request.content_type or ""
    if not isinstance(content_type, str):
        content_type = ""

    agent_loop = request.app["agent_loop"]
    timeout_s: float = request.app.get("request_timeout", 120.0)
    model_name: str = request.app.get("model_name", "nanobot")

    stream = False
    # Defaulted here, not only in the JSON branch: a multipart upload never
    # reaches the code that parses it, and the non-streaming path below reads
    # it either way.
    powerful = False
    profile = None
    _channel = "api"
    _chat_id = API_CHAT_ID
    try:
        if content_type.startswith("multipart/"):
            text, media_paths, session_id, requested_model = await _parse_multipart(request)
        else:
            try:
                body = await request.json()
            except Exception:
                return _error_json(400, "Invalid JSON body")
            stream = body.get("stream", False)
            requested_model = body.get("model")
            no_escalate = bool(body.get("no_escalate", False))
            # A boolean, not a model name: the caller says "this turn is worth
            # the better model" and the deployment decides which one that is.
            # Letting a request name a model would make every client a place
            # where the model roster is written down.
            powerful = bool(body.get("powerful", False))
            # A *role*, not a model name — the roster lives in this process's
            # config (`modelProfiles`), so a client names what kind of turn this
            # is and never has to know, or be redeployed for, which model serves
            # it. An unconfigured role falls through to `powerful`/default
            # rather than erroring: a deployment with no profiles keeps working.
            raw_profile = body.get("profile")
            profile = str(raw_profile).strip()[:64] if raw_profile else None
            text, media_paths = _parse_json_content(body)
            session_id = body.get("session_id")
            _channel = body.get("channel") or "api"
            _chat_id = body.get("chat_id") or API_CHAT_ID
    except ValueError as e:
        return _error_json(400, str(e))
    except _FileSizeExceeded as e:
        return _error_json(413, str(e), err_type="invalid_request_error")
    except Exception:
        logger.exception("Error parsing upload")
        return _error_json(413, "File too large or invalid upload")

    if requested_model and requested_model != model_name:
        return _error_json(400, f"Only configured model '{model_name}' is available")

    session_key = _session_key_for(_channel, _chat_id, session_id)
    session_locks: dict[str, asyncio.Lock] = request.app["session_locks"]
    session_lock = session_locks.setdefault(session_key, asyncio.Lock())

    logger.info(
        "API request session_key={} media={} text={} stream={}",
        session_key, len(media_paths), text[:80], stream,
    )
    # -- streaming path --
    if stream:
        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        # Never compressed. zlib holds small writes until it has a block's worth
        # or the stream closes, and every SSE event here is small -- so with a
        # client that accepts deflate (home-core's relay does, it is `requests`)
        # the whole turn arrived in one burst at the end: steps, text and [DONE]
        # within the same millisecond, and a two-minute tool turn showed nothing
        # at all while it ran. Measured 2026-09-10: first step at 9.5s with
        # deflate, 7.3s without, on the same turn. The proxy in front of the
        # browser compresses with a flush per event, which is the place for it.
        await resp.prepare(request)

        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        queue: asyncio.Queue[str | tuple[str, str] | None] = asyncio.Queue()
        stream_failed = False
        any_tokens_sent = False
        # How much text has actually reached the reader. `any_tokens_sent` alone
        # is too weak a test for "the reader already has an answer": a model
        # that opens with "Voy a buscar la luz en Home Assistant" has sent
        # tokens and said nothing, and on 2026-09-20 that sentence is what ended
        # a turn. The stall handler below counted it as a partial answer and so
        # declined to escalate — the household got an announcement of work that
        # never happened, waited fifty minutes, and asked "Y?".
        #
        # Length is the test because a *finished* short answer never reaches
        # that branch: it ends the stream normally. Arriving there means the
        # model was still working, so anything this short is preamble.
        streamed_chars = 0

        # Latency for the performance line, all from the moment the request
        # arrived: the first word the reader sees, and the whole turn. Taken
        # here rather than by the page, which only sees its own side of the
        # relay and cannot tell a slow model from a slow queue.
        _t0 = time.monotonic()
        _first_token_at: list[float | None] = [None]
        _llm_calls: list[int] = [0]

        async def _on_stream(token: str) -> None:
            if _first_token_at[0] is None and token:
                _first_token_at[0] = time.monotonic()
            await queue.put(token)

        _last_ts: list[float] = [time.monotonic()]
        _last_llm_ms: list[int] = [0]
        # Whether `_last_llm_ms` belongs to the iteration whose usage comes next.
        # It does after a tool hint; the final, answering iteration has none,
        # and timing it with the previous iteration's stretch reported a rate
        # for words the model had not written yet.
        _llm_ms_fresh: list[bool] = [False]
        _usage_step: list[dict[str, Any] | None] = [None]
        _stream_ended: list[bool] = [False]
        _final_usage = asyncio.Event()
        _finished: list[bool] = [False]
        _enders: list[asyncio.Future] = []

        async def _finish() -> None:
            """The usage step, then the terminator -- once, whoever gets here first."""
            if _finished[0]:
                return
            _finished[0] = True
            # Latency rides on the usage step rather than getting one of its
            # own: a turn whose provider reports no usage sends no step at all,
            # which keeps a plain stream nothing but `choices` chunks for any
            # strict OpenAI-style client. Every engine this house runs reports
            # usage, so nothing is lost here.
            step = _usage_step[0]
            if step is not None:
                step["total_ms"] = int((time.monotonic() - _t0) * 1000)
                if _first_token_at[0] is not None:
                    step["first_ms"] = int((_first_token_at[0] - _t0) * 1000)
                step["calls"] = _llm_calls[0]
                with contextlib.suppress(Exception):
                    await queue.put(("step", step))
            _usage_step[0] = None
            await queue.put(None)

        async def _finish_when_usage_lands() -> None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_final_usage.wait(), timeout=_USAGE_GRACE_S)
            await _finish()

        async def _on_stream_end(*_a: Any, resuming: bool = False, **_kw: Any) -> None:
            if not resuming:
                # This is what ends the consumer on a turn with no tool call --
                # and it fires *before* the hook reports that iteration's usage:
                # the runner closes the stream, then runs `after_iteration`.
                # Terminating here, as this used to, ended the response one
                # step too early, so a plain chat answer never carried a
                # performance line and a tool turn's line described its first
                # model call rather than its last. So the terminator waits for
                # the final usage, briefly: it normally lands in the same
                # millisecond, and a provider that reports none must not hold
                # the end of an answer hostage. `_run`'s `finally` is the
                # backstop for a turn that returns before either happens.
                _stream_ended[0] = True
                _final_usage.clear()
                _enders.append(asyncio.ensure_future(_finish_when_usage_lands()))

        async def _on_progress(
            content: str,
            *,
            tool_hint: bool = False,
            tool_events: list[Any] | None = None,
            thinking: str | None = None,
            usage: dict[str, Any] | None = None,
            alive: bool = False,
            plan: dict[str, Any] | None = None,
            **_kw: Any,
        ) -> None:
            now = time.monotonic()

            if plan is not None:
                # The whole checklist, every time: the page redraws its card
                # from the last one and needs nothing earlier (tools/plan.py).
                await queue.put(("step", {"type": "plan", **plan}))
                return

            if alive:
                # The model is thinking and has produced nothing to show yet.
                # Nothing goes on the wire — this exists so the loop below can
                # tell a turn that is working from one that has died.
                await queue.put(("alive", ""))
                return

            if usage is not None:
                pt = usage.get("prompt_tokens", 0) or 0
                ct = usage.get("completion_tokens", 0) or 0
                if pt or ct:
                    # `model` is what actually served the turn, not what was
                    # configured -- inside an outage window those differ, and
                    # the step should say which one wrote the words on screen.
                    #
                    # The rate is completion tokens over the time since the
                    # last step, which is the stretch the model spent
                    # generating them. On a local engine that number is the
                    # one that moves when the card is busy, so it is worth
                    # more here than the raw token count.
                    step: dict[str, Any] = {"type": "usage", "in": pt, "out": ct}
                    if usage.get("model"):
                        step["model"] = usage["model"]
                    if usage.get("effort"):
                        step["effort"] = usage["effort"]
                    # The generating stretch if this iteration had one (it
                    # ended in a tool call), else the time since the last mark:
                    # the request, or the tools finishing.
                    secs = ((_last_llm_ms[0] / 1000.0) if _llm_ms_fresh[0]
                            else (now - _last_ts[0]))
                    _llm_ms_fresh[0] = False
                    if ct and secs > 0.05:
                        step["rate"] = round(ct / secs, 1)
                    # Held, not sent. The hook that produces this fires as the
                    # last iteration finishes, and on a turn with no tool call
                    # that raced the end of the response: the step was queued
                    # after the consumer had already stopped reading, so a
                    # plain chat answer reported nothing at all while a
                    # tool-using turn reported fine. Emitted once, immediately
                    # before the terminator, where the consumer is still there
                    # -- which also makes it a turn summary rather than a
                    # mid-turn sample, and keeps the last (cumulative) usage
                    # when a turn ran several iterations.
                    _llm_calls[0] += 1
                    _usage_step[0] = step
                    if _stream_ended[0]:
                        _final_usage.set()
                return

            if not tool_hint:
                if tool_events:
                    _last_ts[0] = now
                return

            # tool_hint=True: LLM just finished, tools about to start
            llm_ms = int((now - _last_ts[0]) * 1000)
            # Kept for the usage step, which arrives after this one has already
            # moved the mark: measuring from `_last_ts` there timed the gap
            # since the tool hint (milliseconds) instead of the stretch the
            # model spent generating, so the rate was silently dropped.
            _last_llm_ms[0] = llm_ms
            _llm_ms_fresh[0] = True
            _last_ts[0] = now

            await queue.put(("step", {"type": "llm", "ms": llm_ms}))

            if thinking:
                snippet = _scrub_creds(thinking.strip())
                if snippet:
                    await queue.put(("step", {"type": "thinking", "text": snippet}))

            for te in (tool_events or []):
                name = te.get("name", "")
                args = te.get("arguments", {}) or {}
                detail = _format_tool_detail(name, args)
                await queue.put(("step", {"type": "tool", "name": name, "detail": detail}))

            # Keep exec hint text for backward compat (scrub credentials)
            names = {te.get("name", "") for te in (tool_events or [])}
            if "exec" in names and content:
                await queue.put(("hint", _scrub_creds(content)))

        fallback_content: list[str] = []

        async def _run() -> None:
            nonlocal stream_failed
            try:
                async with session_lock:
                    result = await asyncio.wait_for(
                        agent_loop.process_direct(
                            content=text,
                            media=media_paths if media_paths else None,
                            session_key=session_key,
                            channel=_channel,
                            chat_id=_chat_id,
                            on_stream=_on_stream,
                            on_stream_end=_on_stream_end,
                            on_progress=_on_progress,
                            powerful=powerful,
                            profile=profile,
                        ),
                        timeout=timeout_s,
                    )
                # If the model used the message tool, no tokens were streamed —
                # store the content so the SSE loop can emit it.
                if result is not None:
                    content = _response_text(result)
                    if content and content.strip():
                        fallback_content.append(content)
            except Exception as _exc:
                stream_failed = True
                logger.exception("Streaming error for session {}", session_key)
                log_error("api.chat_completions.stream", _exc, session_key=session_key)
            finally:
                # Every turn gets one, tool or not -- and if the stream's end
                # is still waiting on usage, this is the usage it was waiting
                # for or the proof there will be none.
                await _finish()

        # Escalate to sub-agent if nothing at all arrives within 60s OR if
        # streaming stalls for 60s after it started. Ollama needs up to ~25s
        # for first token on complex tasks (building tool calls with long
        # system prompts). no_escalate=True (debug flag) disables both.
        #
        # "Nothing at all" is the load-bearing part, and it used to mean "no
        # token the reader can see". A thinking model deliberates for a minute
        # before its first visible token while streaming reasoning the whole
        # time, so the turns that needed the most thought were exactly the ones
        # ruled dead and pushed to a sub-agent — the person asked a question in
        # a chat and got "I'll tell you when it's ready" for their trouble. Every
        # item on the queue now counts as proof of life, including the liveness
        # ticks minted from reasoning deltas, so this fires on a *stalled* turn
        # and not merely on a slow one.
        #
        # A `powerful` turn gets longer: the flag means the caller asked for a
        # slower, better model, so timing out on the budget tuned for the fast
        # one would cancel it and fall back to the *sub-agent* model — the
        # harder the question, the more reliably it degrades, which inverts the
        # whole point of the flag. HomeCore sets it on every profession turn.
        _first_token_budget = (
            _FIRST_TOKEN_BUDGET_POWERFUL_S if powerful else _FIRST_TOKEN_BUDGET_S
        )
        _FIRST_TOKEN_TIMEOUT_S = None if no_escalate else _first_token_budget
        _IDLE_AFTER_START_S = None if no_escalate else _IDLE_AFTER_START_BUDGET_S

        task = asyncio.create_task(_run())
        # Findable by `/v1/stop` for as long as it runs. Only the streaming
        # path registers: it is the one a person waits on and can ask to stop,
        # and the non-streaming path holds no task to cancel — it awaits inside
        # the handler, so the request is the only handle on it.
        active_runs: dict[str, set] = request.app["active_runs"]
        active_runs.setdefault(session_key, set()).add(task)

        # -- output pacing --
        #
        # Tokens arrive in whatever shape the provider and the network hand
        # them over: forty at once, then nothing for two seconds, then forty
        # more. Written straight through, the same answer reads as smooth on
        # one turn and as a stutter on the next, for reasons that have nothing
        # to do with what was said — and a burst that lands all at once is not
        # read as typing at all, it is read as a page appearing.
        #
        # So the text is buffered and released at a steady rate. The rate
        # floats up when the buffer is deep, which bounds the latency pacing
        # can add: a long answer takes longer to arrive than a short one,
        # because a long answer takes longer to read. Progress events and the final flush are
        # ordered against the buffer, never interleaved into it.
        pending = ""
        last_emit = time.monotonic()

        def _pace_slice(now: float) -> str:
            """The next few words, at a rate somebody can read along with."""
            nonlocal pending, last_emit
            # Speeds up with the backlog, up to a ceiling. Bounded, unlike the
            # old `backlog / max_lag`, which had no ceiling at all and turned
            # every long answer into a single paint.
            speedup = 1.0 + (len(pending) / _PACE_BACKLOG_FULL) * (_PACE_MAX_SPEEDUP - 1.0)
            rate = _PACE_CHARS_PER_S * min(speedup, _PACE_MAX_SPEEDUP)
            n = int(rate * (now - last_emit))
            if n <= 0:
                return ""

            if n < len(pending):
                # Words arrive whole: letters accumulating mid-word is a
                # typewriter, and this is meant to read like somebody talking.
                cut = pending.rfind(" ", 0, n + 1)
                if cut > 0:
                    n = cut + 1
                elif " " in pending or len(pending) <= _PACE_LONGEST_WORD:
                    # The slice landed inside a word. Wait rather than split it
                    # — `last_emit` does not move, so the next tick's slice is
                    # longer and will reach the space. Two cases: the start of
                    # an answer, where one tick's worth is shorter than the
                    # first word, and the last word of all, which has no space
                    # after it to find.
                    return ""
                # else: a long run with no word boundary in it — a URL, a base64
                # blob, a table row. Cut where the rate says, because waiting
                # for a space that is not coming would hold the whole thing back
                # and then paint it, which is what this rewrite exists to
                # remove.

            last_emit = now
            out, pending = pending[:n], pending[n:]
            return out

        async def _flush_pending() -> None:
            """Write the whole buffer now. For ordering and endings, not pace."""
            nonlocal pending, last_emit
            if pending:
                await resp.write(_sse_chunk(pending, model_name, chunk_id))
                pending = ""
            last_emit = time.monotonic()

        last_activity = time.monotonic()
        # One getter, reused across pacing ticks.
        #
        # This used to be `wait_for(shield(queue.get()), ...)` with a fresh
        # `queue.get()` every time round. `wait_for` cancels what it is handed,
        # and it is handed the *shield* -- so the getter behind it survived the
        # timeout and stayed in the queue's waiting line, while the next lap
        # created another. The pile of live getters nobody awaited each took an
        # item and dropped it. It fired whenever the model was slower than the
        # 50 ms tick, which is every long answer: a 1411-character reply reached
        # the chat as 656 characters, in alternating strips cut mid-word.
        #
        # So the getter outlives the tick, and only the lap that actually takes
        # an item clears it.
        getter: asyncio.Future | None = None
        try:
            while True:
                now = time.monotonic()
                esc_timeout_s = _IDLE_AFTER_START_S if any_tokens_sent else _FIRST_TOKEN_TIMEOUT_S
                if esc_timeout_s is None:
                    wait_s = _PACE_TICK_S if pending else None
                else:
                    wait_s = max(0.0, last_activity + esc_timeout_s - now)
                    if pending:
                        wait_s = min(wait_s, _PACE_TICK_S)
                if getter is None:
                    getter = asyncio.ensure_future(queue.get())
                try:
                    item = await asyncio.wait_for(
                        asyncio.shield(getter),
                        timeout=wait_s,
                    )
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    if esc_timeout_s is None or now - last_activity < esc_timeout_s:
                        # Not a stall — the pacer asked to be woken so the
                        # buffer keeps draining while the model works. With
                        # escalation off this is the only way the wait ends.
                        slice_ = _pace_slice(now)
                        if slice_:
                            await resp.write(_sse_chunk(slice_, model_name, chunk_id))
                        continue
                    if any_tokens_sent and streamed_chars >= _STALL_PREAMBLE_CHARS:
                        # Whatever the pacer is still holding belongs to the
                        # reader: this branch ends the stream, so unsent text
                        # would otherwise be lost to a stall it predates.
                        await _flush_pending()
                        # Partial response already streamed — end gracefully, don't inject noise
                        logger.warning(
                            "Mid-stream stall for session {}; ending stream with partial response",
                            session_key,
                        )
                    else:
                        # Nothing shown yet — escalate to sub-agent
                        logger.warning(
                            "Stream timeout for session {}; escalating to sub-agent",
                            session_key,
                        )
                        try:
                            await agent_loop.subagents.spawn(
                                task=text,
                                # What was being talked about. Without it the
                                # subagent gets the question and nothing else,
                                # and a question that refers to the thing on
                                # screen cannot be answered from the words
                                # alone. See `_escalation_context`.
                                context=_escalation_context(agent_loop, session_key),
                                # Carry the caller's escalation across: a turn
                                # worth the stronger model does not become
                                # worth less because it timed out. `text` still
                                # holds its standing-context markers, so the
                                # sub-agent gets the persona too — stripped by
                                # _process_message the same as any other turn.
                                complex=powerful,
                                origin_channel=_channel,
                                origin_chat_id=_chat_id,
                                session_key=session_key,
                            )
                            notice = delegate.ack("long")
                            await resp.write(_sse_chunk(notice, model_name, chunk_id))
                        except Exception:
                            logger.exception("Escalation failed for session {}", session_key)
                    try:
                        await resp.write(_sse_chunk("", model_name, chunk_id, finish_reason="stop"))
                        await resp.write(_SSE_DONE)
                    except Exception:
                        pass
                    finally:
                        task.cancel()
                    return resp
                # Taken. Only here: every path above that did not return an
                # item leaves the getter in place, still waiting on the queue.
                getter = None
                # Anything at all on the queue means the turn is alive, which
                # is the whole point of the liveness ticks: they are the only
                # item that arrives while a model is thinking in silence.
                last_activity = time.monotonic()
                if item is None:
                    break
                if isinstance(item, tuple):
                    kind, value = item
                    if kind == "alive":
                        continue
                    # A step or a hint that overtook buffered text would put
                    # the tool it describes above the sentence that introduces
                    # it, so the buffer goes out first.
                    await _flush_pending()
                    if kind == "hint":
                        await resp.write(_sse_hint(value, chunk_id))
                    elif kind == "step":
                        await resp.write(_sse_step(value, chunk_id))
                else:
                    pending += item
                    streamed_chars += len(item)
                    # True on arrival, not on the wire: the token exists, so
                    # this turn is past the point where escalating to a
                    # sub-agent could still be the right answer.
                    any_tokens_sent = True

            # The producer is finished. Let the tail out at the same pace
            # rather than dumping it: at the ceiling this is bounded by the
            # length of what is left, which is the trade — a long answer takes
            # longer to arrive, because a long answer takes longer to read.
            #
            while pending:
                await asyncio.sleep(_PACE_TICK_S)
                slice_ = _pace_slice(time.monotonic())
                if slice_:
                    await resp.write(_sse_chunk(slice_, model_name, chunk_id))
        finally:
            # A getter still waiting on the queue when the stream ends is a
            # pending future nobody will await.
            if getter is not None:
                getter.cancel()
            task.cancel()
            # Every exit from the loop above passes through here, including the
            # escalation branch's `return` — so the registry cannot accumulate
            # tasks that ended, and a later stop cannot aim at one of them.
            runs = active_runs.get(session_key)
            if runs is not None:
                runs.discard(task)
                if not runs:
                    active_runs.pop(session_key, None)

        if not stream_failed:
            # Emit fallback content from message tool if nothing was streamed
            if fallback_content and not any_tokens_sent:
                await resp.write(_sse_chunk(fallback_content[0], model_name, chunk_id))
            await resp.write(_sse_chunk("", model_name, chunk_id, finish_reason="stop"))
            await resp.write(_SSE_DONE)
        else:
            # Together AI failed (timeout or quick error). If no meaningful
            # response was delivered, escalate to sub-agent instead of showing error.
            if _channel != "api" and text:
                logger.warning(
                    "Stream failed for session {}; escalating to sub-agent",
                    session_key,
                )
                try:
                    await agent_loop.subagents.spawn(
                        task=text,
                        context=_escalation_context(agent_loop, session_key),
                        complex=powerful,       # see the timeout branch above
                        origin_channel=_channel,
                        origin_chat_id=_chat_id,
                        session_key=session_key,
                    )
                    notice = delegate.ack("long")
                    await resp.write(_sse_chunk(notice, model_name, chunk_id))
                    await resp.write(_sse_chunk("", model_name, chunk_id, finish_reason="stop"))
                    await resp.write(_SSE_DONE)
                except Exception:
                    logger.exception("Sub-agent escalation failed for session {}", session_key)
                    err_payload = _json.dumps({"error": "Failed to process request"})
                    try:
                        await resp.write(f"data: {err_payload}\n\n".encode())
                        # No [DONE] here on purpose. It is the protocol's
                        # success terminator, and a client that reads it treats
                        # the stream as having completed normally — the error
                        # frame above becomes just another chunk it ignores.
                        # The response closes right after, so clients that wait
                        # for a terminator get EOF instead of a false success.
                    except Exception:
                        pass
            else:
                stream_error_detail = ""
                try:
                    recent = get_errors(limit=1, source="api.chat_completions.stream")
                    if recent:
                        stream_error_detail = recent[0].get("error_message", "")
                except Exception:
                    pass
                err_payload = _json.dumps({"error": stream_error_detail or "Failed to process request"})
                try:
                    await resp.write(f"data: {err_payload}\n\n".encode())
                    # Deliberately no [DONE] — see the note on the escalation
                    # failure path above. A failed request must not terminate
                    # like a successful one.
                except Exception:
                    pass
        return resp

    # -- non-streaming path --
    _FALLBACK = EMPTY_FINAL_RESPONSE_MESSAGE

    try:
        async with session_lock:
            try:
                response = await asyncio.wait_for(
                    agent_loop.process_direct(
                        content=text,
                        media=media_paths if media_paths else None,
                        session_key=session_key,
                        channel=_channel,
                        chat_id=_chat_id,
                        powerful=powerful,
                        profile=profile,
                    ),
                    timeout=timeout_s,
                )
                response_text = _response_text(response)

                if not response_text or not response_text.strip():
                    logger.warning("Empty response for session {}, retrying", session_key)
                    retry_response = await asyncio.wait_for(
                        agent_loop.process_direct(
                            content=text,
                            media=media_paths if media_paths else None,
                            session_key=session_key,
                            channel=_channel,
                            chat_id=_chat_id,
                            powerful=powerful,
                            profile=profile,
                        ),
                        timeout=timeout_s,
                    )
                    response_text = _response_text(retry_response)
                    if not response_text or not response_text.strip():
                        logger.warning("Empty response after retry, using fallback")
                        response_text = _FALLBACK

            except asyncio.TimeoutError:
                return _error_json(504, f"Request timed out after {timeout_s}s")
            except Exception as _exc:
                logger.exception("Error processing request for session {}", session_key)
                log_error("api.chat_completions", _exc, session_key=session_key)
                return _error_json(500, f"Internal server error: {_exc!r}", err_type="server_error")
    except Exception as _exc:
        logger.exception("Unexpected API lock error for session {}", session_key)
        log_error("api.chat_completions.lock", _exc, session_key=session_key)
        return _error_json(500, f"Internal server error: {_exc!r}", err_type="server_error")

    return web.json_response(_chat_completion_response(response_text, model_name))


async def handle_models(request: web.Request) -> web.Response:
    """GET /v1/models"""
    model_name = request.app.get("model_name", "nanobot")
    return web.json_response(
        {
            "object": "list",
            "data": [
                {
                    "id": model_name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "nanobot",
                }
            ],
        }
    )


async def handle_health(request: web.Request) -> web.Response:
    """GET /health"""
    return web.json_response({"status": "ok"})


def _api_auth_error(request: web.Request) -> web.Response | None:
    """Gate /v1/chat/completions on NANOBOT_API_SECRET. None means "let it through".

    This endpoint drives a specific family member's assistant: it can send
    messages as them, run their skills and read their history. It was reachable
    by anything on the LAN, alongside cameras and ESP32s — the devices that
    actually get compromised.

    Open when unset, unlike the debug gate which is closed. The asymmetry is
    deliberate: an unset debug secret costs an operator a diagnostic, while an
    unset secret here would take away the family's assistant with no warning
    and no way for them to fix it. HomeCore was taught to send the credential
    first (HomeCore 92737bc) precisely so this can be turned on without an
    outage — and if the credential goes missing later, degrading to the old
    behaviour beats a house that cannot talk to its own house.
    """
    secret = os.environ.get("NANOBOT_API_SECRET", "").strip()
    if not secret:
        return None
    provided = ""
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        provided = auth[7:].strip()
    if not provided:
        provided = (request.headers.get("X-Nanobot-Auth") or "").strip()
    if not provided or not hmac.compare_digest(provided, secret):
        logger.warning(
            "api: rejected an unauthenticated request from {}",
            request.remote or "unknown",
        )
        return web.json_response({"error": "unauthorized"}, status=401)
    return None


def _debug_auth_error(request: web.Request) -> web.Response | None:
    """Gate the debug routes on NANOBOT_DEBUG_SECRET. None means "let it through".

    These return the agent's recent commands and their output, which is the
    most sensitive thing this process holds. They were open to anyone who could
    reach the port, and that is how a Paperless API token got out — every token
    in the house had to be rotated.

    Closed by default, unlike the websocket's token_issue_secret which only
    warns when unset. That one gates issuing a token; this one gates reading
    what already happened, so an operator who has not thought about it should
    get silence rather than the log.

    Compared with compare_digest: the check is cheap and remote, so a timing
    signal is worth denying even though it is a stretch to exploit.
    """
    secret = os.environ.get("NANOBOT_DEBUG_SECRET", "").strip()
    if not secret:
        return web.json_response(
            {"error": "debug endpoints are disabled",
             "detail": "set NANOBOT_DEBUG_SECRET to enable, then send it as "
                       "X-Debug-Secret or Authorization: Bearer <secret>"},
            status=403,
        )
    provided = (request.headers.get("X-Debug-Secret") or "").strip()
    if not provided:
        auth = (request.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
    if not provided or not hmac.compare_digest(provided, secret):
        return web.json_response({"error": "unauthorized"}, status=401)
    return None


async def handle_debug_errors(request: web.Request) -> web.Response:
    """GET /v1/debug/errors — return recent error log entries.

    Query params:
        limit (int, default 50): max entries to return
        source (str, optional): filter by error source
        clear (bool, default false): clear log after reading
    """
    if denied := _debug_auth_error(request):
        return denied
    try:
        limit = int(request.query.get("limit", "50"))
    except (ValueError, TypeError):
        limit = 50
    source = request.query.get("source")
    errors = get_errors(limit=limit, source=source)
    if request.query.get("clear", "").lower() in ("1", "true", "yes"):
        clear_errors()
    return web.json_response({"errors": errors, "count": len(errors)})


async def handle_debug_shell_log(request: web.Request) -> web.Response:
    """GET /v1/debug/shell-log — return recent exec tool invocations with output.

    Query params:
        limit (int, default 20): max entries to return
        clear (bool, default false): clear log after reading
    """
    if denied := _debug_auth_error(request):
        return denied
    try:
        limit = int(request.query.get("limit", "20"))
    except (ValueError, TypeError):
        limit = 20
    entries = get_execs(limit=limit)
    if request.query.get("clear", "").lower() in ("1", "true", "yes"):
        clear_execs()
    return web.json_response({"entries": entries, "count": len(entries)})


async def handle_debug_profile(request: web.Request) -> web.Response:
    """GET /v1/debug/profile — where turns spend their time and tokens.

    Query params:
        limit (int, default 50): rows per table
        session (str, optional): only this session key
        clear (bool, default false): reset the buffers after reading
    """
    if denied := _debug_auth_error(request):
        return denied
    try:
        limit = max(1, min(500, int(request.query.get("limit", "50"))))
    except (ValueError, TypeError):
        limit = 50
    snap = PROFILER.snapshot(limit=limit, session_key=request.query.get("session"))
    if request.query.get("clear", "").lower() in ("1", "true", "yes"):
        PROFILER.clear()
    return web.json_response(snap)


async def handle_debug_profile_panel(request: web.Request) -> web.Response:
    """GET /v1/debug/profile.html — the same thing, readable.

    Deliberately unauthenticated, because it contains nothing: it is markup
    that fetches /v1/debug/profile with the secret taken from the URL
    *fragment*. A fragment is never sent to a server, so it cannot end up in an
    access log or a proxy's history the way `?secret=` would — which is the
    only reason this is two routes instead of one.

        http://<host>:8900/v1/debug/profile.html#<NANOBOT_DEBUG_SECRET>
    """
    return web.Response(text=_PROFILE_PANEL_HTML, content_type="text/html")


def _workspace_auth_error(request: web.Request) -> web.Response | None:
    """Gate /v1/workspace/* on NANOBOT_API_SECRET, or on the debug secret.

    These routes were open while the two doors either side of them were shut,
    and they are not the harmless file list the names suggest: ``list`` shows
    media, but ``files/{path}`` served **any** path under the workspace — cron
    jobs, `memory/`, and `sessions/*.jsonl`, which hold whole conversations
    including the agent's reasoning. Anything on the LAN could read them with
    one unauthenticated GET.

    Two credentials are accepted because two callers exist. HomeCore presents
    the per-instance API secret (it proxies media at ``/chat/download``), and
    an operator checking whether a config deploy actually landed has the debug
    secret — which already unlocks strictly more than this. Requiring a second
    credential for a lesser capability would only push people to pass the API
    secret around.

    Open only when NEITHER secret is set, matching ``_api_auth_error``: a
    deployment that has configured no credential at all keeps working exactly
    as before rather than losing its photos with no way to fix it.
    """
    api = os.environ.get("NANOBOT_API_SECRET", "").strip()
    debug = os.environ.get("NANOBOT_DEBUG_SECRET", "").strip()
    if not api and not debug:
        return None
    auth = (request.headers.get("Authorization") or "").strip()
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    for secret, provided in (
        (api, bearer or (request.headers.get("X-Nanobot-Auth") or "").strip()),
        (debug, (request.headers.get("X-Debug-Secret") or "").strip() or bearer),
    ):
        if secret and provided and hmac.compare_digest(provided, secret):
            return None
    logger.warning(
        "api: rejected an unauthenticated workspace request from {}",
        request.remote or "unknown",
    )
    return web.json_response({"error": "unauthorized"}, status=401)


def _workspace_path(agent_loop, file_path: str) -> Path | None:
    """*file_path* resolved inside the workspace, or None if it points outside.

    The old check was ``".." in file_path``, which reads like a traversal guard
    and is not one: ``workspace / "/etc/passwd"`` is ``/etc/passwd``, because
    joining an absolute path *replaces* the base. The route pattern is
    ``{path:.*}``, so ``GET /v1/workspace/files//etc/hostname`` walked straight
    out of the container's workspace and returned the file. Verified against a
    live instance before this was written.

    Resolving both sides also follows symlinks, so a link planted inside the
    workspace cannot point out of it either.
    """
    if not file_path:
        return None
    root = Path(agent_loop.workspace).resolve()
    try:
        full = (root / file_path).resolve()
    except OSError:  # bad bytes, a name too long, a loop of symlinks
        return None
    if full == root or root not in full.parents:
        return None
    return full


async def handle_workspace_list(request: web.Request) -> web.Response:
    """GET /v1/workspace/list — list files in workspace/media, newest first."""
    if denied := _workspace_auth_error(request):
        return denied
    agent_loop = request.app["agent_loop"]
    media_dir = agent_loop.workspace / "media"
    files = []
    if media_dir.exists():
        for f in media_dir.rglob("*"):
            if f.is_file():
                rel = f.relative_to(agent_loop.workspace)
                stat = f.stat()
                files.append({
                    "path": str(rel).replace("\\", "/"),
                    "size": stat.st_size,
                    "modified": int(stat.st_mtime),
                })
    files.sort(key=lambda x: x["modified"], reverse=True)
    return web.json_response({"files": files})


async def handle_workspace_delete(request: web.Request) -> web.Response:
    """DELETE /v1/workspace/files/{path} — delete a file from workspace/media."""
    if denied := _workspace_auth_error(request):
        return denied
    agent_loop = request.app["agent_loop"]
    file_path = request.match_info.get("path", "")
    full_path = _workspace_path(agent_loop, file_path)
    if full_path is None:
        return web.Response(status=400, text="Invalid path")
    if not file_path.startswith("media/"):
        return web.Response(status=403, text="Can only delete from media/")
    if not full_path.is_file():
        return web.Response(status=404, text="File not found")
    full_path.unlink()
    return web.json_response({"deleted": file_path})


async def handle_workspace_file(request: web.Request) -> web.Response:
    """GET /v1/workspace/files/{path} — serve a file from the agent workspace."""
    if denied := _workspace_auth_error(request):
        return denied
    agent_loop = request.app["agent_loop"]
    file_path = request.match_info.get("path", "")
    full_path = _workspace_path(agent_loop, file_path)
    if full_path is None:
        return web.Response(status=400, text="Invalid path")
    if not full_path.is_file():
        return web.Response(status=404, text="File not found")
    import mimetypes
    content_type = mimetypes.guess_type(str(full_path))[0] or "application/octet-stream"
    return web.FileResponse(full_path, headers={"Content-Type": content_type})


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    agent_loop, model_name: str = "nanobot", request_timeout: float = 120.0
) -> web.Application:
    """Create the aiohttp application.

    Args:
        agent_loop: An initialized AgentLoop instance.
        model_name: Model name reported in responses.
        request_timeout: Per-request timeout in seconds.
    """
    app = web.Application(client_max_size=20 * 1024 * 1024)  # 20MB for base64 images
    app["agent_loop"] = agent_loop
    app["model_name"] = model_name
    app["request_timeout"] = request_timeout
    app["session_locks"] = {}  # per-user locks, keyed by session_key
    # The streaming turns in flight, keyed by session_key, so `/v1/stop` has
    # something to cancel. Entries are removed as each turn ends.
    app["active_runs"] = {}

    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_post("/v1/stop", handle_stop)
    app.router.add_post("/v1/sessions/fork", handle_session_fork)
    app.router.add_post("/v1/subagents/cancel", handle_subagent_cancel)
    app.router.add_post("/v1/cron/action", handle_cron_action)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/debug/errors", handle_debug_errors)
    app.router.add_get("/v1/debug/shell-log", handle_debug_shell_log)
    app.router.add_get("/v1/debug/profile", handle_debug_profile)
    app.router.add_get("/v1/debug/profile.html", handle_debug_profile_panel)
    app.router.add_get("/v1/workspace/list", handle_workspace_list)
    app.router.add_get("/v1/workspace/files/{path:.*}", handle_workspace_file)
    app.router.add_delete("/v1/workspace/files/{path:.*}", handle_workspace_delete)
    return app
