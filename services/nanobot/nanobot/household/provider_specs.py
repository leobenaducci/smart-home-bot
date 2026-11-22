"""household: the fork's extra provider specs (D1).

In the fork these were registry entries; upstream's config treats any extra
``providers.<name>`` block as a custom OpenAI-compatible provider and builds a
dynamic spec for it, which needs an ``apiBase`` and a key. The household's
NanoGPT, Together, Ollama Cloud, the second Ollama for vision, the
``openai_compatible`` slot and every further ``ollama_<id>`` instance keep the
fork's defaults on top of that dynamic spec:

- a default base URL (NanoGPT, Together, Ollama Cloud), so a block without
  ``apiBase`` still reaches the right host;
- keyless local servers (``ollama_*`` except cloud, ``openai_compatible``);
- gateway flag (NanoGPT, Together: ``vendor/model`` ids pass through).

They are *not* added to upstream's ``PROVIDERS``: that would make the config
reject ``providers.nanogpt`` as a clash with a built-in, and upstream's
provider-contract tests enumerate that tuple.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

_LOCAL_OLLAMA_RE = re.compile(r"ollama_([a-z][a-z0-9]{0,15})")

_FIXED: dict[str, dict[str, Any]] = {
    "nanogpt": {"display_name": "NanoGPT", "is_gateway": True,
                "default_api_base": "https://nano-gpt.com/api/v1", "env_key": "NANOGPT_API_KEY"},
    "together_ai": {"display_name": "Together AI", "is_gateway": True,
                    "default_api_base": "https://api.together.xyz/v1", "env_key": "TOGETHER_API_KEY"},
    "ollama_cloud": {"display_name": "Ollama Cloud", "default_api_base": "https://ollama.com/v1",
                     "env_key": "OLLAMA_CLOUD_API_KEY"},
    "ollama_vision": {"display_name": "Ollama (vision)", "is_local": True, "env_key": "OLLAMA_API_KEY"},
    "openai_compatible": {"display_name": "OpenAI-compatible", "is_local": True,
                          "env_key": "OPENAI_COMPATIBLE_API_KEY"},
}


def household_fields(name: str) -> dict[str, Any] | None:
    raw = (name or "").replace("-", "_").lower()
    if raw in _FIXED:
        return dict(_FIXED[raw])
    m = _LOCAL_OLLAMA_RE.fullmatch(raw)
    if m and m.group(1) != "cloud":
        return {"display_name": f"Ollama ({m.group(1)})", "is_local": True, "env_key": "OLLAMA_API_KEY"}
    return None


def household_dynamic_spec(name: str, provider_config: Any) -> Any:
    """The dynamic spec upstream would build, with the fork's defaults, or None."""
    fields = household_fields(name)
    if fields is None:
        return None
    from nanobot.providers.registry import create_dynamic_spec

    if not getattr(provider_config, "api_base", None) and not fields.get("default_api_base"):
        raise ValueError(f"Provider '{name}' requires api_base in config.")
    spec = create_dynamic_spec(
        name,
        display_name=getattr(provider_config, "display_name", None) or fields.pop("display_name"),
        thinking_style=getattr(provider_config, "thinking_style", None) or "",
    )
    fields.pop("display_name", None)
    known = {f.name for f in dataclasses.fields(spec)}
    return dataclasses.replace(spec, **{k: v for k, v in fields.items() if k in known})


__all__ = ["household_dynamic_spec", "household_fields"]
