"""household: skill names renamed after a model or a config learned them.

A skill renamed after models learned it keeps answering to the old name: the
history holds invocations under it and config written before the rename keeps
naming it. Both are aliased on read rather than rewritten.
"""

SKILL_ALIASES = {
    "tasks": "chores",
    "home-lights": "lights",
}


# The fork's "Use when ..." endings on built-in skills whose upstream
# description says what the skill is but not when to reach for it (A15).
# Appended on read, so upstream's text and body stay upstream's.
SKILL_TRIGGERS = {
    "clawhub": "Use when asked to find, add or install a new skill.",
    "cron": "Use when asked to be reminded of something later, or to make something "
            "happen on a schedule.",
    "github": "Use when asked about a repository, an issue, a pull request or a CI run.",
    "tmux": "Use when driving an interactive terminal program that needs keystrokes.",
}


def with_trigger(name: str, description: str) -> str:
    """*description* with the household's trigger, unless it already says when."""
    from nanobot.household.skill_roots import household_enabled

    trigger = SKILL_TRIGGERS.get(name)
    if not trigger or not household_enabled() or "use when" in description.lower():
        return description
    return f"{description.rstrip()} {trigger}"
