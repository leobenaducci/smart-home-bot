"""`alfred-skill`: every household skill behind one command, for pi's tools."""
import json
from pathlib import Path

import pytest

from nanobot.harness import alfred_skill as A

GUIDE = """```python
import json
def get_thing(name):
    return {"thing": name}
def set_thing(name, value):
    return {"ok": True, "name": name, "value": value}
```
"""


def skill(root: Path, name: str, desc: str, guide: str | None = GUIDE):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f'---\nname: {name}\ndescription: "{desc}"\n---\n# {name}\n')
    if guide is not None:
        (d / "SKILL_PYTHON.md").write_text(guide)


@pytest.fixture()
def roots(tmp_path, monkeypatch):
    ws, builtin = tmp_path / "ws", tmp_path / "builtin"
    monkeypatch.setattr(A, "BUILTIN_SKILLS_DIR", builtin)
    monkeypatch.setattr(A, "_settings", lambda: (ws, {"off"}))
    return ws, builtin


def test_found_in_the_loaders_order(roots):
    """Workspace, then a service's remote skill, then the builtin -- SkillsLoader's order."""
    ws, builtin = roots
    skill(builtin, "lights", "builtin")
    skill(ws / ".skills-remote", "lights", "remote")
    assert A.find("lights") == ws / ".skills-remote" / "lights" / "SKILL.md"
    skill(ws / "skills", "lights", "workspace")
    assert A.find("lights") == ws / "skills" / "lights" / "SKILL.md"


def test_a_disabled_skill_is_off_here_too(roots):
    _, builtin = roots
    skill(builtin, "off", "switched off for this member")
    assert A.find("off") is None
    assert "off" not in [s["skill"] for s in A.list_skills()]


def test_a_name_cannot_walk_out_of_the_skill_roots(roots):
    assert A.find("../../etc") is None and A.find("") is None


def test_list_has_web_and_only_runnable_skills(roots):
    _, builtin = roots
    skill(builtin, "thing", "a thing")
    skill(builtin, "docs-only", "no python guide", guide=None)
    names = [s["skill"] for s in A.list_skills()]
    assert names[0] == "web" and "thing" in names and "docs-only" not in names
    assert next(s for s in A.list_skills() if s["skill"] == "thing")["actions"] == ["get_thing", "set_thing"]


def test_an_action_runs_through_the_runners_translation(roots):
    _, builtin = roots
    skill(builtin, "thing", "a thing")
    code, out = A.run("thing", "set_thing", {"name": "lamp", "value": 3})
    assert code == 0 and json.loads(out) == {"ok": True, "name": "lamp", "value": 3}


def test_wrong_names_say_what_exists(roots):
    _, builtin = roots
    skill(builtin, "thing", "a thing")
    code, out = A.run("thing", "nope", {})
    assert code == 2 and json.loads(out)["actions"] == ["get_thing", "set_thing"]
    code, out = A.run("nope", "x", {})
    assert code == 2 and "thing" in json.loads(out)["skills"]


def test_bad_json_is_an_error_not_a_crash(roots, capsys):
    assert A.main(["thing", "get_thing", "{not json"]) == 2
    assert "not valid JSON" in capsys.readouterr().out


def test_args_cannot_change_the_action(roots):
    """A read call carrying {"action": "set_thing"} used to run set_thing."""
    _, builtin = roots
    skill(builtin, "thing", "a thing")
    code, out = A.run("thing", "get_thing", {"name": "x", "action": "set_thing"})
    assert code == 2 and "may not contain" in json.loads(out)["error"]
