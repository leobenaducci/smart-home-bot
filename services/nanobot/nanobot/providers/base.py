"""Base LLM provider interface."""

import asyncio
import json
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar

from loguru import logger

from nanobot.utils.helpers import image_placeholder_text
from nanobot.utils.outage_store import SHARED_DETOURS, SHARED_OUTAGES
from nanobot.utils.profiling import PROFILER


@dataclass
class ToolCallRequest:
    """A tool call request from the LLM."""
    id: str
    name: str
    arguments: dict[str, Any]
    extra_content: dict[str, Any] | None = None
    provider_specific_fields: dict[str, Any] | None = None
    function_provider_specific_fields: dict[str, Any] | None = None

    def to_openai_tool_call(self) -> dict[str, Any]:
        """Serialize to an OpenAI-style tool_call payload."""
        tool_call = {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }
        if self.extra_content:
            tool_call["extra_content"] = self.extra_content
        if self.provider_specific_fields:
            tool_call["provider_specific_fields"] = self.provider_specific_fields
        if self.function_provider_specific_fields:
            tool_call["function"]["provider_specific_fields"] = self.function_provider_specific_fields
        return tool_call


@dataclass
class LLMResponse:
    """Response from an LLM provider."""
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    retry_after: float | None = None  # Provider supplied retry wait in seconds.
    reasoning_content: str | None = None  # Kimi, DeepSeek-R1, MiMo etc.
    thinking_blocks: list[dict] | None = None  # Anthropic extended thinking
    # Structured error metadata used by retry policy when finish_reason == "error".
    error_status_code: int | None = None
    error_kind: str | None = None  # e.g. "timeout", "connection"
    error_type: str | None = None  # Provider/type semantic, e.g. insufficient_quota.
    error_code: str | None = None  # Provider/code semantic, e.g. rate_limit_exceeded.
    error_retry_after_s: float | None = None
    error_should_retry: bool | None = None
    # Set by the runner when the model wrote something shaped like a skill
    # invocation that resolved to no call. The turn then ends as
    # `bad_invocation` rather than `completed`, which is what lets a stronger
    # model take it over -- see agent/classify.py.
    bad_invocation: bool = False
    # Set only when the answering model is not the one the caller asked for,
    # i.e. an outage fallback served the turn. Appended last so field order —
    # and therefore positional construction — is unchanged.
    served_by_model: str | None = None
    # What the *model* said, when we have relabelled the response as an error.
    # An answerless completion is our failure label over the provider's own
    # `length`, and the panel should still be able to say the model truncated:
    # that is the diagnosis, while "error" is only what we did about it.
    # Appended last, for the same reason as the field above.
    model_finish_reason: str | None = None

    @property
    def has_tool_calls(self) -> bool:
        """Check if response contains tool calls."""
        return len(self.tool_calls) > 0

    @property
    def should_execute_tools(self) -> bool:
        """Tools execute only when has_tool_calls AND finish_reason is ``tool_calls`` / ``stop``.
        Blocks gateway-injected calls under ``refusal`` / ``content_filter`` / ``error`` (#3220)."""
        if not self.has_tool_calls:
            return False
        return self.finish_reason in ("tool_calls", "stop")


@dataclass(frozen=True)
class GenerationSettings:
    """Default generation settings."""

    temperature: float = 0.7
    max_tokens: int = 4096
    reasoning_effort: str | None = None
    # Model, or ordered models, to try after the retry ladder is spent.
    # See _try_fallback_model.
    fallback_model: str | list[str] | None = None


_SYNTHETIC_USER_CONTENT = "(conversation continued)"


