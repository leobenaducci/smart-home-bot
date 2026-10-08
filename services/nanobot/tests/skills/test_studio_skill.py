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
    for action in ("clone_project", "edit_character", "download_video"):
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


def test_download_video_fetches_render_when_no_shot(ns, tmp_path, monkeypatch):
    calls = []

    def fake_curl(method, path, data=None):
        calls.append((method, path, data))
        if method == "GET" and path == "projects":
            return {"projects": [{"id": "p1", "name": "Faro Zorro"}]}
        if method == "GET" and path == "projects/p1":
            return {"id": "p1", "name": "Faro Zorro",
                    "renders": [{"file": "renders/20261006-120000.mp4", "created": 1}]}
        return {}

    ns["_curl"] = fake_curl
    uploaded = []

    def fake_download(pid, rel, local_path):
        calls.append(("download", pid, rel, local_path))
        Path(local_path).write_bytes(b"fake video")
        return {"ok": True, "local_path": local_path}

    def fake_upload(local_path, remote_path=None):
        uploaded.append((local_path, remote_path))
        return {"ok": True, "remote_path": "ana/alfred/studio_Faro_Zorro_20261006-120000.mp4",
                "download_link": "[studio_Faro_Zorro_20261006-120000.mp4](download:ana/alfred/studio_Faro_Zorro_20261006-120000.mp4)"}

    ns["_download_project_file"] = fake_download
    ns["_upload_to_share"] = fake_upload
    monkeypatch.setenv("FILE_SHARE_FOLDER", "ana")
    out = ns["download_video"]("Faro Zorro")

    assert out["download_link"].startswith("[")
    assert "download:" in out["download_link"]
    assert any(c[2] == "renders/20261006-120000.mp4" for c in calls if c[0] == "download")


def test_download_video_fetches_shot_take_when_shot_given(ns, tmp_path, monkeypatch):
    calls = []

    def fake_curl(method, path, data=None):
        calls.append((method, path, data))
        if method == "GET" and path == "projects":
            return {"projects": [{"id": "p1", "name": "Faro Zorro"}]}
        if method == "GET" and path == "projects/p1":
            return {"id": "p1", "name": "Faro Zorro",
                    "shots": [{"id": "s1", "title": "Opening", "takes": [
                        {"id": "t1", "file": "takes/s1/t1.mp4"}], "chosen": 0}]}
        return {}

    ns["_curl"] = fake_curl

    def fake_download(pid, rel, local_path):
        Path(local_path).write_bytes(b"fake video")
        return {"ok": True}

    def fake_upload(local_path, remote_path=None):
        return {"ok": True, "remote_path": "ana/alfred/file.mp4",
                "download_link": "[file.mp4](download:ana/alfred/file.mp4)"}

    ns["_download_project_file"] = fake_download
    ns["_upload_to_share"] = fake_upload
    monkeypatch.setenv("FILE_SHARE_FOLDER", "ana")
    out = ns["download_video"]("Faro Zorro", shot=1)

    assert "download:" in out["download_link"]


def test_download_video_lists_shots_when_no_render(ns, monkeypatch):
    def fake_curl(method, path, data=None):
        if method == "GET" and path == "projects":
            return {"projects": [{"id": "p1", "name": "Faro Zorro"}]}
        if method == "GET" and path == "projects/p1":
            return {"id": "p1", "name": "Faro Zorro",
                    "shots": [{"id": "s1", "takes": [{"id": "t1", "file": "takes/s1/t1.mp4"}], "chosen": 0},
                              {"id": "s2", "takes": [], "chosen": -1}]}
        return {}

    ns["_curl"] = fake_curl
    monkeypatch.setenv("FILE_SHARE_FOLDER", "ana")
    out = ns["download_video"]("Faro Zorro")

    assert "error" in out
    assert out["shots_with_video"] == [1]


def _fake_curl_run(ns, *, returncode, body=b"", stderr=""):
    """Stand in for curl: write `body` to the -o path, the way curl would."""
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        if body:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(body)
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    ns["subprocess"] = type("S", (), {"run": staticmethod(run)})
    return seen


def test_download_refuses_an_error_answer_instead_of_saving_it(ns, tmp_path):
    out_path = tmp_path / "film.mp4"
    seen = _fake_curl_run(ns, returncode=22, stderr="curl: (22) The requested URL returned error: 404")
    out = ns["_download_project_file"]("p1", "renders/missing.mp4", str(out_path))
    assert "error" in out and "404" in out["error"]
    assert not out_path.exists()
    assert "-sfk" in seen[0]


def test_download_does_not_mistake_a_leftover_file_for_the_new_one(ns, tmp_path):
    out_path = tmp_path / "film.mp4"
    out_path.write_bytes(b"from an earlier run")
    _fake_curl_run(ns, returncode=22, stderr="curl: (22) returned error: 404")
    out = ns["_download_project_file"]("p1", "renders/missing.mp4", str(out_path))
    assert "error" in out
    assert not out_path.exists()


def test_download_keeps_a_file_curl_wrote_successfully(ns, tmp_path):
    out_path = tmp_path / "film.mp4"
    _fake_curl_run(ns, returncode=0, body=b"video bytes")
    out = ns["_download_project_file"]("p1", "renders/ok.mp4", str(out_path))
    assert out == {"ok": True, "local_path": str(out_path)}
    assert out_path.read_bytes() == b"video bytes"


def test_upload_to_share_needs_a_configured_host(ns):
    ns["SHARE_HOST"] = ""
    ns["SHARE_FOLDER"] = "ana"
    out = ns["_upload_to_share"]("/nonexistent")
    assert out == {"error": "file-share is not configured for this assistant"}
