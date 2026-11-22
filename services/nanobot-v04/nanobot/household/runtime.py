"""household: the running gateway's loop, for household code outside a turn.

Hooks are built per turn and do not see the turn's LLMRuntime. Household code
that needs a model call of its own (skill translation, the classifier) asks
the loop the gateway bound here for its current provider and model, which
follows provider reloads the way the loop itself does.
"""

from __future__ import annotations

from typing import Any


class HouseholdRuntime:
    def __init__(self) -> None:
        self.loop: Any = None
        self.config: Any = None

    def bind(self, loop: Any, config: Any = None) -> None:
        self.loop = loop
        if config is not None:
            self.config = config

    @property
    def provider(self) -> Any:
        return getattr(self.loop, "provider", None)

    @property
    def model(self) -> str | None:
        return getattr(self.loop, "model", None)


RUNTIME = HouseholdRuntime()


# Per-turn flags household hooks raise for the household loop (a skill block
# that resolved to nothing is the stop reason `bad_invocation`, which upstream's
# runner does not know).
from contextvars import ContextVar  # noqa: E402

TURN_FLAGS: ContextVar[dict | None] = ContextVar("household_turn_flags", default=None)


def raise_turn_flag(name: str) -> None:
    flags = TURN_FLAGS.get()
    if flags is not None:
        flags[name] = True
