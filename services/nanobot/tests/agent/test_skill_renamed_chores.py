"""The chores skill was called `tasks` until 2026-09-11.

"Tasks" is also what HEARTBEAT.md, cron and spawned subagents call their work,
so the name pointed a model at every kind of task but this one. Two things
outlive a rename and have to keep working under the old name:

  * stored conversations, full of {"skill": "tasks"} blocks a model reads back
    and copies -- the block must still run the chores skill;
  * config that says `disabledSkills: ["tasks"]` -- the house instance does, to
    keep chores away from a container that holds no member credentials. If the
    old name stopped matching, that instance would gain the skill silently.
"""

from __future__ import annotations

from pathlib import Path

from nanobot.agent.runner import _resolve_skill_name
from nanobot.agent.skills import BUILTIN_SKILLS_DIR, SKILL_ALIASES, SkillsLoader

SKILL_PATHS = {"chores": "/app/nanobot/skills/chores/SKILL.md",
               "grocery": "/app/nanobot/skills/grocery/SKILL.md"}


def test_the_skill_ships_as_chores():
    assert (BUILTIN_SKILLS_DIR / "chores" / "SKILL.md").is_file()
    assert not (BUILTIN_SKILLS_DIR / "tasks").exists(), \
        "a `tasks` directory beside `chores` would list the skill twice"
    head = (BUILTIN_SKILLS_DIR / "chores" / "SKILL.md").read_text(encoding="utf-8")
    assert "\nname: chores\n" in head
    assert '{\\"skill\\":\\"chores\\"' in head, "the description still invokes the old name"


def test_the_description_says_tareas():
    """The family's word for chores is "tareas"; the benchmark's question is
    "¿qué tareas tengo pendientes para hoy?"."""
    head = (BUILTIN_SKILLS_DIR / "chores" / "SKILL.md").read_text(encoding="utf-8")
    description = next(line for line in head.splitlines() if line.startswith("description:"))
    assert "tareas" in description


def test_a_block_written_with_the_old_name_runs_chores():
    assert _resolve_skill_name("tasks", SKILL_PATHS) == "chores"
    assert _resolve_skill_name("Tasks", SKILL_PATHS) == "chores"
    assert _resolve_skill_name("chores", SKILL_PATHS) == "chores"


def test_an_alias_never_beats_a_real_skill_of_that_name():
    paths = {**SKILL_PATHS, "tasks": "/workspace/skills/tasks/SKILL.md"}
    assert _resolve_skill_name("tasks", paths) == "tasks"


def test_an_old_name_with_nothing_behind_it_still_fails():
    assert _resolve_skill_name("tasks", {"grocery": "/x/SKILL.md"}) is None


def test_disabling_the_old_name_disables_chores(tmp_path: Path):
    loader = SkillsLoader(tmp_path, disabled_skills={"tasks"})
    names = {s["name"] for s in loader.list_skills(filter_unavailable=False)}
    assert "chores" not in names, "disabledSkills: ['tasks'] no longer switches chores off"
    assert "grocery" in names


def test_the_house_instance_keeps_chores_off():
    import json
    repo = Path(__file__).resolve().parents[2]
    house = json.loads((repo / "config" / "instances" / "house" / "config.json")
                       .read_text(encoding="utf-8"))
    disabled = set(house["agents"]["defaults"]["disabledSkills"])
    assert disabled & {"chores", *[old for old, new in SKILL_ALIASES.items() if new == "chores"]}
