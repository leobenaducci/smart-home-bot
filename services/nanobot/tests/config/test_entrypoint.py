"""What `entrypoint.sh` puts in a container's workspace.

There is one entrypoint and one `config/` now. Both used to be doubled — a
separate `entrypoint-multiuser.sh`, and a whole `house-config/` mounted at the
same path as `shared-config/` — and the pairs had already drifted: `TOOLS.md`
and `HEARTBEAT.md` existed twice, byte-identical, waiting for somebody to edit
one of them.

Collapsing them puts real branching in a shell script that nothing tested. The
four things below are the ones whose failure is silent:

- **The instance's file wins, the base fills the rest.** Get that backwards and
  the room-facing assistant boots with the per-member persona — which knows
  about chores, files and somebody's documents — in a room anybody can walk
  into.
- **`USER.md` is copied, not symlinked.** Memory consolidation writes to it and
  the mount is read-only, so a symlink is `EROFS` on every consolidation. It is
  also never overwritten: doing so would throw away everything the agent had
  learned, on every restart.
- **`null` in an overlay deletes.** An instance has to be able to hold *fewer*
  credentials than the base; listing a provider is the same as demanding its
  credential.
- **A broken overlay is loud, not fatal.** It costs the difference, not the
  assistant.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO / "entrypoint.sh"
CONFIG = REPO / "config"


@pytest.fixture
def run(tmp_path):
    """Run the entrypoint with a throwaway HOME and a stub `nanobot`."""
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "nanobot").write_text('#!/bin/sh\necho "nanobot $*"\n')
    (stub / "nanobot").chmod(0o755)

    class Result:
        __slots__ = ("stdout", "stderr")

    def _run(instance="", config=CONFIG, home=None):
        home = home or tmp_path / f"home-{instance or 'none'}"
        home.mkdir(exist_ok=True)
        env = {**os.environ,
               "PATH": f"{stub}:{os.environ['PATH']}",
               "HOME": str(home),
               "NANOBOT_CONFIG": str(config),
               "NANOBOT_INSTANCE": instance}
        # Files, not pipes. The entrypoint backgrounds the FAMILY.md refresh
        # loop, which inherits stdout and stderr and never exits — so
        # `capture_output=True` waits on a pipe that nothing will ever close,
        # and the test hangs rather than failing.
        out = tmp_path / f"out-{instance or 'none'}"
        err = tmp_path / f"err-{instance or 'none'}"
        with open(out, "w") as o, open(err, "w") as e:
            subprocess.run(["sh", str(ENTRYPOINT)], env=env,
                           stdout=o, stderr=e, timeout=60)
        res = Result()
        res.stdout = out.read_text(encoding="utf-8")
        res.stderr = err.read_text(encoding="utf-8")
        return home / ".nanobot", res

    return _run


def _config(root):
    return json.loads((root / "config.json").read_text(encoding="utf-8"))


def test_a_member_instance_gets_the_base_of_everything(run):
    root, proc = run("user1")
    ws = root / "workspace"
    for name in ("SOUL.md", "AGENTS.md", "TOOLS.md", "HEARTBEAT.md", "MORNING.md"):
        assert (ws / name).is_symlink()
        assert Path(os.readlink(ws / name)) == CONFIG / name
    # USER.md is per person and seeded by the deployer from the admin page's
    # profiles, so the entrypoint must not invent one.
    assert not (ws / "USER.md").exists()
    assert "gateway --api-port" in proc.stdout


def test_the_house_instance_overrides_only_what_it_has(run):
    root, _ = run("house")
    ws = root / "workspace"
    inst = CONFIG / "instances" / "house"
    for name in ("SOUL.md", "AGENTS.md", "MORNING.md"):
        assert Path(os.readlink(ws / name)) == inst / name, name
    # The whole point of one shared directory: these exist once.
    for name in ("TOOLS.md", "HEARTBEAT.md"):
        assert Path(os.readlink(ws / name)) == CONFIG / name, name
        assert not (inst / name).exists(), f"{name} is back in two places"


def test_the_house_config_is_whole_and_holds_fewer_credentials(run):
    """Not an overlay, and this is what that buys.

    Listing a provider is the same as demanding its credential — nanobot
    refuses to start on an unset env reference — so an overlay would make
    inheriting the default for the one assistant anybody in a room can talk to.

    `together_ai` used to be the example of what it withholds, and is no longer
    a fair one: a cross-provider fallback is routed to that provider's own
    client, and a config that does not declare it has no client to build. With
    the block absent the deployer wrote the fallback in anyway and nanobot
    logged `Fallback provider together_ai is not configured in this process`
    during a real OpenCode Go outage, then answered the room with an error —
    for the assistant this file's own comments call the most latency-sensitive
    caller in the house. Its credential was in this container's environment
    the whole time, so nothing was being withheld; only the rescue was.

    What the rule is really about is the credentials this instance does NOT
    hold, and those are unchanged: nothing user-bound, and no scraper.
    """
    root, _ = run("house")
    cfg = _config(root)
    assert "brightdata" not in cfg["tools"]["mcpServers"]
    base = json.loads((CONFIG / "config.json").read_text(encoding="utf-8"))
    assert "together_ai" in base["providers"], "the base is what it differs from"


    # Every provider it declares has to be one it can actually build, which is
    # the property the missing block broke.
    for name, block in (cfg.get("providers") or {}).items():
        if name.startswith("_"):
            continue
        assert "apiKey" in block or "apiBase" in block, name


def test_user_md_is_seeded_once_and_then_belongs_to_the_agent(run, tmp_path):
    home = tmp_path / "house-home"
    root, _ = run("house", home=home)
    user_md = root / "workspace" / "USER.md"
    # A copy, not a link: the mount is read-only and Dream writes here.
    assert user_md.is_file() and not user_md.is_symlink()
    assert os.access(user_md, os.W_OK)

    user_md.write_text("what the house learned\n", encoding="utf-8")
    run("house", home=home)
    assert user_md.read_text(encoding="utf-8") == "what the house learned\n"


def test_the_instances_skills_are_replaced_wholesale(run):
    root, _ = run("house")
    skills = root / "workspace" / "skills"
    shipped = {p.name for p in (CONFIG / "instances" / "house" / "skills").iterdir()
               if p.is_dir()}
    assert {p.name for p in skills.iterdir() if p.is_dir()} == shipped
    assert shipped, "the house instance ships skills; this test proves nothing empty"


def test_an_overlay_merges_and_null_deletes(run, tmp_path):
    cfg_dir = tmp_path / "cfg"
    shutil.copytree(CONFIG, cfg_dir)
    (cfg_dir / "config.userX.json").write_text(json.dumps({
        "providers": {"together_ai": None},
        "gateway": {"morningGreeting": {"enabled": False}},
    }), encoding="utf-8")

    root, proc = run("userX", config=cfg_dir)
    cfg = _config(root)
    assert cfg["gateway"]["morningGreeting"]["enabled"] is False
    assert "together_ai" not in cfg["providers"]
    assert "merged overlay" in proc.stderr


def test_a_malformed_overlay_costs_the_difference_not_the_assistant(run, tmp_path):
    cfg_dir = tmp_path / "cfg"
    shutil.copytree(CONFIG, cfg_dir)
    (cfg_dir / "config.userY.json").write_text("not json", encoding="utf-8")

    root, proc = run("userY", config=cfg_dir)
    assert "could not be merged" in proc.stderr
    # The base config is in place and valid — the container comes up as the
    # ordinary assistant rather than not at all.
    from nanobot.config.schema import Config
    Config.model_validate(_config(root))


def test_no_instance_named_runs_the_base_unchanged(run):
    root, _ = run("")
    base = json.loads((CONFIG / "config.json").read_text(encoding="utf-8"))
    assert _config(root) == base
    assert not (root / "workspace" / "USER.md").exists()


def test_the_profile_overlay_is_fetched_unredacted():
    """The entrypoint asks for the real passwords, and the page asks for stars.

    `/profiles/api/export` grew a `?redact=1` so the mailboxes page can show
    what this container receives without printing IMAP passwords into devtools.
    The container is the one caller that must not pass it: the overlay is what
    the email channel logs in with, and a redacted one is a config that looks
    complete and authenticates against nothing.

    Nothing else can catch this. `--check-contract` compares exports against
    the `${VAR}` a compose file reads, and a query string is neither — so the
    pairing between «the page redacts» and «the container does not» is only
    ever asserted here.
    """
    src = ENTRYPOINT.read_text(encoding="utf-8")
    fetch = [ln for ln in src.splitlines() if "/export" in ln]
    assert fetch, "the entrypoint no longer fetches the profile overlay"
    for line in fetch:
        assert "redact" not in line, line
