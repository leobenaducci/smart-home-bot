"""household: outage windows per model, shared across the house (fork D1).

Upstream's ``FallbackProvider`` keeps one in-memory circuit per wrapper: three
consecutive failures of the primary skip it for 60 s, in this process only.
The fork learned an outage once for the whole house:

- a window per *model* at its gateway (base URL), not per wrapper, so every
  route reaching the same dead model skips it -- primary or fallback;
- an hour long (``MODEL_OUTAGE_COOLDOWN_S``): a vendor outage lasts hours;
- written to ``model-outages.json`` under ``NANOBOT_SHARED_STATE_DIR``
  (``household/outage_store.py``, verbatim), so the other members'
  containers skip it too.

``HouseholdFallbackProvider`` adds that around upstream's candidate walk: a
candidate inside a window answers a synthetic transient error at once, which
upstream's own rules treat as "try the next one". A candidate that failed
with an outage-shaped error opens its window as soon as another candidate
answers in its place (the fork's rule: a rescue is what tells a broken model
from everything being down), or after ``FAILURE_THRESHOLD`` such failures in a
row when nothing rescues it. When every candidate is inside a window the windows are ignored for
that request -- a slow answer beats none.

Detours (A11, fork ``_serving_route``): while the admin page lends a server's
card to a benchmark it writes ``model-detours.json`` (``DetourStore``), and a
call for that gateway goes to the detour's model and provider until the
deadline. It is decided, not discovered, so no window opens and the chain is
not walked; a failing detour target is treated like any other candidate.
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger

from nanobot.household.outage_store import SHARED_DETOURS, SHARED_OUTAGES
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.fallback_provider import FallbackProvider

MODEL_OUTAGE_COOLDOWN_S = 3600.0
FAILURE_THRESHOLD = 3

# gateway -> model -> monotonic deadline (this process); the shared store is
# the cross-process copy, in wall-clock time.
_LOCAL: dict[str, dict[str, float]] = {}
_FAILURES: dict[tuple[str, str], int] = {}


_NO_BASE = "provider:"  # a gateway with no base URL: windows stay in this process


def gateway_of(provider: Any) -> str:
    """The gateway half of a window's key, exactly as the fork wrote it (B5).

    The fork keyed the shared file by the provider's ``api_base`` with the
    trailing slash stripped and nothing else changed, and shared nothing for a
    provider without one. A mixed fleet (fork and trial containers) reads and
    writes one ``model-outages.json``, so the keys must match byte for byte.
    """
    base = getattr(provider, "api_base", None) or getattr(provider, "_effective_base", None) or ""
    base = str(base).rstrip("/")
    return base or _NO_BASE + str(getattr(provider, "provider_name", "") or "")


def model_key(model: str | None) -> str:
    """The model half, as the fork's ``LLMProvider._model_identity`` spelled it.

    The wire name after a ``vendor/`` prefix, lower-cased: six specs strip the
    prefix, so ``deepseek/DeepSeek-V4`` and ``deepseek-v4`` are one model and
    one outage."""
    return (model or "").split("/")[-1].strip().lower()


def _shared(gateway: str) -> bool:
    return bool(gateway) and not gateway.startswith(_NO_BASE)


def remaining(gateway: str, model: str) -> float:
    key = model_key(model)
    local = _LOCAL.get(gateway, {}).get(key)
    left = max(0.0, local - time.monotonic()) if local else 0.0
    if left or not _shared(gateway):
        return left
    return SHARED_OUTAGES.remaining(gateway, key)


def mark_down(gateway: str, model: str, seconds: float = MODEL_OUTAGE_COOLDOWN_S) -> None:
    key = model_key(model)
    _LOCAL.setdefault(gateway, {})[key] = time.monotonic() + seconds
    if _shared(gateway):
        SHARED_OUTAGES.mark(gateway, key, time.time() + seconds)
    logger.warning("outage: {} at {} presumed down for {:.0f} s", model, gateway, seconds)


def outage_shaped(response: LLMResponse) -> bool:
    """An error that says the model is unavailable, not that the request was wrong."""
    return (response.finish_reason == "error" and FallbackProvider._should_fallback(response)
            and response.error_kind not in ("authentication", "oauth_auth_required"))


def record(gateway: str, model: str, response: LLMResponse) -> None:
    key = (gateway, model_key(model))
    if response.finish_reason != "error":
        _FAILURES.pop(key, None)
        return
    if not FallbackProvider._should_fallback(response) or response.error_kind in (
        "authentication", "oauth_auth_required",
    ):
        return  # request-shaped or credential problems are not an outage of the model
    _FAILURES[key] = _FAILURES.get(key, 0) + 1
    if _FAILURES[key] >= FAILURE_THRESHOLD:
        _FAILURES.pop(key, None)
        mark_down(gateway, model)


def _skipped(model: str, left: float) -> LLMResponse:
    return LLMResponse(
        content=f"Model '{model}' is inside a shared outage window ({left:.0f} s left)",
        finish_reason="error", error_kind="timeout", error_status_code=503,
        error_should_retry=True,
    )


class HouseholdFallbackProvider(FallbackProvider):
    """Upstream's wrapper plus shared per-model outage windows.

    Upstream's own circuit (three primary failures skip the primary for 60 s)
    is per *wrapper* and keyed on nothing. The household's role runtimes on
    the default provider (powerful, profiles, sub-agents, plans, heartbeat,
    classifier) all reuse the loop's wrapper with their own model, so that
    circuit would skip a healthy everyday model because the powerful one
    failed. The per-model windows above do its job, so it is off (A5).
    """

    def _primary_available(self) -> bool:
        return True

    def _detoured(self, provider: LLMProvider, detour: dict[str, Any]) -> LLMProvider:
        """The client for a detour's target: the same one unless it names another provider."""
        name = detour.get("provider")
        if not name or name == getattr(provider, "provider_name", None):
            return provider
        cache = self.__dict__.setdefault("_detour_providers", {})
        key = (name, detour["model"])
        if key not in cache:
            from nanobot.config.schema import ModelPresetConfig

            cache[key] = self._provider_factory(ModelPresetConfig(model=detour["model"], provider=name))
        return cache[key]

    async def _try_candidates(self, call, kwargs, has_streamed, on_stream_recover, on_fallback_attempt):
        state: dict[str, Any] = {"bypass": False, "skipped": 0, "called": 0, "failed": []}

        async def windowed(provider: LLMProvider, call_kwargs: dict[str, Any]) -> LLMResponse:
            gw = gateway_of(provider)
            model = call_kwargs.get("model") or provider.get_default_model()
            if (detour := SHARED_DETOURS.target(gw)) and detour["model"] != model:
                logger.info("detour: {} at {} is lent out; calling {}", model, gw, detour["model"])
                provider = self._detoured(provider, detour)
                call_kwargs = {**call_kwargs, "model": detour["model"]}
                gw, model = gateway_of(provider), detour["model"]
            left = 0.0 if state["bypass"] else remaining(gw, model)
            if left:
                state["skipped"] += 1
                return _skipped(model, left)
            state["called"] += 1
            try:
                response = await call(provider, call_kwargs)
            except Exception as exc:
                if not isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    record(gw, model, LLMProvider._error_response_from_exception(exc))
                raise
            record(gw, model, response)
            if outage_shaped(response):
                state["failed"].append((gw, model))
            elif response.finish_reason != "error":
                # Rescued: another candidate answered in the failed ones' place,
                # which is what tells "this model is broken" from "everything
                # is down" (fork `_mark_model_down`, called once the fallback
                # has answered). Their windows open now, not after two more
                # slow turns: each failed call already ran the retry ladder.
                for down_gw, down_model in state["failed"]:
                    if (down_gw, model_key(down_model)) != (gw, model_key(model)) \
                            and not remaining(down_gw, down_model):
                        mark_down(down_gw, down_model)
                state["failed"].clear()
            return response

        response = await super()._try_candidates(windowed, kwargs, has_streamed, on_stream_recover,
                                                 on_fallback_attempt)
        if state["called"] == 0 and state["skipped"] and (has_streamed is None or not has_streamed[0]):
            logger.warning("outage: every candidate is inside a window; trying them anyway")
            state["bypass"] = True
            self._primary_tripped_at = None
            response = await super()._try_candidates(windowed, kwargs, has_streamed, on_stream_recover,
                                                     on_fallback_attempt)
        return response


def serving_label(provider: Any, model: str) -> str:
    """The model answering now, for ``/status`` (A15, fork ``serving_model``).

    During an outage window the wrapper skips the configured model; saying so
    keeps ``/status`` from naming a model that is not the one answering."""
    if not isinstance(provider, HouseholdFallbackProvider) or not model:
        return model
    if not remaining(gateway_of(provider._primary), model):
        return model
    for preset in provider._fallback_presets:
        try:
            candidate = provider._provider_factory(preset) if preset.provider else provider._primary
        except Exception:  # noqa: BLE001 -- a label must not fail /status
            continue
        if not remaining(gateway_of(candidate), preset.model):
            return f"{preset.model} (fallback for {model})"
    return model


__all__ = ["HouseholdFallbackProvider", "gateway_of", "model_key", "serving_label", "MODEL_OUTAGE_COOLDOWN_S", "mark_down", "record", "remaining"]