class LLMProvider(ABC):
    """Base class for LLM providers."""

    _CHAT_RETRY_DELAYS = (1, 2, 4)
    _PERSISTENT_MAX_DELAY = 60
    _PERSISTENT_IDENTICAL_ERROR_LIMIT = 10
    # Persistent mode used to leave the ladder *only* through the counter
    # above, and that counter compares error strings — it resets the moment a
    # gateway varies its body by a byte (a request id, a trace id, a
    # timestamp). Both deployed configs run `persistent`, so on a gateway that
    # stamps its 500s the outage fallback would never have fired at all. It
    # gets its own trigger here, one that counts attempts instead of comparing
    # text, and retrying continues afterwards if the fallback did not help —
    # which is what `persistent` promises.
    #
    # Three, not ten. Ten was measured on 2026-08-24 during a real luna outage:
    # eleven requests and 31.0s of waiting before the fallback was tried, on
    # the one turn per hour that pays for the discovery. The delays are 1, 2, 4
    # and then a doubling capped at 60, so most of that half-minute is the tail
    # — and waiting is the wrong instrument for the failure it is waiting on. A
    # model returning the same hard 5xx to every request is broken, not busy;
    # `_is_model_specific_error` already skips the ladder outright for the
    # errors that say so in words, and this is the same reasoning for the ones
    # that do not. The trade is bounded on the other side too: the fallback is
    # tried, not adopted. A blip that answers on the retry after it costs
    # nothing, and the window only sticks when the fallback actually answered
    # in the failing model's place.
    _PERSISTENT_FALLBACK_ATTEMPTS = 3
    # How long a model stays routed to its fallback once the fallback has been
    # seen to answer in its place. A vendor-side outage lasts hours, not one
    # turn, so re-proving the model dead on every call costs the whole ladder
    # again for nothing. See _mark_model_down.
    _MODEL_OUTAGE_COOLDOWN_S = 3600.0
    # A completion that carried no answer and called no tool. Not a provider
    # error -- the request succeeded -- so nothing downstream treated it as a
    # failure, and the family simply got silence.
    #
    # The shape that produced it: a reasoning model asked to do triage spends
    # its whole budget thinking, stops at the cap, and returns `content: ""`
    # with `finish_reason: "length"`. Measured on Qwen3.6-35B-A3B, that is what
    # both the default and "low" do. Turning thinking off is the fix for *that*
    # model; this is the floor under every model, because "the assistant said
    # nothing" is the one outcome a notification must never have.
    _ANSWERLESS = "the model returned no answer and called no tool"
    _RETRY_HEARTBEAT_CHUNK = 30
    _TRANSIENT_ERROR_MARKERS = (
        "429",
        "rate limit",
        "500",
        "502",
        "503",
        "504",
        "overloaded",
        "timeout",
        "timed out",
        "connection",
        "server error",
        "temporarily unavailable",
        "upstream request failed",
        "速率限制",
    )
    _RETRYABLE_STATUS_CODES = frozenset({408, 409, 429})
    # Permanent, but permanent about the *model* rather than the key — see
    # _is_model_specific_error.
    _MODEL_SPECIFIC_ERROR_STATUSES = frozenset({404})
    _MODEL_SPECIFIC_ERROR_MARKERS = (
        "model_not_found",
        "model not found",
        "unknown model",
        "no such model",
        "does not exist",
        "decommissioned",
        "has been deprecated",
        "has been retired",
        # A parameter this model will not accept. Same shape as a retired
        # model and the same remedy: retrying sends the identical request and
        # gets the identical refusal, while another model answers it fine.
        #
        # Measured 2026-09-02: `reasoning_effort: "none"` is required by
        # qwen3.6-35b-a3b and rejected by openai/gpt-oss-20b, which answers
        # 400 "Input validation error". The roles moved provider, the setting
        # did not, and every chore and geofence turn handed the family the raw
        # error dict instead of falling back to a model that would have
        # answered. Losing a notification is the expensive failure here.
        "input validation error",
        # A prompt this model cannot hold. Same shape again: the request is
        # not going to shrink on a retry, and a model with a bigger context
        # answers it unchanged.
        #
        # Measured 2026-09-02: FreeToken served triage at a 16k ceiling while
        # this household's own event prompts run 25.8k on average and 32.4k at
        # the largest, so every chore and geofence turn answered "prompt is
        # too long: 27852 tokens > 16384 maximum" -- and handed the family
        # that sentence as Alfred's reply. Raising the ceiling fixed this
        # house; the fallback is what stops the next one being a silent
        # outage, because a context that fits today stops fitting as a
        # session grows.
        "prompt is too long",
        "context length",
        "maximum context length",
        "reduce the length of the messages",
    )
    # The subset of the above that is about the *request* rather than about the
    # model. These still take the fallback -- see `_is_request_shaped_failure`
    # -- but must not open an outage window afterwards: a prompt that overflowed
    # a ceiling once is not evidence the model is down, and pinning the house
    # to the fallback for an hour on the strength of one long session is a
    # bigger outage than the one it is reacting to.
    _REQUEST_SHAPED_ERROR_MARKERS = (
        "prompt is too long",
        "context length",
        "maximum context length",
        "reduce the length of the messages",
    )
    _TRANSIENT_ERROR_KINDS = frozenset({"timeout", "connection"})
    _NON_RETRYABLE_429_ERROR_TOKENS = frozenset({
        "insufficient_quota",
        "quota_exceeded",
        "quota_exhausted",
        "billing_hard_limit_reached",
        "insufficient_balance",
        "credit_balance_too_low",
        "billing_not_active",
        "payment_required",
    })
    _RETRYABLE_429_ERROR_TOKENS = frozenset({
        "rate_limit_exceeded",
        "rate_limit_error",
        "too_many_requests",
        "request_limit_exceeded",
        "requests_limit_exceeded",
        "overloaded_error",
    })
    _NON_RETRYABLE_429_TEXT_MARKERS = (
        "insufficient_quota",
        "insufficient quota",
        "quota exceeded",
        "quota exhausted",
        "billing hard limit",
        "billing_hard_limit_reached",
        "billing not active",
        "insufficient balance",
        "insufficient_balance",
        "credit balance too low",
        "payment required",
        "out of credits",
        "out of quota",
        "exceeded your current quota",
    )
    _RETRYABLE_429_TEXT_MARKERS = (
        "rate limit",
        "rate_limit",
        "too many requests",
        "retry after",
        "try again in",
        "temporarily unavailable",
        "overloaded",
        "concurrency limit",
        "速率限制",
    )

    _SENTINEL = object()

    # Model name -> monotonic deadline until which it is presumed down, keyed
    # per gateway rather than per instance. See _model_down_until.
    _OUTAGE_WINDOWS: ClassVar[dict[str, dict[str, float]]] = {}

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        self.api_key = api_key
        self.api_base = api_base
        self.generation: GenerationSettings = GenerationSettings()

    @property
    def _model_down_until(self) -> dict[str, float]:
        """Models presumed down at this gateway, to their monotonic deadlines.

        Shared by every provider instance pointing at the same `api_base`,
        because "gpt-5.6-luna is 500ing at opencode" is a fact about somebody
        else's server and not about which of our objects noticed. There is one
        object per route — cli/commands.py builds a separate provider for the
        main model, for sub-agents, for `powerful` and for each `modelProfiles`
        entry — and luna is the model on four of them here. Instance-local, the
        outage was re-discovered four times: every route paid the full ladder
        (ten requests and ~31s in the deployed `persistent` mode, per LLM call,
        and a turn makes one per tool round trip) before swapping, and until it
        did, its turns were labelled with the model that was not answering.
        First route to learn it now covers the rest.

        Per gateway and not global: a model name means nothing without the
        host serving it, and `qwen3-vl:8b` being down on `ollama.home` says
        nothing about anything at opencode. A provider with no `api_base` —
        a test double, an SDK-configured client — keeps its own window, which
        is the old behaviour and the safe reading of "gateway unknown".

        Providers are built once at startup and held for the life of the
        process, and every call runs on the one asyncio loop, so this needs no
        lock and no expiry sweep: `_model_is_down` drops entries as it reads
        them.
        """
        base = (self.api_base or "").rstrip("/")
        if not base:
            return self.__dict__.setdefault("_local_model_down_until", {})
        return LLMProvider._OUTAGE_WINDOWS.setdefault(base, {})

    @staticmethod
    def _sanitize_empty_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Sanitize message content: fix empty blocks, strip internal _meta fields."""
        result: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content")

            if isinstance(content, str) and not content:
                clean = dict(msg)
                clean["content"] = None if (msg.get("role") == "assistant" and msg.get("tool_calls")) else "(empty)"
                result.append(clean)
                continue

            if isinstance(content, list):
                new_items: list[Any] = []
                changed = False
                for item in content:
                    if (
                        isinstance(item, dict)
                        and item.get("type") in ("text", "input_text", "output_text")
                        and not item.get("text")
                    ):
                        changed = True
                        continue
                    if isinstance(item, dict) and "_meta" in item:
                        new_items.append({k: v for k, v in item.items() if k != "_meta"})
                        changed = True
                    else:
                        new_items.append(item)
                if changed:
                    clean = dict(msg)
                    if new_items:
                        clean["content"] = new_items
                    elif msg.get("role") == "assistant" and msg.get("tool_calls"):
                        clean["content"] = None
                    else:
                        clean["content"] = "(empty)"
                    result.append(clean)
                    continue

            if isinstance(content, dict):
                clean = dict(msg)
                clean["content"] = [content]
                result.append(clean)
                continue

            result.append(msg)
        return result

    @staticmethod
    def _tool_name(tool: dict[str, Any]) -> str:
        """Extract tool name from either OpenAI or Anthropic-style tool schemas."""
        name = tool.get("name")
        if isinstance(name, str):
            return name
        fn = tool.get("function")
        if isinstance(fn, dict):
            fname = fn.get("name")
            if isinstance(fname, str):
                return fname
        return ""

    @classmethod
    def _tool_cache_marker_indices(cls, tools: list[dict[str, Any]]) -> list[int]:
        """Return cache marker indices: builtin/MCP boundary and tail index."""
        if not tools:
            return []

        tail_idx = len(tools) - 1
        last_builtin_idx: int | None = None
        for i in range(tail_idx, -1, -1):
            if not cls._tool_name(tools[i]).startswith("mcp_"):
                last_builtin_idx = i
                break

        ordered_unique: list[int] = []
        for idx in (last_builtin_idx, tail_idx):
            if idx is not None and idx not in ordered_unique:
                ordered_unique.append(idx)
        return ordered_unique

    @staticmethod
    def _sanitize_request_messages(
        messages: list[dict[str, Any]],
        allowed_keys: frozenset[str],
    ) -> list[dict[str, Any]]:
        """Keep only provider-safe message keys and normalize assistant content."""
        sanitized = []
        for msg in messages:
            clean = {k: v for k, v in msg.items() if k in allowed_keys}
            if clean.get("role") == "assistant" and "content" not in clean:
                clean["content"] = None
            sanitized.append(clean)
        return sanitized

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        """
        Send a chat completion request.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions.
            model: Model identifier (provider-specific).
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
            tool_choice: Tool selection strategy ("auto", "required", or specific tool dict).

        Returns:
            LLMResponse with content and/or tool calls.
        """
        pass

    @classmethod
    def _is_transient_error(cls, content: str | None) -> bool:
        err = (content or "").lower()
        return any(marker in err for marker in cls._TRANSIENT_ERROR_MARKERS)

    @classmethod
    def _is_model_specific_error(cls, response: LLMResponse) -> bool:
        """True when the error is about *this model*, not about the key or the gateway.

        Deliberately narrower than "not transient". A 401, a revoked key or a
        spent quota says the same thing to every model on the account, so
        swapping models there buys nothing and costs a request — which is what
        `test_a_permanent_error_does_not_spend_a_call_on_the_fallback` pins. A
        model that was retired, renamed or never existed is the opposite case:
        no amount of retrying fixes it, and another model on the same key
        answers fine. That is the most literal form of "one dead model", so it
        gets the fallback even though the retry ladder would be pointless.
        """
        if response.error_kind == "answerless":
            return True
        if response.error_status_code in cls._MODEL_SPECIFIC_ERROR_STATUSES:
            return True
        text = (response.content or "").lower()
        return any(marker in text for marker in cls._MODEL_SPECIFIC_ERROR_MARKERS)

    @classmethod
    def _is_request_shaped_failure(cls, response: LLMResponse) -> bool:
        """True when the refusal is about *this request*, not this model's health.

        Both halves of `_MODEL_SPECIFIC_ERROR_MARKERS` send the turn to the
        fallback, and both should: retrying an oversized prompt gets the same
        refusal, and a model with more room answers it unchanged. But only one
        half says anything about the model *afterwards*. A retired model is
        still retired on the next turn; a prompt that overflowed a 16k ceiling
        says nothing at all about the next turn, which is usually a fresh
        session an eighth of the size.

        The distinction matters because a successful fallback calls
        `_mark_model_down`, which pins this route -- and, through
        SHARED_OUTAGES, every other container on the same gateway -- to the
        fallback for a whole hour. One long session would take the household's
        chosen model out from under every short turn in the house.
        """
        text = (response.content or "").lower()
        return any(marker in text for marker in cls._REQUEST_SHAPED_ERROR_MARKERS)

    @classmethod
    def _is_transient_response(cls, response: LLMResponse) -> bool:
        """Prefer structured error metadata, fallback to text markers for legacy providers."""
        if response.error_should_retry is not None:
            return bool(response.error_should_retry)

        if response.error_status_code is not None:
            status = int(response.error_status_code)
            if status == 429:
                return cls._is_retryable_429_response(response)
            if status in cls._RETRYABLE_STATUS_CODES or status >= 500:
                return True
            if 400 <= status < 500:
                # A 4xx that is not 408/409/429 will say the same thing forever,
                # and this has to win over the text markers below — otherwise a
                # gateway that wraps its errors decides the question with its
                # own prose.
                #
                # opencode's gateway prefixes *every* failure, permanent ones
                # included, with "Upstream request failed", which is in
                # _TRANSIENT_ERROR_MARKERS. So a 400 was retried ten times over
                # 42 seconds and then handed to the family as a raw error dict.
                # Worse, `_run_with_retry` only strips images and retries for
                # errors it considers *non*-transient — so the one fallback
                # that fixes "unknown variant `image_url`" could never run on
                # this gateway, which is the one the whole house uses.
                return False

        kind = (response.error_kind or "").strip().lower()
        if kind in cls._TRANSIENT_ERROR_KINDS:
            return True

        return cls._is_transient_error(response.content)

    @staticmethod
    def _normalize_error_token(value: Any) -> str | None:
        if value is None:
            return None
        token = str(value).strip().lower()
        return token or None

    @classmethod
    def _extract_error_type_code(cls, payload: Any) -> tuple[str | None, str | None]:
        data: dict[str, Any] | None = None
        if isinstance(payload, dict):
            data = payload
        elif isinstance(payload, str):
            text = payload.strip()
            if text:
                try:
                    parsed = json.loads(text)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    data = parsed
        if not isinstance(data, dict):
            return None, None

        error_obj = data.get("error")
        type_value = data.get("type")
        code_value = data.get("code")
        if isinstance(error_obj, dict):
            type_value = error_obj.get("type") or type_value
            code_value = error_obj.get("code") or code_value

        return cls._normalize_error_token(type_value), cls._normalize_error_token(code_value)

    @classmethod
    def _is_retryable_429_response(cls, response: LLMResponse) -> bool:
        type_token = cls._normalize_error_token(response.error_type)
        code_token = cls._normalize_error_token(response.error_code)
        semantic_tokens = {
            token for token in (type_token, code_token)
            if token is not None
        }
        if any(token in cls._NON_RETRYABLE_429_ERROR_TOKENS for token in semantic_tokens):
            return False

        content = (response.content or "").lower()
        if any(marker in content for marker in cls._NON_RETRYABLE_429_TEXT_MARKERS):
            return False

        if any(token in cls._RETRYABLE_429_ERROR_TOKENS for token in semantic_tokens):
            return True
        if any(marker in content for marker in cls._RETRYABLE_429_TEXT_MARKERS):
            return True
        # Unknown 429 defaults to WAIT+retry.
        return True

    @staticmethod
    def _enforce_role_alternation(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Merge consecutive same-role messages and drop trailing assistant messages.

        Some providers (OpenAI-compat, Azure, vLLM, Ollama, etc.) reject requests
        where the last message is 'assistant' (prefill not supported) or two
        consecutive non-system messages share the same role.
        """
        if not messages:
            return messages

        merged: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            if (
                merged
                and role != "system"
                and role not in ("tool",)
                and merged[-1].get("role") == role
                and role in ("user", "assistant")
            ):
                prev = merged[-1]
                if role == "assistant":
                    prev_has_tools = bool(prev.get("tool_calls"))
                    curr_has_tools = bool(msg.get("tool_calls"))
                    if curr_has_tools:
                        merged[-1] = dict(msg)
                        continue
                    if prev_has_tools:
                        continue
                prev_content = prev.get("content") or ""
                curr_content = msg.get("content") or ""
                if isinstance(prev_content, str) and isinstance(curr_content, str):
                    prev["content"] = (prev_content + "\n\n" + curr_content).strip()
                else:
                    merged[-1] = dict(msg)
            else:
                merged.append(dict(msg))

        last_popped = None
        while merged and merged[-1].get("role") == "assistant":
            last_popped = merged.pop()

        # If removing trailing assistant messages left only system messages,
        # the request would be invalid for most providers (e.g. Zhipu/GLM
        # error 1214).  Recover by converting the last popped assistant
        # message to a user message so the LLM can still see the content.
        if (
            merged
            and last_popped is not None
            and not any(m.get("role") in ("user", "tool") for m in merged)
        ):
            recovered = dict(last_popped)
            recovered["role"] = "user"
            merged.append(recovered)

        # Safety net: ensure the first non-system message is not a bare
        # ``assistant`` message.  Providers like GLM reject system→assistant
        # with error 1214.  This can happen when upstream truncation (e.g.
        # _snip_history) drops the only user message.  Insert a synthetic
        # user message to keep the sequence valid.
        for i, msg in enumerate(merged):
            if msg.get("role") != "system":
                if msg.get("role") == "assistant" and not msg.get("tool_calls"):
                    merged.insert(i, {"role": "user", "content": _SYNTHETIC_USER_CONTENT})
                break

        return merged

    @staticmethod
    def _strip_image_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """Replace image_url blocks with text placeholder. Returns None if no images found."""
        found = False
        result = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                new_content = []
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "image_url":
                        path = (b.get("_meta") or {}).get("path", "")
                        placeholder = image_placeholder_text(path, empty="[image omitted]")
                        new_content.append({"type": "text", "text": placeholder})
                        found = True
                    else:
                        new_content.append(b)
                result.append({**msg, "content": new_content})
            else:
                result.append(msg)
        return result if found else None

    @staticmethod
    def _strip_image_content_inplace(messages: list[dict[str, Any]]) -> bool:
        """Replace image_url blocks with text placeholder *in-place*.

        Mutates the content lists of the original message dicts so that
        callers holding references to those dicts also see the stripped
        version.
        """
        found = False
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                for i, b in enumerate(content):
                    if isinstance(b, dict) and b.get("type") == "image_url":
                        path = (b.get("_meta") or {}).get("path", "")
                        placeholder = image_placeholder_text(path, empty="[image omitted]")
                        content[i] = {"type": "text", "text": placeholder}
                        found = True
        return found

    async def _safe_chat(self, **kwargs: Any) -> LLMResponse:
        """Call chat() and convert unexpected exceptions to error responses."""
        try:
            return await self.chat(**kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return LLMResponse(content=f"Error calling LLM: {exc}", finish_reason="error")

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """Stream a chat completion, calling *on_content_delta* for each text chunk.

        *on_reasoning_delta* receives the model's thinking as it arrives. It is
        liveness, not content: a thinking model can deliberate for a minute
        before its first visible token, and a caller with no way to see that is
        forced to treat "working" and "hung" as the same thing. Providers with
        no separate thinking channel simply never call it.

        Returns the same ``LLMResponse`` as :meth:`chat`.  The default
        implementation falls back to a non-streaming call and delivers the
        full content as a single delta.  Providers that support native
        streaming should override this method.
        """
        response = await self.chat(
            messages=messages, tools=tools, model=model,
            max_tokens=max_tokens, temperature=temperature,
            reasoning_effort=reasoning_effort, tool_choice=tool_choice,
        )
        if on_content_delta and response.content:
            await on_content_delta(response.content)
        return response

    async def _safe_chat_stream(self, **kwargs: Any) -> LLMResponse:
        """Call chat_stream() and convert unexpected exceptions to error responses."""
        try:
            return await self.chat_stream(**kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return LLMResponse(content=f"Error calling LLM: {exc}", finish_reason="error")

    async def chat_stream_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = _SENTINEL,
        temperature: object = _SENTINEL,
        reasoning_effort: object = _SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
        retry_mode: str = "standard",
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """Call chat_stream() with retry on transient provider failures."""
        if max_tokens is self._SENTINEL or max_tokens is None:
            max_tokens = self.generation.max_tokens
        if temperature is self._SENTINEL or temperature is None:
            temperature = self.generation.temperature
        if reasoning_effort is self._SENTINEL:
            reasoning_effort = self.generation.reasoning_effort

        kw: dict[str, Any] = dict(
            messages=messages, tools=tools, model=model,
            max_tokens=max_tokens, temperature=temperature,
            reasoning_effort=reasoning_effort, tool_choice=tool_choice,
            on_content_delta=on_content_delta,
            on_reasoning_delta=on_reasoning_delta,
        )
        return await self._run_with_retry(
            self._safe_chat_stream,
            kw,
            messages,
            retry_mode=retry_mode,
            on_retry_wait=on_retry_wait,
        )

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = _SENTINEL,
        temperature: object = _SENTINEL,
        reasoning_effort: object = _SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
        retry_mode: str = "standard",
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """Call chat() with retry on transient provider failures.

        Parameters default to ``self.generation`` when not explicitly passed,
        so callers no longer need to thread temperature / max_tokens /
        reasoning_effort through every layer. Explicit ``None`` is also
        normalized to the provider's generation defaults so that downstream
        ``_build_kwargs`` never sees ``None`` for ``max_tokens`` / ``temperature``
        (which would crash ``max(1, max_tokens)``).
        """
        if max_tokens is self._SENTINEL or max_tokens is None:
            max_tokens = self.generation.max_tokens
        if temperature is self._SENTINEL or temperature is None:
            temperature = self.generation.temperature
        if reasoning_effort is self._SENTINEL:
            reasoning_effort = self.generation.reasoning_effort

        kw: dict[str, Any] = dict(
            messages=messages, tools=tools, model=model,
            max_tokens=max_tokens, temperature=temperature,
            reasoning_effort=reasoning_effort, tool_choice=tool_choice,
        )
        return await self._run_with_retry(
            self._safe_chat,
            kw,
            messages,
            retry_mode=retry_mode,
            on_retry_wait=on_retry_wait,
        )

    @classmethod
    def _extract_retry_after(cls, content: str | None) -> float | None:
        text = (content or "").lower()
        patterns = (
            r"retry after\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds|s|sec|secs|seconds|m|min|minutes)?",
            r"try again in\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds|s|sec|secs|seconds|m|min|minutes)",
            r"wait\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds|s|sec|secs|seconds|m|min|minutes)\s*before retry",
            r"retry[_-]?after[\"'\s:=]+(\d+(?:\.\d+)?)",
        )
        for idx, pattern in enumerate(patterns):
            match = re.search(pattern, text)
            if not match:
                continue
            value = float(match.group(1))
            unit = match.group(2) if idx < 3 else "s"
            return cls._to_retry_seconds(value, unit)
        return None

    @classmethod
    def _to_retry_seconds(cls, value: float, unit: str | None = None) -> float:
        normalized_unit = (unit or "s").lower()
        if normalized_unit in {"ms", "milliseconds"}:
            return max(0.1, value / 1000.0)
        if normalized_unit in {"m", "min", "minutes"}:
            return max(0.1, value * 60.0)
        return max(0.1, value)

    @classmethod
    def _extract_retry_after_from_headers(cls, headers: Any) -> float | None:
        if not headers:
            return None

        def _header_value(name: str) -> Any:
            if hasattr(headers, "get"):
                value = headers.get(name) or headers.get(name.title())
                if value is not None:
                    return value
            if isinstance(headers, dict):
                for key, value in headers.items():
                    if isinstance(key, str) and key.lower() == name.lower():
                        return value
            return None

        try:
            retry_ms = _header_value("retry-after-ms")
            if retry_ms is not None:
                value = float(retry_ms) / 1000.0
                if value > 0:
                    return value
        except (TypeError, ValueError):
            pass

        retry_after = _header_value("retry-after")
        if retry_after is None:
            return None
        retry_after_text = str(retry_after).strip()
        if not retry_after_text:
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?", retry_after_text):
            return cls._to_retry_seconds(float(retry_after_text), "s")
        try:
            retry_at = parsedate_to_datetime(retry_after_text)
        except Exception:
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        remaining = (retry_at - datetime.now(retry_at.tzinfo)).total_seconds()
        return max(0.1, remaining)

    @classmethod
    def _extract_retry_after_from_response(cls, response: LLMResponse) -> float | None:
        if response.error_retry_after_s is not None and response.error_retry_after_s > 0:
            return response.error_retry_after_s
        if response.retry_after is not None and response.retry_after > 0:
            return response.retry_after
        return cls._extract_retry_after(response.content)

    async def _sleep_with_heartbeat(
        self,
        delay: float,
        *,
        attempt: int,
        persistent: bool,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        remaining = max(0.0, delay)
        while remaining > 0:
            if on_retry_wait:
                kind = "persistent retry" if persistent else "retry"
                await on_retry_wait(
                    f"Model request failed, {kind} in {max(1, int(round(remaining)))}s "
                    f"(attempt {attempt})."
                )
            chunk = min(remaining, self._RETRY_HEARTBEAT_CHUNK)
            await asyncio.sleep(chunk)
            remaining -= chunk

    @staticmethod
    def _model_identity(model: str | None, provider: str | None = None) -> str:
        """Compare model names the way the gateway will see them.

        Six registry specs set ``strip_model_prefix``, so ``_build_kwargs``
        sends ``model_name.split("/")[-1]`` and ``openrouter/claude-opus-4-5``
        reaches the wire as the same model as ``claude-opus-4-5``. Comparing
        the configured spellings would let a fallback that *is* the failing
        model past the guard and spend one more request on it, and would file
        one outage under two different health keys.

        `provider` qualifies it, and matters as soon as a fallback may live
        somewhere else: the same weights are served under the same name by more
        than one host. `deepseek-ai/DeepSeek-V4-Flash` on a hosted gateway and
        on a box in the house are the same string and emphatically not the same
        health. Filing them together would mark the local one down because the
        hosted one was, and route around the rescue that was working.

        Unqualified stays unqualified: every existing key, and every same-
        provider comparison, is unchanged.
        """
        name = (model or "").split("/")[-1].strip().lower()
        return f"{provider.strip().lower()}:{name}" if provider else name

    def _fallback_entries(self) -> list[tuple[str, str | None]]:
        """The configured fallback as (model, provider), in order.

        `modelFallback` takes a name, an ordered list of them, or an entry
        written `{"model": ..., "provider": ...}` naming another provider
        entirely. A bare name means *this* provider, which is what every
        existing config says and what it has always meant.

        Cross-provider entries exist because a same-provider fallback cannot
        rescue the failure people actually have. It was built for one dead
        model behind a healthy gateway -- which is real, and is what happened
        on 2026-08-23 -- but a provider that is down takes every model on it,
        and then a second name on the same key is a second identical failure.
        A household running one local engine has the sharpest version of this:
        it serves one model, so there is no same-provider rescue to name.

        Everything that reads the setting goes through here, because the
        readers want different halves and one of them silently took a list
        where it expected a string: `_model_identity` called `.split` on it and
        the whole rescue path died with an AttributeError -- during an outage,
        which is the only time it runs.
        """
        value = self.generation.fallback_model
        if isinstance(value, (str, dict)):
            value = [value]
        out: list[tuple[str, str | None]] = []
        for item in value or []:
            if isinstance(item, dict):
                model = str(item.get("model") or "").strip()
                provider = str(item.get("provider") or "").strip() or None
            else:
                model, provider = str(item or "").strip(), None
            if model:
                out.append((model, provider))
        return out

    def _fallback_candidates(self) -> list[str]:
        """Just the model names. Kept for callers comparing names only."""
        return [model for model, _ in self._fallback_entries()]

    def _sibling(self, provider: str, model: str):
        """Another configured provider, built once and reused.

        None when this process cannot build one -- an unconfigured name, or a
        provider constructed outside the factory (every test double). The
        caller treats that as "this candidate cannot help" and walks on, which
        is the same thing it does for a candidate that answers badly.
        """
        factory = getattr(self, "sibling_for", None)
        if not callable(factory):
            return None
        cache = self.__dict__.setdefault("_sibling_cache", {})
        if provider not in cache:
            try:
                cache[provider] = factory(provider, model)
            except Exception as exc:  # noqa: BLE001 - a bad block must not break the rescue
                logger.warning("Cannot build fallback provider {}: {}", provider, exc)
                cache[provider] = None
        return cache[provider]

    def _requested_model(self, kw: dict[str, Any]) -> str:
        """The model a call will actually reach.

        ``kw["model"]`` is None whenever the caller wants the provider default,
        which is the case for an ordinary chat turn. It has to be resolved
        before it can be compared against the fallback or used as a health key,
        or a default that already equals the fallback retries itself for no
        reason and an outage is remembered under two different names.
        """
        return kw.get("model") or self.get_default_model()

    def _model_is_down(self, model: str, provider: str | None = None) -> bool:
        """True while *model* is inside the outage window opened for it.

        Looks in three places, cheapest first: this instance's window, then the
        one shared by every instance in this process, then the file every
        *container* on the box shares. The last is what stops five family
        instances and casa each paying the ladder for the same failure on
        somebody else's server — measured at eleven requests and 31s, once per
        container, before it existed.
        """
        key = self._model_identity(model, provider)
        until = self._model_down_until.get(key)
        if until is not None:
            if time.monotonic() < until:
                return True
            # Expiry is a comparison, not a reaper: the entry is dropped the
            # next time anyone asks, and the model gets a fresh chance. If it
            # is still dead the next exhausted ladder re-opens the window.
            del self._model_down_until[key]
        remaining = self._shared_outage_remaining(key)
        if remaining <= 0:
            return False
        # Adopted into the local window, so the rest of this process answers
        # from memory rather than stat-ing the file for every call. Converted
        # to monotonic on the way in: the file speaks wall clock because that
        # is the only clock two containers agree on, and everything in here
        # speaks monotonic because an NTP step must not unstick a window.
        self._model_down_until[key] = time.monotonic() + remaining
        logger.info(
            "Model {} is already known down at {} by another instance; "
            "routing to the fallback for the next {}s",
            model, self.api_base or "this provider", int(remaining),
        )
        return True

    def _shared_outage_remaining(self, model_identity: str) -> float:
        """Seconds left on a window some other container opened, or 0.0.

        Scoped by `api_base` exactly like `_OUTAGE_WINDOWS`: a model name means
        nothing without the host serving it, and a provider that cannot say
        which gateway it talks to shares nothing — the honest reading of
        "gateway unknown", and what keeps a test double out of the file.
        """
        base = (self.api_base or "").rstrip("/")
        if not base:
            return 0.0
        return SHARED_OUTAGES.remaining(base, model_identity)

    def _mark_model_down(self, model: str) -> None:
        """Route *model* to the fallback for the next _MODEL_OUTAGE_COOLDOWN_S.

        Called only once the fallback has actually answered in this model's
        place — that is what distinguishes "this model is broken while the key
        and the gateway are healthy" from "everything is down", and only the
        first justifies pinning the house to a different model. Monotonic, not
        wall clock: an NTP step must not unstick or freeze the window.
        """
        key = self._model_identity(model)
        self._model_down_until[key] = time.monotonic() + self._MODEL_OUTAGE_COOLDOWN_S
        # And out to the other containers, so the first route in the house to
        # learn this is the only one that pays for it.
        base = (self.api_base or "").rstrip("/")
        if base:
            SHARED_OUTAGES.mark(base, key, time.time() + self._MODEL_OUTAGE_COOLDOWN_S)
        logger.warning(
            "Model {} is presumed down at {}; every route on that gateway goes "
            "to the fallback for the next {}s",
            model,
            self.api_base or "this provider",
            int(self._MODEL_OUTAGE_COOLDOWN_S),
        )

    def serving_model(self, model: str | None = None) -> tuple[str, str | None]:
        """The model a call for *model* would reach if it were made right now.

        Returns ``(serving, displaced)``: *displaced* is the model that was
        asked for when an outage window has it routed to the fallback, and
        None on the ordinary path where the answer comes from the model named.

        Public because the routing is otherwise invisible from outside this
        class, and something had to be able to ask. The agent tells the family
        which model is answering them, and it used to read that off the
        configured roster — so during the 2026-08-23 outage it would have kept
        saying `gpt-5.6-luna` for the whole hour that `deepseek-v4-flash` was
        doing the answering. Read-only: asking cannot open or extend a window,
        and the one mutation underneath it is the lazy expiry `_model_is_down`
        already performs for every caller.
        """
        serving, _provider, displaced = self._serving_route(model)
        return serving, displaced

    def _serving_route(
        self, model: str | None = None
    ) -> tuple[str, str | None, str | None]:
        """`serving_model`, plus which provider is to answer.

        Split out rather than widening `serving_model`, whose two-tuple the
        agent, the CLI and three tests all read. The provider half is only
        needed by the code that has to *make* the call.
        """
        requested = self._requested_model({"model": model})
        # A server lent to a benchmark: its calls go where the detour says
        # until the deadline, without walking the rescue chain -- it is not
        # down, it is borrowed (utils/outage_store.DetourStore).
        detour = SHARED_DETOURS.target(self.api_base or "")
        if detour and detour["model"] != requested:
            return detour["model"], detour["provider"], requested
        here = self._model_identity(requested)
        # Never the model being rescued: routing it to itself is the retry
        # ladder again, and it could not be the displaced half of this pair.
        candidates = [e for e in self._fallback_entries()
                      if self._model_identity(e[0], e[1]) != here]
        if not candidates:
            return requested, None, None
        if not self._model_is_down(requested):
            return requested, None, None
        # The first candidate that is not itself inside an outage window.
        #
        # A chain exists because one rescue can share its fate with the model
        # it rescues, and `_try_fallback_model` walks past a dead first
        # candidate to the next one — but this decided the route from
        # `_fallback_candidates()[0]` alone. So after a turn rescued by the
        # *second* candidate, both the everyday model and the first candidate
        # were marked down and every later call was still sent straight at the
        # first one, paying its whole ladder before walking the chain again.
        # That is exactly the cost `_route_around_dead_model` exists to remove.
        #
        # All of them down falls back to the first: the window says nothing
        # about which of them recovered, and the chain is walked again anyway.
        for candidate, provider in candidates:
            if not self._model_is_down(candidate, provider):
                return candidate, provider, requested
        return candidates[0][0], candidates[0][1], requested

    def routed(self, model: str | None = None) -> tuple["LLMProvider", str]:
        """(provider, model) a call for *model* should go to right now.

        For callers that use `chat()` directly rather than the retrying
        wrapper -- the turn classifier -- so an outage window or a detour
        (a server lent to a benchmark) reaches them too. Without it the
        classifier kept asking an unloaded text server, which loaded the model
        back in the middle of the benchmark that had freed its card.
        """
        serving, provider, displaced = self._serving_route(model)
        if displaced is None:
            return self, serving
        if provider:
            sibling = self._sibling(provider, serving)
            return (sibling, serving) if sibling is not None else (self, displaced)
        return self, serving

    def _route_around_dead_model(
        self, kw: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None, str | None]:
        """Swap the fallback in up front while the requested model is known down.

        Returns the kwargs to use and, when a swap happened, the model that was
        asked for — so the answer can still be labelled as a fallback turn.

        Without this the outage is re-discovered from scratch on every single
        call: the ladder runs again against a model already proven dead, which
        in the deployed `persistent` mode is ten more requests and ~31s of
        waiting before the same swap is made again — and an agent turn makes
        one call per tool round trip, so the cost is paid several times per
        answer, for every turn, for the whole outage. One turn learns it and
        the rest of the window goes straight to the model that works.

        The decision itself lives in `serving_model`, so what the agent tells
        the family about which model is answering and what actually gets called
        cannot drift apart — they are the same answer read twice.
        """
        serving, provider, displaced = self._serving_route(self._requested_model(kw))
        if displaced is None:
            return kw, None, None
        logger.info(
            "Model {} is inside its outage window, going straight to {}{}",
            displaced, serving, f" on {provider}" if provider else "",
        )
        return {**kw, "model": serving}, displaced, provider

    async def _try_fallback_model(
        self,
        call: Callable[..., Awaitable[LLMResponse]],
        kw: dict[str, Any],
        failed: LLMResponse,
    ) -> LLMResponse | None:
        """Try the configured fallback model once. None if unset or no help.

        A single model can be down while the gateway in front of it is healthy,
        and retrying is the wrong shape of fix for that. On 2026-08-23
        gpt-5.6-luna answered a hard 500 on every attempt while kimi-k3 and
        deepseek-v4-flash answered normally on the same key and base URL — the
        model was not busy, it was broken, so the delays only spent the house's
        patience before handing back the same error dict. Worse, luna was the
        main `model` and three `modelProfiles` on the family instances, and both
        `model` and `subagentModel` on the casa voice one — so a single dead
        model on somebody else's server took down every Alfred at once.

        This runs after the ladder above has established that waiting does not
        help, and once per call: if the fallback is erroring too, the caller
        gets the original error rather than a second one that explains less.
        When it *does* answer, the model it rescued is marked down so the rest
        of the outage skips the ladder entirely — see _route_around_dead_model.
        """
        current = self._requested_model(kw)
        here = self._model_identity(current)
        # Deduped, and never the model that just failed: a rescue that is the
        # thing being rescued is the retry ladder again, wearing a hat. The key
        # carries the provider, so the same model name on two hosts is two
        # candidates -- which is the whole point of naming another provider.
        seen: set = set()
        candidates = []
        for model, provider in self._fallback_entries():
            key = self._model_identity(model, provider)
            if key == here or key in seen:
                continue
            seen.add(key)
            candidates.append((model, provider))
        if not candidates:
            return None

        for index, (fallback, provider) in enumerate(candidates):
            result = await self._try_one_fallback(
                call, kw, failed, fallback, current, first=index == 0,
                provider=provider)
            if result is not None:
                return result
        return None

    async def _try_one_fallback(
        self,
        call: Callable[..., Awaitable[LLMResponse]],
        kw: dict[str, Any],
        failed: LLMResponse,
        fallback: str,
        current: str,
        first: bool,
        provider: str | None = None,
    ) -> LLMResponse | None:
        """One candidate. None when it did not rescue the call.

        Only the first candidate gets the retry ladder below, and that is a
        latency decision rather than an oversight. The ladder exists for the 429
        that this feature *creates*: during an outage every container and every
        route swings onto the same first fallback at the same moment. The
        candidates after it are not carrying that stampede, so a bounded wait
        there buys much less -- and the turn has already spent the main model's
        whole ladder plus this one. Somebody is waiting.
        """
        logger.warning(
            "Model {} still failing after retries, trying fallback {}{}: {}",
            current,
            fallback,
            f" on {provider}" if provider else "",
            # `error_kind` because an answerless turn has no content to quote:
            # it is a failure this side named, not one the provider reported.
            (failed.content or failed.error_kind or "")[:120],
        )
        if provider:
            # Another provider means another client: its own base URL, its own
            # key. Swapping the model name alone would post this candidate at
            # the host that is already failing, which is the failure the
            # cross-provider entry exists to escape.
            sibling = self._sibling(provider, fallback)
            if sibling is None:
                logger.warning(
                    "Fallback provider {} is not configured in this process; "
                    "skipping {}", provider, fallback)
                return None
            routed = getattr(sibling, getattr(call, "__name__", ""), None)
            if routed is None:
                # Distinct from the message above on purpose: that one sends
                # somebody to their config, and this one is a wrapper that lost
                # the method's name, which is a bug in here.
                logger.warning(
                    "Cannot route to {} on {}: no {!r} on that provider",
                    fallback, provider, getattr(call, "__name__", ""))
                return None
            call = routed
        fallback_kw = {**kw, "model": fallback}
        result = await call(**fallback_kw)
        # The fallback gets a ladder of its own, bounded and never persistent.
        #
        # During an outage every family container and casa, and each of their
        # subagent / powerful / Profesion routes, swings onto the same fallback
        # on the same key at the same moment — so a 429 there is the expected
        # consequence of this feature working, not a reason to give up. Handing
        # back the main model's 500 because the rescue was busy for one second
        # fails on exactly the load the rescue creates.
        #
        # Bounded because this is already the last resort: `persistent` would
        # hang a turn on the fallback forever, and the caller is better served
        # by the original error than by never being answered at all.
        for base_delay in (self._CHAT_RETRY_DELAYS if first else ()):
            if result.finish_reason != "error" or not self._is_transient_response(result):
                break
            wait = self._extract_retry_after_from_response(result) or base_delay
            logger.warning(
                "Fallback model {} returned a transient error, retrying in {}s: {}",
                fallback,
                int(round(wait)),
                (result.content or "")[:120],
            )
            await asyncio.sleep(wait)
            result = await call(**fallback_kw)
        # An empty body is not a rescue. The fallback spends reasoning tokens
        # where the main model spends none, so under a ceiling picked for a
        # non-reasoning model it can burn the whole budget thinking and return
        # `length` with nothing in it — which reaches the family as Alfred not
        # answering, with nothing in the log that looks like an error. Counting
        # that as success would also open the outage window on the strength of
        # a reply nobody received.
        rescued = result.finish_reason != "error" and bool(
            (result.content or "").strip() or result.tool_calls
        )
        if not rescued:
            # Says only what it knows. Whether the caller now gets the original
            # error or another candidate is the loop's decision, and this line
            # used to claim the first of those on the way to doing the second.
            logger.warning(
                "Fallback model {} did not help (finish_reason={}): {}",
                fallback,
                result.finish_reason,
                (result.content or "")[:120],
            )
            return None
        # ...but not when the refusal was about the request. The fallback
        # answered *this* prompt; the next one is a different size and belongs
        # back on the model the household chose.
        if not self._is_request_shaped_failure(failed):
            self._mark_model_down(current)
        logger.info("Fallback model {} answered in place of {}", fallback, current)
        result.served_by_model = fallback
        return result

    @staticmethod
    def _track_streamed_content(
        kw: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, bool]]:
        """Wrap ``on_content_delta`` so the ladder knows if anything was painted.

        Returns the kwargs to call with and a one-key dict the wrapper flips the
        first time the model emits a non-empty delta.
        """
        streamed = {"any": False}
        original = kw.get("on_content_delta")
        if original is None:
            return kw, streamed

        async def _counting(delta: str) -> None:
            if delta:
                streamed["any"] = True
            await original(delta)

        return {**kw, "on_content_delta": _counting}, streamed

    @staticmethod
    def _silenced_stream(kw: dict[str, Any]) -> dict[str, Any]:
        """The same call with the delta callbacks removed."""
        return {**kw, "on_content_delta": None, "on_reasoning_delta": None}

    async def _run_with_retry(
        self,
        call: Callable[..., Awaitable[LLMResponse]],
        kw: dict[str, Any],
        original_messages: list[dict[str, Any]],
        *,
        retry_mode: str,
        on_retry_wait: Callable[[str], Awaitable[None]] | None,
    ) -> LLMResponse:
        """Time the ladder, then run it — see `_run_with_retry_inner`.

        Every LLM request this process makes funnels through here: the turn,
        the sub-agent, describe_image, memory consolidation, the heartbeat. So
        it is the one place that can say what a call cost without every caller
        being taught to measure, and the only one that can separate the three
        numbers a slow turn hides — time the model took, time spent asleep
        between retries the model caused, and how many requests it took to get
        one answer. `counted` wraps the callable rather than the returns, of
        which there are eight; the fallback path gets it too, because it is
        handed the same wrapper.
        """
        if not PROFILER.enabled:
            return await self._run_with_retry_inner(
                call, kw, original_messages,
                retry_mode=retry_mode, on_retry_wait=on_retry_wait,
            )
        stats = {"attempts": 0, "request_s": 0.0}

        async def counted(**kwargs: Any) -> LLMResponse:
            stats["attempts"] += 1
            started = time.perf_counter()
            try:
                return await call(**kwargs)
            finally:
                stats["request_s"] += time.perf_counter() - started

        # The wrapper answers to the wrapped method's name, because a
        # cross-provider fallback finds the sibling's equivalent method with
        # `getattr(sibling, call.__name__)`. Unnamed, that lookup asked every
        # provider for a method called `counted`, found none, and reported the
        # provider as not configured -- so the rescue was silently disabled
        # whenever the profiler was on, which in the deployed containers is
        # always. It cost the house its fallback through a real outage while
        # every by-hand test passed, because those call the method directly.
        counted.__name__ = getattr(call, "__name__", "counted")

        requested = self._requested_model(kw)
        t0 = time.perf_counter()
        response: LLMResponse | None = None
        failure: str | None = None
        try:
            response = await self._run_with_retry_inner(
                counted, kw, original_messages,
                retry_mode=retry_mode, on_retry_wait=on_retry_wait,
            )
            return response
        except BaseException as exc:                        # noqa: BLE001 - re-raised
            failure = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            PROFILER.record_call(
                model=requested,
                served_by=response.served_by_model if response else None,
                api_base=self.api_base,
                duration_s=time.perf_counter() - t0,
                request_s=stats["request_s"],
                attempts=stats["attempts"],
                # The model's own reason where we have one: an answerless
                # completion is recorded as the `length` it actually was, not
                # as the `error` we relabelled it to.
                finish_reason=((response.model_finish_reason or response.finish_reason)
                               if response else "exception"),
                usage=response.usage if response else None,
                stream=bool(kw.get("on_content_delta")),
                # Not truncated here. `record_call` redacts and *then* cuts,
                # and its own comment says why: `redact()` replaces whole secret
                # values, so a credential echoed in a 401 body that straddles
                # offset 200 arrives as a fragment, matches nothing, and is kept
                # -- in a buffer /v1/debug/profile serves and the portal proxies
                # to a browser. Cutting first defeats the redaction that follows.
                error=failure or (
                    # An answerless turn carries no content by design -- see
                    # `_run_with_retry_inner`, where the label is kept out of
                    # anything a person reads -- so name it here rather than
                    # filing a blank error against it.
                    (response.content
                     or (self._ANSWERLESS
                         if response.error_kind == "answerless" else ""))
                    if response is not None and response.finish_reason == "error"
                    else None
                ),
                messages=len(kw.get("messages") or ()),
                content_chars=len(response.content or "") if response else 0,
                answerless=bool(response is not None
                                and response.error_kind == "answerless"),
                reasoning_chars=len(response.reasoning_content or "") if response else 0,
                tool_calls=len(response.tool_calls) if response else 0,
            )

    async def _run_with_retry_inner(
        self,
        call: Callable[..., Awaitable[LLMResponse]],
        kw: dict[str, Any],
        original_messages: list[dict[str, Any]],
        *,
        retry_mode: str,
        on_retry_wait: Callable[[str], Awaitable[None]] | None,
    ) -> LLMResponse:
        attempt = 0
        delays = list(self._CHAT_RETRY_DELAYS)
        persistent = retry_mode == "persistent"
        last_error_key: str | None = None
        identical_error_count = 0
        fallback_tried = False
        # A model already proven dead does not get the ladder again.
        kw, rerouted_from, rerouted_to = self._route_around_dead_model(kw)
        if rerouted_to:
            # The window says the requested model is down and the candidate
            # that answers lives somewhere else, so the call itself has to go
            # somewhere else. Swapping only `kw["model"]` would post another
            # provider's model name at the provider that is down -- a 404 on
            # top of an outage, once per call, for the whole window.
            sibling = self._sibling(rerouted_to, kw.get("model") or "")
            routed = getattr(sibling, call.__name__, None) if sibling else None
            if routed is not None:
                call = routed
            else:
                # Cannot reach that provider from here. Undo the swap rather
                # than send its model name to this one, and let the ordinary
                # ladder discover the outage again -- slower, but correct.
                kw, rerouted_from = {**kw, "model": rerouted_from}, None
        kw, streamed = self._track_streamed_content(kw)
        while True:
            attempt += 1
            response = await call(**kw)
            if streamed["any"]:
                # Every path below re-calls with these same kwargs, and the
                # callback paints straight into the reader's message — so a
                # model that streamed half an answer before dying would be
                # followed by a whole fresh one appended to it. Silenced from
                # here on, for the retries and the fallback alike: the answer
                # still arrives in the response, and `trim_to` at stream end is
                # what the reader is left with (agent/loop.py on_stream_end).
                # Losing the token-by-token paint on a turn that already failed
                # once is the cheaper half of that trade.
                kw = self._silenced_stream(kw)
            # Before the gate below, because that gate is what hands an empty
            # answer to the caller as a success. Empty *content* alone is not
            # enough: a turn that calls a tool and says nothing is normal and
            # common, and failing those would break every tool-using turn.
            # Narrowed to `length` deliberately. `runner.py` already recovers a
            # blank completion -- two retries, then a finalization prompt
            # ("Please provide your response…") -- and both are gated on
            # `finish_reason != "error"`, so relabelling *every* blank here
            # would step in front of a floor that already works, and a model
            # that goes blank once mid-turn would end in error on a household
            # with no fallback configured.
            #
            # `length` is the shape that recovery cannot help. The model did
            # not stumble, it ran out of budget -- on the case this exists for,
            # spending it all on reasoning -- so asking the same model the same
            # question again returns the same nothing, three times, and only
            # then reaches a fallback the family has been waiting on.
            if (response.finish_reason == "length"
                    and not (response.content or "").strip()
                    and not response.tool_calls):
                logger.warning(
                    "Model {} returned no answer (finish_reason={}); treating "
                    "it as a failure so the fallback can answer instead",
                    kw.get("model"), response.finish_reason)
                response = LLMResponse(
                    # Deliberately no content. `runner.py` renders an error
                    # turn as `clean or spec.error_message`, so whatever is put
                    # here is spoken to the family in Alfred's voice and pushed
                    # by HomeCore as the notification body -- and "the model
                    # returned no answer and called no tool" is a diagnosis,
                    # not a chore reminder. The label lives in the log line
                    # above and in the profiler record below.
                    content=None,
                    finish_reason="error",
                    usage=response.usage,
                    # Carried rather than dropped: this is precisely the turn
                    # where reasoning ate the whole budget, so the profiler's
                    # `reasoning_chars` is the evidence for the diagnosis.
                    # Rebuilding the response without it reports zero for the
                    # one case the field exists to explain.
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                    # Model-specific on purpose: re-asking the same model the
                    # same question returns the same empty answer, so the retry
                    # ladder would only spend the family's patience. This is
                    # "one dead model", and it takes the fallback directly.
                    error_kind="answerless",
                    model_finish_reason=response.finish_reason,
                )
            if response.finish_reason != "error":
                if rerouted_from is not None:
                    response.served_by_model = kw.get("model")
                return response
            error_key = ((response.content or "").strip().lower() or None)
            if error_key and error_key == last_error_key:
                identical_error_count += 1
            else:
                last_error_key = error_key
                identical_error_count = 1 if error_key else 0

            if not self._is_transient_response(response):
                # Never for an answerless turn. The images are not why the
                # model said nothing, so stripping them either spends a request
                # on the same silence or -- worse -- returns an answer about a
                # picture the model was no longer shown, which is then handed
                # back as the reply to a question about that picture.
                stripped = (None if response.error_kind == "answerless"
                            else self._strip_image_content(original_messages))
                if stripped is not None and stripped != kw["messages"]:
                    logger.warning(
                        "Non-transient LLM error with image content, retrying without images"
                    )
                    retry_kw = dict(kw)
                    retry_kw["messages"] = stripped
                    result = await call(**retry_kw)
                    if result.finish_reason != "error":
                        # Permanently strip images from the original messages so
                        # subsequent iterations do not repeat the error-retry cycle.
                        self._strip_image_content_inplace(original_messages)
                        return result
                    # The images were not the problem. Carry the post-strip
                    # error forward instead of returning it blind, so the
                    # model-specific check below still gets its turn.
                    response = result
                # `rerouted_from` here means this call was already the rescue:
                # the requested model is inside its outage window and we sent
                # the turn at the first fallback candidate instead. If *that*
                # one answers permanently -- `Model is disabled`, the 401 shape
                # -- there is nothing to wait for and the rest of the chain is
                # exactly what it is for. Without this the chain was walked only
                # on the turn that discovered the outage: every turn afterwards
                # went to the first candidate, got its 401, and handed the
                # family an error while a healthy third model sat unused for the
                # rest of the hour-long window.
                if self._is_model_specific_error(response) or rerouted_from is not None:
                    # Waiting cannot fix a model that was retired or renamed,
                    # but another model on the same key answers fine — so this
                    # skips the ladder and goes straight to the fallback.
                    fallback = await self._try_fallback_model(call, kw, response)
                    if fallback is not None:
                        return fallback
                return response

            ladder_spent = (
                identical_error_count >= self._PERSISTENT_IDENTICAL_ERROR_LIMIT
                if persistent
                else attempt > len(delays)
            )
            # Persistent mode's only exit compares error strings, so a gateway
            # that stamps a request id into its 500s would never reach the
            # fallback at all. Count attempts as well.
            #
            # `not fallback_tried` guards BOTH arms, and that is the whole
            # point of the flag. It used to sit inside the parenthesis only,
            # which was invisible while both constants were 10 -- the two arms
            # became true on the same attempt, so the rescue ran once, which is
            # what the docstring promises. Lowering
            # _PERSISTENT_FALLBACK_ATTEMPTS to 3 pulled them apart: the second
            # arm fires at attempt 3, then `ladder_spent` fires again at the
            # identical-error limit of 10 and runs the fallback's entire ladder
            # a second time. Measured on a double-failing provider: 18 requests,
            # 8 of them on the fallback, against 14 and 4 before -- so the
            # not-rescued case became *more* expensive, and during a real
            # outage, when every route in every container has swung onto that
            # one fallback, it doubles the load on it at the worst moment.
            if not fallback_tried and (
                ladder_spent
                or (persistent and attempt >= self._PERSISTENT_FALLBACK_ATTEMPTS)
            ):
                fallback_tried = True
                fallback = await self._try_fallback_model(call, kw, response)
                if fallback is not None:
                    return fallback

            if ladder_spent:
                # Announced only now. Channels drop `_retry_wait` messages
                # (channels/manager.py), so this is not what the family reads —
                # but the interactive CLI does surface it, and the log line
                # said "giving up" on a path that was about to try a fallback
                # and often succeed. Saying it after the attempt makes both
                # honest.
                if persistent:
                    logger.warning(
                        "Stopping persistent retry after {} identical transient errors: {}",
                        identical_error_count,
                        (response.content or "")[:120].lower(),
                    )
                    notice = (
                        f"Persistent retry stopped after {identical_error_count} identical errors."
                    )
                else:
                    logger.warning(
                        "LLM request failed after {} retries, giving up: {}",
                        attempt,
                        (response.content or "")[:120].lower(),
                    )
                    notice = f"Model request failed after {attempt} retries, giving up."
                if on_retry_wait:
                    await on_retry_wait(notice)
                return response

            base_delay = delays[min(attempt - 1, len(delays) - 1)]
            delay = self._extract_retry_after_from_response(response) or base_delay
            if persistent:
                delay = min(delay, self._PERSISTENT_MAX_DELAY)

            logger.warning(
                "LLM transient error (attempt {}{}), retrying in {}s: {}",
                attempt,
                "+" if persistent and attempt > len(delays) else f"/{len(delays)}",
                int(round(delay)),
                (response.content or "")[:120].lower(),
            )
            await self._sleep_with_heartbeat(
                delay,
                attempt=attempt,
                persistent=persistent,
                on_retry_wait=on_retry_wait,
            )

    @abstractmethod
    def get_default_model(self) -> str:
        """Get the default model for this provider."""
        pass
