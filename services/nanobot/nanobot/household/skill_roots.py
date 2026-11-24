"""household: where the household's skills live, as SkillsLoader roots.

Two roots between the agent's plugin skills and upstream's built-ins:

- ``remote``: skills served by the service they describe, kept on disk by
  ``remote_skills.RemoteSkillSync`` (present only while the service answers);
- ``household``: the skills shipped in this package (``skills_data/``).

Both are only listed once the household layer is on -- the gateway binds it,
or ``NANOBOT_HOUSEHOLD=1`` -- so upstream's own tests and a plain install see
upstream's skills alone.
"""

from __future__ import annotations

import os
from pathlib import Path

HOUSEHOLD_SKILLS_DIR = Path(__file__).parent / "skills_data"


def household_enabled() -> bool:
    from nanobot.household.runtime import RUNTIME

    return RUNTIME.loop is not None or os.environ.get("NANOBOT_HOUSEHOLD") == "1"


def household_skill_roots(workspace: Path) -> list[tuple[str, Path]]:
    if not household_enabled():
        return []
    from nanobot.household.remote_skills import cache_dir

    return [("remote", cache_dir(workspace)), ("household", HOUSEHOLD_SKILLS_DIR)]


def household_skill_groups(workspace: Path) -> list[tuple[str, str, Path]]:
    labels = {"remote": "House service skills", "household": "Household skills"}
    return [(labels[source], source, root) for source, root in household_skill_roots(workspace)]
