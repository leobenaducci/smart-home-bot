"""Load and render agent system prompt templates (Jinja2) under nanobot/templates/.

Agent prompts live in ``templates/agent/`` (pass names like ``agent/identity.md``).
Shared copy lives under ``agent/_snippets/`` and is included via
``{% include 'agent/_snippets/....md' %}``.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from nanobot.context_sources import record_source

_TEMPLATES_ROOT = Path(__file__).resolve().parent.parent / "templates"


def _environment() -> Environment:
    # household: the household's copies of the templates it changed come
    # first (nanobot/household/templates); everything else is upstream's.
    from nanobot.household.skill_roots import household_enabled

    return _environment_for(household_enabled())


@lru_cache
def _environment_for(household: bool) -> Environment:
    roots = [str(_TEMPLATES_ROOT)]
    if household:
        roots.insert(0, str(_TEMPLATES_ROOT.parent / "household" / "templates"))
    # Plain-text prompts: do not HTML-escape variable values.
    return Environment(
        loader=FileSystemLoader(roots),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_template(name: str, *, strip: bool = False, track_source: bool = False, **kwargs: Any) -> str:
    """Render ``name`` (e.g. ``agent/identity.md``, ``agent/platform_policy.md``) under ``templates/``.

    Use ``strip=True`` for single-line user-facing strings when the file ends
    with a trailing newline you do not want preserved.
    """
    text = _environment().get_template(name).render(**kwargs)
    text = text.rstrip() if strip else text
    if track_source:
        record_source(_TEMPLATES_ROOT / name, text)
    return text
