"""household: the fork's narrow 400 retries on OpenAI-compatible providers (D1).

A 400 is usually wrong the same way twice, so upstream does not retry it.
The fork had three exceptions, because each one changes nothing the person
chose and each one made a whole model unreachable without it:

- **effort refusal**: NanoGPT names the accepted ``reasoning_effort`` levels
  in its refusal ("Supported values are: none, high"); the nearest accepted
  level is remembered per model and sent instead (ties go up);
- **temperature refusal** ("only 1 is allowed for this model"): the model is
  remembered as taking no temperature and the request re-sent without it;
- **streaming only** (Together: "This model only supports streaming"): the
  non-streamed call is re-sent down the streaming path. Without it the
  household's Together fallback was unreachable during an outage.

Called from ``OpenAICompatProvider.chat``/``chat_stream`` (the same thin
wrappers that settle a reasoning echo). Errors only: a good reply is never
re-sent.
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Awaitable, Callable

from loguru import logger

NO_TEMPERATURE: set[str] = set()
EFFORTS_ACCEPTED: dict[str, tuple[str, ...]] = {}
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def accepted_efforts(text: str | None) -> tuple[str, ...] | None:
    text = text or ""
    if "reasoning_effort" not in text:
        return None
    m = re.search(r"[Ss]upported values are:?\s*([a-z][a-z ,]*)", text)
    if not m:
        return None
    levels = tuple(v.strip() for v in m.group(1).split(",") if v.strip())
    return levels or None


def nearest_effort(want: str, accepted: tuple[str, ...]) -> str:
    if want in accepted:
        return want
    order = EFFORT_ORDER
    at = order.index(want) if want in order else order.index("low")
    known = [a for a in accepted if a in order] or list(accepted)
    return min(known, key=lambda a: (abs(order.index(a) - at) if a in order else 99,
                                     -(order.index(a) if a in order else 0)))


def rejected_temperature(text: str | None) -> bool:
    text = (text or "").lower()
    return "temperature" in text and (
        "only 1" in text or "not support" in text or "unsupported" in text
        or "invalid" in text or "does not support" in text)


def requires_streaming(text: str | None) -> bool:
    text = (text or "").lower()
    return "stream" in text and (
        "only supports streaming" in text
        or "only support streaming" in text
        or ('"stream": true' in text or "'stream': true" in text)
        or ("must" in text and "stream" in text and "true" in text))


def refuses_temperature(model_name: str | None) -> bool:
    return bool(model_name) and model_name.lower() in NO_TEMPERATURE


def _bind(fn: Callable[..., Any], messages: Any, args: tuple, kwargs: dict) -> dict[str, Any]:
    bound = inspect.signature(fn).bind(messages, *args, **kwargs)
    return dict(bound.arguments)


async def call(provider: Any, *, streaming: bool, messages: Any, args: tuple,
               kwargs: dict) -> Any:
    inner: Callable[..., Awaitable[Any]] = (
        provider._chat_stream_unsettled if streaming else provider._chat_unsettled)
    call_kw = _bind(inner, messages, args, kwargs)
    model = call_kw.get("model") or provider.default_model or ""
    names = {model.lower(), provider._request_model_name(model).lower()}
    want = call_kw.get("reasoning_effort")
    for name in names:
        if want and name in EFFORTS_ACCEPTED:
            call_kw["reasoning_effort"] = nearest_effort(want, EFFORTS_ACCEPTED[name])
    response = await inner(**call_kw)
    if getattr(response, "finish_reason", None) != "error":
        return response
    text = response.content
    if not streaming and requires_streaming(text):
        logger.info("provider: {} answers only streamed; re-sending on the streaming path", model)
        stream_kw = _bind(provider._chat_stream_unsettled, call_kw.pop("messages"), (), call_kw)
        return await provider._chat_stream_unsettled(**stream_kw)
    accepted = accepted_efforts(text)
    if accepted and call_kw.get("reasoning_effort"):
        for name in names:
            EFFORTS_ACCEPTED[name] = accepted
        retry = nearest_effort(call_kw["reasoning_effort"], accepted)
        if retry != call_kw["reasoning_effort"]:
            logger.info("provider: {} accepts reasoning_effort {} only; sending {}",
                        model, ", ".join(accepted), retry)
            call_kw["reasoning_effort"] = retry
            return await inner(**call_kw)
    if rejected_temperature(text) and not (names & NO_TEMPERATURE):
        NO_TEMPERATURE.update(names)
        logger.info("provider: {} refuses temperature; retrying without it", model)
        return await inner(**call_kw)
    return response


__all__ = ["accepted_efforts", "call", "nearest_effort", "refuses_temperature",
           "rejected_temperature", "requires_streaming"]
