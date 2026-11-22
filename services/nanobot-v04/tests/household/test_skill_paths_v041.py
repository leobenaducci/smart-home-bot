"""household: skill invocations route through v0.4.1's skills summary format."""

from nanobot.agent.skills import SkillsLoader
from nanobot.household import text_calls


def _system(body: str) -> list[dict]:
    return [{"role": "system", "content": body}]


def test_relative_paths_resolve_against_their_section_root(tmp_path):
    body = (
        "### Workspace skills (`skills`)\n"
        "- **lights** — Turn `lights` on and off  `lights/SKILL.md`\n\n"
        f"### Built-in skills (`{tmp_path / 'builtin'}`)\n"
        "- **weather** — Forecasts  `weather/SKILL.md`\n"
    )
    token = text_calls.SKILL_ROOT_WORKSPACE.set(tmp_path)
    try:
        paths = text_calls._collect_skill_paths(_system(body))
    finally:
        text_calls.SKILL_ROOT_WORKSPACE.reset(token)
    assert paths["lights"] == str(tmp_path / "skills" / "lights" / "SKILL.md")
    assert paths["weather"] == str(tmp_path / "builtin" / "weather" / "SKILL.md")


def test_real_summary_from_upstream_loader_routes(tmp_path):
    skill = tmp_path / "skills" / "lights"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: lights\ndescription: House lights\n---\n# lights\n")
    summary = SkillsLoader(tmp_path).build_skills_summary()
    token = text_calls.SKILL_ROOT_WORKSPACE.set(tmp_path)
    try:
        paths = text_calls._collect_skill_paths(_system(summary))
    finally:
        text_calls.SKILL_ROOT_WORKSPACE.reset(token)
    assert paths["lights"] == str(skill / "SKILL.md")


def test_aliases_route_old_names():
    paths = {"lights": "/x/lights/SKILL.md", "chores": "/x/chores/SKILL.md"}
    assert text_calls._resolve_skill_name("home_lights", paths) == "lights"
    assert text_calls._resolve_skill_name("tasks", paths) == "chores"
