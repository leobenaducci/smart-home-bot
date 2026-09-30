"""No commit an assistant makes carries this house's real values out.

The broker is never given them: the deployer writes salted hashes of the
credentials and of the user store's logins, e-mails and phones, and a commit's
added lines are cut into words and hashed against them. A refusal names the
kind of value and the file, never the value.
"""
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from nanobot.code_broker import leaks, workspace as W
from nanobot.code_broker.workspace import BrokerError, branch, commit

ROOT = Path(__file__).resolve().parents[4]


def guard(values: dict[str, str], salt: str = "s4lt") -> dict:
    return {"salt": salt, "items": {leaks.digest(salt, v): label for v, label in values.items()}}


def diff_adding(path: str, *lines: str) -> str:
    body = "\n".join("+" + ln for ln in lines)
    return f"diff --git a/{path} b/{path}\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}\n"


def test_the_two_gates_agree_on_what_a_key_looks_like():
    spec = importlib.util.spec_from_file_location("publish_check", ROOT / "deploy" / "publish_check.py")
    pc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pc)
    assert leaks.KEY_SHAPES == pc.KEY_SHAPES


def test_a_real_credential_a_login_an_email_and_a_phone_are_found():
    g = guard({"nano-0123456789abcdefghij": "credential NANOGPT_API_KEY",
               "999000111": "a user store username",
               "tomi@ejemplo.org": "a user store email",
               "5491155550101": "a user store phone"})
    d = diff_adding("app/config.py",
                    'KEY = "nano-0123456789abcdefghij"',
                    "ADMINS = ['999000111']",
                    "# write to tomi@ejemplo.org",
                    "CALL = '+54 9 11 5555 0101'")
    found = leaks.findings(d, g)
    assert len(found) == 4 and all("app/config.py" in f for f in found)
    assert not any("0123456789" in f or "tomi@" in f for f in found), "never the value"


def test_ordinary_code_is_not_a_finding():
    g = guard({"nano-0123456789abcdefghij": "credential NANOGPT_API_KEY"})
    d = diff_adding("app.py", "def turn_on(device):", "    return requests.post(BASE + '/on')")
    assert leaks.findings(d, g) == []


def test_key_shapes_are_found_even_without_the_hashes():
    d = diff_adding("x.env", "TOKEN=ghp_" + "a" * 36)
    assert leaks.findings(d, None) == ["a GitHub token in x.env"]


def _repo(tmp_path):
    root = tmp_path / "ws"
    (root / "user2").mkdir(parents=True)
    (root / W.MARKER).write_text("")
    proj = root / "user2" / "proj"
    subprocess.run(["git", "init", "-q", "-b", "main", str(proj)], check=True)
    (proj / "README.md").write_text("x\n")
    subprocess.run(["git", "-C", str(proj), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(proj), "-c", "user.email=a@example.com", "-c", "user.name=a",
                    "commit", "-qm", "init"], check=True)
    return W.Workspace(root), proj


@pytest.mark.asyncio
async def test_the_broker_refuses_a_commit_that_carries_one(tmp_path, monkeypatch):
    ws, proj = _repo(tmp_path)
    gfile = tmp_path / "hashes.json"
    gfile.write_text(json.dumps(guard({"nano-0123456789abcdefghij": "credential NANOGPT_API_KEY"})))
    monkeypatch.setattr(leaks, "GUARD_FILE", str(gfile))
    monkeypatch.setattr(W, "_checked_out", lambda workspace, user, slug: proj)
    await branch(ws, "user2", "proj", "trabajo")
    (proj / "settings.py").write_text('KEY = "nano-0123456789abcdefghij"\n')
    with pytest.raises(BrokerError) as exc:
        await commit(ws, "user2", "proj", "Add settings")
    assert exc.value.status == 403 and "credential NANOGPT_API_KEY in settings.py" in str(exc.value)
    assert "0123456789" not in str(exc.value)
    staged = subprocess.run(["git", "-C", str(proj), "diff", "--cached", "--name-only"],
                            capture_output=True, text=True).stdout
    assert staged == "", "a refused commit leaves nothing staged"
    (proj / "settings.py").write_text('KEY = os.environ["NANOGPT_API_KEY"]\n')
    out = await commit(ws, "user2", "proj", "Add settings, read from the environment")
    assert out["action"] == "committed"
