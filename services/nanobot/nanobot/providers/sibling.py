"""Building a second provider, for a fallback that lives on another one.

A provider instance holds one client: one base URL, one key. A cross-provider
fallback has to reach a different one, and the instance cannot build it -- it
has no config. So it is handed this.

It lives in its own module because there are four places that construct a
provider (the facade, and the CLI's main, subagent and vision builders) and the
first attempt wired only one of them. That looked like it worked -- the
factory built the provider correctly when called by hand -- while the process
serving the house used a different builder, found no factory, and logged
`Fallback provider together_ai is not configured in this process` during a real
outage. One definition, imported by all four, is what stops that recurring.
"""

from __future__ import annotations

from typing import Any, Callable


def sibling_factory(config: Any) -> Callable[[str, str], Any]:
    """Return `build(provider_name, model)` for the providers *config* declares.

    Returns None for a provider the config has no block for, which the rescue
    path treats as "this candidate cannot help" and walks past. Deliberately
    survivable: a fallback naming a provider nobody configured must not be the
    thing that turns an outage into an exception. The deployer refuses to write
    one, so reaching this is already a misconfiguration.
    """

    def build(provider_name: str, model: str) -> Any:
        from nanobot.providers.openai_compat_provider import OpenAICompatProvider
        from nanobot.providers.registry import find_by_name

        spec = find_by_name(provider_name)
        name = spec.name if spec else provider_name
        providers = getattr(config, "providers", None)
        block = None
        if providers is not None:
            block = getattr(providers, name, None)
            if block is None and hasattr(providers, "get"):
                block = providers.get(name)
        if block is None:
            return None
        base = getattr(block, "api_base", None) or (
            spec.default_api_base if spec else None)
        return OpenAICompatProvider(
            api_key=getattr(block, "api_key", None),
            api_base=base,
            default_model=model,
            extra_headers=getattr(block, "extra_headers", None),
            spec=spec,
        )

    return build
