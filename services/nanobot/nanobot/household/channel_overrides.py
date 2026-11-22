"""household: channels whose runtime the household replaces, by plugin name.

``whatsapp`` keeps upstream's manifest (and its contract tests) but runs the
Baileys-bridge channel (``whatsapp_bridge``) once the household layer is on,
without upstream's start-time install of Neonize. The channel name, and with
it every ``whatsapp:``/``whatsapp-own:`` session key, is unchanged.
"""

from __future__ import annotations

_OVERRIDES: dict[str, tuple[str, tuple[str, ...]]] = {
    "whatsapp": ("nanobot.household.whatsapp_bridge:WhatsAppChannel", ()),
}


def household_runtime(name: str) -> str | None:
    from nanobot.household.skill_roots import household_enabled

    if name in _OVERRIDES and household_enabled():
        return _OVERRIDES[name][0]
    return None


def household_dependencies(name: str) -> tuple[str, ...] | None:
    """The replacement's dependencies (shipped in the image), or None."""
    from nanobot.household.skill_roots import household_enabled

    if name in _OVERRIDES and household_enabled():
        return _OVERRIDES[name][1]
    return None
