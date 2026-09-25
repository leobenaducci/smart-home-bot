"""A model that answers only streamed must still answer.

Together refuses `Qwen/Qwen3.8-Flash` on the non-streaming path with
`This model only supports streaming. Set "stream": true.` -- and that model is
this stack's configured *fallback*. So when the main model went down the rescue
could not run either, and an outage that should have cost a slower answer cost
the answer entirely. `nanobot-house` failed its deploy verification on exactly
this, with the log blaming the main model's 500.

The request is right in that exchange; only the transport is refused. So it is
re-sent down the streaming path rather than reported.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nanobot.providers.openai_compat_provider import OpenAICompatProvider

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"  <- {detail}"))
    if not cond:
        failures.append(label)


print("the provider recognises the refusal")
for msg in (
    'This model only supports streaming. Set "stream": true.',
    "Error: this model only support streaming",
    "you must set stream to true for this model",
):
    check(f"  {msg[:44]!r}", OpenAICompatProvider._requires_streaming(Exception(msg)))

print("and does not mistake its neighbours for it")
for msg in (
    "temperature: only 1 is allowed for this model",
    "model is disabled",
    "rate limit exceeded",
    "Internal server error",
):
    check(f"  {msg[:44]!r}", not OpenAICompatProvider._requires_streaming(Exception(msg)))


print("\na refused non-streaming call is re-sent as a streaming one")


class _Boom:
    """The chat.completions.create that Together gives for such a model."""

    async def create(self, **kwargs):
        raise RuntimeError('This model only supports streaming. Set "stream": true.')


class _Provider(OpenAICompatProvider):
    def __init__(self):                                   # no network, no config
        self.api_base = "https://example.invalid/v1"
        self.default_model = "Qwen/Qwen3.8-Flash"
        self._spec = None
        self.streamed_with = None

        class _C:
            chat = type("chat", (), {"completions": _Boom()})()
        self._client = _C()

    def _should_use_responses_api(self, *a, **k):
        return False

    def _build_kwargs(self, *a, **k):
        return {"model": self.default_model, "messages": []}

    async def chat_stream(self, **kwargs):
        self.streamed_with = kwargs
        return "STREAMED"


p = _Provider()
out = asyncio.run(p.chat(messages=[{"role": "user", "content": "hola"}],
                         model="Qwen/Qwen3.8-Flash", max_tokens=64,
                         temperature=0.3))
check("the answer comes back rather than the error", out == "STREAMED", repr(out))
check("it went down the streaming path at all", p.streamed_with is not None)
check("carrying the same question",
      (p.streamed_with or {}).get("messages") == [{"role": "user", "content": "hola"}],
      p.streamed_with)
check("and the same model, not the provider default",
      (p.streamed_with or {}).get("model") == "Qwen/Qwen3.8-Flash", p.streamed_with)
check("with no delta callback -- nobody is reading this one",
      (p.streamed_with or {}).get("on_content_delta") is None, p.streamed_with)

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    raise SystemExit(1)
print("all checks passed")
