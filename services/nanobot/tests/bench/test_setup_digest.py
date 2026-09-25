"""The benchmark stamps each result with what the model was given.

On 2026-09-11 five fixes landed -- skills, prompts, a runner rescue, Home
Assistant tools switched off -- and none touched cases.json, so every result
from before them looked as current as the ones after. `setup_digest` hashes
the setup; the admin page runs the same function in the container a result
came from and marks the result when they differ. These tests pin what moves
it and what must not.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

BENCH = Path(__file__).resolve().parents[2] / "bench" / "model_bench.py"


@pytest.fixture(scope="module")
def mb():
    spec = importlib.util.spec_from_file_location("model_bench", BENCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(workspace: Path, disabled_skills=(), disabled_tools=()):
    server = SimpleNamespace(enabled_tools=["*"], disabled_tools=list(disabled_tools))
    return SimpleNamespace(
        workspace_path=str(workspace),
        agents=SimpleNamespace(defaults=SimpleNamespace(disabled_skills=list(disabled_skills))),
        tools=SimpleNamespace(mcp_servers={"homeassistant": server}),
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    for name in ("AGENTS.md", "SOUL.md", "TOOLS.md", "USER.md"):
        (tmp_path / name).write_text(f"# {name}\n", encoding="utf-8")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "MEMORY.md").write_text("yesterday\n", encoding="utf-8")
    return tmp_path


def test_it_is_stable(mb, workspace):
    assert mb.setup_digest(_config(workspace)) == mb.setup_digest(_config(workspace))
    assert len(mb.setup_digest(_config(workspace))) == 12


def test_a_shared_prompt_file_moves_it(mb, workspace):
    before = mb.setup_digest(_config(workspace))
    (workspace / "SOUL.md").write_text("# SOUL.md\nnew rule\n", encoding="utf-8")
    assert mb.setup_digest(_config(workspace)) != before


def test_a_workspace_skill_moves_it(mb, workspace):
    before = mb.setup_digest(_config(workspace))
    (workspace / "skills" / "x").mkdir(parents=True)
    (workspace / "skills" / "x" / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    assert mb.setup_digest(_config(workspace)) != before


def test_what_the_model_is_offered_moves_it(mb, workspace):
    base = mb.setup_digest(_config(workspace))
    assert mb.setup_digest(_config(workspace, disabled_skills=["chores"])) != base
    assert mb.setup_digest(_config(workspace, disabled_tools=["todo_get_items"])) != base


def test_the_person_does_not_move_it(mb, workspace):
    """USER.md and memory change every day; they are the person, not the setup."""
    before = mb.setup_digest(_config(workspace))
    (workspace / "USER.md").write_text("# USER.md\nlikes tea now\n", encoding="utf-8")
    (workspace / "memory" / "MEMORY.md").write_text("today\n", encoding="utf-8")
    assert mb.setup_digest(_config(workspace)) == before


def test_a_config_from_before_disabled_tools_still_hashes(mb, workspace):
    """The page runs this in containers that may not be redeployed yet."""
    config = _config(workspace)
    del config.tools.mcp_servers["homeassistant"].disabled_tools
    assert len(mb.setup_digest(config)) == 12
