"""Every bundled skill must actually present a description to the model.

A SKILL.md's frontmatter is YAML, so an unquoted description containing ": "
parses as a mapping and the whole block is discarded. Nothing errors: the skill
still loads, and its description silently becomes its own name — which is what
happened to `document` for as long as it had "the bundled script: exec …" in
it. The model's only clue about when to use a skill is that one line, so the
failure mode is a skill nobody ever invokes.
"""

import re
import tempfile
from pathlib import Path

from nanobot.agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader


def _skill_dirs():
    return sorted(p for p in Path(BUILTIN_SKILLS_DIR).iterdir()
                  if (p / "SKILL.md").is_file())


def test_every_bundled_skill_has_a_parsed_description():
    summary = SkillsLoader(Path(tempfile.mkdtemp())).build_skills_summary()
    broken = []
    for line in summary.splitlines():
        m = re.match(r"- \*\*(\S+?)\*\* — (.*)", line)
        if not m:
            continue
        name, desc = m.group(1), m.group(2).strip()
        # The fallback rendering is "<name>  `<path to SKILL.md>`".
        if desc.startswith(name) and "SKILL.md" in desc:
            broken.append(name)
    assert not broken, (
        f"frontmatter failed to parse for: {broken}. A description containing "
        f'": " must be quoted (and its inner quotes escaped).'
    )


def test_a_description_with_a_colon_is_quoted():
    """The specific trap, checked at the source rather than through the loader,
    so the message points at the file to fix."""
    offenders = []
    for d in _skill_dirs():
        text = (d / "SKILL.md").read_text(encoding="utf-8")
        m = re.search(r"^description:(.*)$", text, re.M)
        if not m:
            continue
        value = m.group(1).strip()
        if value.startswith(('"', "'", "|", ">")):
            continue          # quoted or a block scalar — safe
        if ": " in value:
            offenders.append(d.name)
    assert not offenders, f"unquoted description containing ': ' in: {offenders}"
