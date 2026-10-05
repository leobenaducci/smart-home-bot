"""The Studio skill: project and character management actions.

`pytest -q tests/skills/test_studio_skill.py` from services/nanobot/.

These actions are thin wrappers over the Studio API, but the skill has to
resolve projects and characters by the names people use, then call the right
endpoint with the right body. A mismatch means the model invokes an action
that the API refuses, and the person gets "Alfred could not do it" instead
of a copied project or an updated character.
"""

import json
import math
import os
import subprocess
from pathlib import Path

import pytest

from nanobot.agent.runner import _load_skill_python_guide, _static_skill_translation
from nanobot.agent.skills import BUILTIN_SKILLS_DIR

SKILL_DIR = Path(BUILTIN_SKILLS_DIR) / "studio"
SKILL_MD = SKILL_DIR / "SKILL.md"


@pytest.fixture()
def guide() -> str:
    src = _load_skill_python_guide(str(SKILL_MD))
    assert src, "SKILL_PYTHON.md did not yield a python block"
    return src


@pytest.fixture()
def ns(guide):
    """The guide, executed the way the runner executes it, with curl stubbed out."""
    scope: dict = {"__name__": "__main__"}
    # The guide reads these at import time.
    for key in ("TASKS_API_URL", "HOMECORE_USER_ID", "HOMECORE_PROXY_TOKEN"):
        os.environ[key] = os.environ.get(key) or "x"
    exec(compile(guide, "<skill:studio>", "exec"), scope)
    return scope


def test_every_advertised_action_exists(guide):
    """The description is the menu the model orders from."""
    for action in ("clone_project", "edit_character"):
        assert f"def {action}(" in guide, action
        assert action in SKILL_MD.read_text(encoding="utf-8"), \
            f"{action} is implemented but never advertised"


def test_clone_project_resolves_by_name_and_posts_duplicate(ns):
    calls = []

    def fake_curl(method, path, data=None):
        calls.append((method, path, data))
        if method == "GET" and path == "projects":
            return {"projects": [{"id": "p1", "name": "Faro Zorro"}]}
        if method == "GET" and path == "projects/p1":
            return {"id": "p1", "name": "Faro Zorro"}
        if method == "POST" and path == "projects/p1/duplicate":
            return {"id": "p2", "name": "Faro Zorro - v2"}
        return {}

    ns["_curl"] = fake_curl
    out = ns["clone_project"]("Faro Zorro", "Faro Zorro - v2")

    assert out["new_project"] == "Faro Zorro - v2"
    assert out["id"] == "p2"
    assert calls[-1] == ("POST", "projects/p1/duplicate", {"name": "Faro Zorro - v2"})


def test_clone_project_omits_name_when_empty(ns):
    calls = []

    def fake_curl(method, path, data=None):
        calls.append((method, path, data))
        if method == "GET" and path == "projects":
            return {"projects": [{"id": "p1", "name": "Faro Zorro"}]}
        if method == "GET" and path == "projects/p1":
            return {"id": "p1", "name": "Faro Zorro"}
        if method == "POST" and path == "projects/p1/duplicate":
            return {"id": "p2", "name": "Faro Zorro (copia)"}
        return {}

    ns["_curl"] = fake_curl
    out = ns["clone_project"]("Faro Zorro")

    assert out["id"] == "p2"
    assert calls[-1] == ("POST", "projects/p1/duplicate", {})


def test_edit_character_resolves_by_name_and_puts_fields(ns):
    calls = []

    def fake_curl(method, path, data=None):
        calls.append((method, path, data))
        if method == "GET" and path == "projects":
            return {"projects": [{"id": "p1", "name": "Faro Zorro"}]}
        if method == "GET" and path == "projects/p1":
            return {"id": "p1", "name": "Faro Zorro"}
        if method == "GET" and path == "projects/p1/characters":
            return {"characters": [{"id": "c1", "name": "Bruma", "look": "old"}]}
        if method == "PUT" and path == "projects/p1/characters/c1":
            return {"id": "c1", "name": "Bruma", "look": "new"}
        return {}

    ns["_curl"] = fake_curl
    out = ns["edit_character"]("Faro Zorro", "Bruma", look="new")

    assert out["updated"] == "Bruma"
    assert ("PUT", "projects/p1/characters/c1", {"look": "new"}) in calls


def test_edit_character_refuses_ambiguous_name(ns):
    def fake_curl(method, path, data=None):
        if method == "GET" and path == "projects":
            return {"projects": [{"id": "p1", "name": "Faro Zorro"}]}
        if method == "GET" and path == "projects/p1":
            return {"id": "p1", "name": "Faro Zorro"}
        if method == "GET" and path == "projects/p1/characters":
            return {"characters": [{"id": "c1", "name": "Bruma"}, {"id": "c2", "name": "Bruma II"}]}
        return {}

    ns["_curl"] = fake_curl
    out = ns["edit_character"]("Faro Zorro", "Bru")

    assert "error" in out
    assert "several" in out["error"]


def test_edit_character_ignores_unknown_fields(ns):
    calls = []

    def fake_curl(method, path, data=None):
        calls.append((method, path, data))
        if method == "GET" and path == "projects":
            return {"projects": [{"id": "p1", "name": "Faro Zorro"}]}
        if method == "GET" and path == "projects/p1":
            return {"id": "p1", "name": "Faro Zorro"}
        if method == "GET" and path == "projects/p1/characters":
            return {"characters": [{"id": "c1", "name": "Bruma"}]}
        if method == "PUT" and path == "projects/p1/characters/c1":
            return {"id": "c1", "name": "Bruma"}
        return {}

    ns["_curl"] = fake_curl
    ns["edit_character"]("Faro Zorro", "Bruma", look="new", extra="drop me")

    assert calls[-1] == ("PUT", "projects/p1/characters/c1", {"look": "new"})


def test_reads_translate_without_an_llm(guide):
    """Static translation is the cheap path; losing it costs a model call each."""
    code = _static_skill_translation(
        {"skill": "studio", "action": "edit_character",
         "project": "Faro Zorro", "character": "Bruma", "look": "new"}, guide)
    assert code and "edit_character(project='Faro Zorro', character='Bruma', look='new')" in code
