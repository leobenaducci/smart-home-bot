#!/usr/bin/env python3
"""A backup is only a backup if it restores.

    python deploy/test_backup.py

So this makes one against a throwaway tree, encrypts it, unpacks it, and puts
it back over deliberately damaged state -- because the failures worth catching
here are the quiet ones. The job this replaces rsynced two trees into one
destination and lost every file whose name existed in both, exiting 0. A
pg_dump wrapper that ignores the exit status stores the error message and calls
it a backup. Both look like success.
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


def _raises(fn, needle):
    try:
        fn()
    except Exception as exc:                       # noqa: BLE001
        return needle.lower() in str(exc).lower()
    return False


tmp = Path(tempfile.mkdtemp(prefix="backup-test-"))
for sub in ("state/home-core", "config", "media", "backups"):
    (tmp / sub).mkdir(parents=True, exist_ok=True)
(tmp / "state/home-core/users.json").write_text(
    '[{"username":"someone","hash":"$2b$12$abc"}]', encoding="utf-8")
(tmp / "state/nanobot/user1/workspace").mkdir(parents=True)
(tmp / "state/nanobot/user1/workspace/USER.md").write_text(
    "what the assistant learned", encoding="utf-8")
# The collision: one service, two trees, one basename. `shared.json` exists in
# both and the two must not become one file.
(tmp / "config/nanobot").mkdir(parents=True)
(tmp / "config/nanobot/shared.json").write_text("config copy", encoding="utf-8")
(tmp / "state/nanobot/shared.json").write_text("state copy", encoding="utf-8")
# Empty by design: the broker runs `persistence false`, and a single-PC install
# needs no ssh key. Neither is a broken backup.
(tmp / "state/mosquitto").mkdir(parents=True)
(tmp / "config/ssh").mkdir(parents=True)
(tmp / "config/smart-home-bot.env").write_text(
    "OPENCODE_API_KEY=k\nBACKUP_ENCRYPTION_KEY=a-test-key-long-enough\n",
    encoding="utf-8")

# A config pointing entirely at the scratch tree, so nothing here can reach
# anything real.
from ruamel.yaml import YAML  # noqa: E402
y = YAML(); y.preserve_quotes = True
cfg = y.load((ROOT / "config" / "home-stack.example.yml").read_text(encoding="utf-8"))
# `plugins` belongs in this list and was missing from it, which is not a tidiness
# point: the example config's `paths.plugins` is the *live* /var/lib/home-stack
# /plugins, so on a machine that runs this stack the scratch config still named
# the household's real plugin directory. `entries()` duly archived it and
# `restore` duly wrote it back -- recreating the directory, which orphaned the
# bind mount every container holds on it. The admin page then reported the
# plugin directory as missing while it sat there on the host, and the only cure
# was recreating the container. It survived only because the archive was made
# from the same directory seconds earlier; a run against an older archive would
# have replaced a household's plugins with whatever that archive held.
cfg["paths"].update({"config": f"{tmp}/config", "state": f"{tmp}/state",
                     "media": f"{tmp}/media", "backups": f"{tmp}/backups",
                     "plugins": f"{tmp}/plugins"})
cfg["backups"] = {"encrypt": True, "keep": 2}
conf = tmp / "home-stack.yml"
with conf.open("w") as fh:
    y.dump(cfg, fh)

# For the subprocesses *and* for this process. The in-process section below
# imports backup.py, whose `D.CONFIG` is resolved at import and prefers the
# *deployed* copy at /var/lib/home-stack -- so on a machine that actually runs
# this stack, every check down there was answered from the live household
# config rather than from the scratch tree built above.
os.environ["HOME_STACK_CONFIG"] = str(conf)
os.environ["HOME_STACK_SECRETS"] = str(tmp / "config/smart-home-bot.env")
env = dict(os.environ)
PY_BIN = str(ROOT / ".venv" / "bin" / "python")


def run(*args):
    return subprocess.run([PY_BIN, str(ROOT / "deploy" / "backup.py"), *args],
                          capture_output=True, text=True, env=env, cwd=ROOT)


print("making one")
made = run()
check("the backup runs", made.returncode == 0, made.stdout[-400:] + made.stderr[-300:])
archives = sorted((tmp / "backups").glob("*.tar.gz.enc"))
check("it is encrypted", len(archives) == 1, [p.name for p in (tmp / "backups").iterdir()])

if archives:
    raw = archives[0].read_bytes()
    check("and the ciphertext gives nothing away",
          b"OPENCODE_API_KEY" not in raw and b"$2b$12$abc" not in raw)
    check("it verified itself before finishing",
          archives[0].with_suffix(archives[0].suffix + ".verified").exists())

print("\nand it restores what was damaged")
(tmp / "state/home-core/users.json").write_text("[]", encoding="utf-8")
shutil.rmtree(tmp / "state/nanobot")
(tmp / "config/nanobot/shared.json").write_text("clobbered", encoding="utf-8")
backup_id = archives[0].name.split(".tar.gz")[0] if archives else "none"

refused = run("restore", backup_id)
check("a restore without --confirm changes nothing",
      refused.returncode == 2
      and (tmp / "state/home-core/users.json").read_text() == "[]",
      refused.stdout[-200:])

done = run("restore", backup_id, "--confirm")
check("with --confirm it puts the user store back",
      json.loads((tmp / "state/home-core/users.json").read_text())[0]["username"] == "someone",
      (tmp / "state/home-core/users.json").read_text())
check("and recreates a directory that was deleted entirely",
      (tmp / "state/nanobot/user1/workspace/USER.md").read_text() == "what the assistant learned")
# The one the old rsync job got wrong, and the one naming the destination after
# the service still got wrong: two trees of one service, one basename. If they
# are merged in the archive, one of these two reads as the other.
check("two trees of one service stay two trees",
      (tmp / "state/nanobot/shared.json").read_text() == "state copy"
      and (tmp / "config/nanobot/shared.json").read_text() == "config copy",
      [(tmp / "state/nanobot/shared.json").read_text(),
       (tmp / "config/nanobot/shared.json").read_text()])
check("it says the containers still need deploying",
      "./home-stack deploy" in done.stdout, done.stdout[-200:])

# --- restoring one service --------------------------------------------------
#
# The shape a restore usually has: the documents came back wrong, or one
# person's assistant memory is gone. Putting the whole archive back drags every
# other service to the same hour -- including the user store and the
# credentials file, which is how a restore of one thing signs the whole house
# out.
print("\nand it restores one service without touching the others")
(tmp / "state/home-core/users.json").write_text("[]", encoding="utf-8")
shutil.rmtree(tmp / "state/nanobot")

narrowed = run("restore", backup_id, "--only", "nanobot", "--confirm")
check("the named service comes back",
      (tmp / "state/nanobot/user1/workspace/USER.md").read_text()
      == "what the assistant learned", narrowed.stdout[-300:])
check("and nothing else does",
      (tmp / "state/home-core/users.json").read_text() == "[]",
      "home-core was restored by a --only nanobot run")
check("it says what it left alone",
      "leaves" in narrowed.stdout and "home-core" in narrowed.stdout,
      narrowed.stdout[-400:])
check("and names only what it touched for the redeploy",
      "./home-stack deploy nanobot" in narrowed.stdout,
      narrowed.stdout[-200:])

# A typo in a service name must not be a run that restores nothing and says it
# is fine. That is the failure this whole file is written against.
typo = run("restore", backup_id, "--only", "home-papreless", "--confirm")
check("a service the archive does not hold is refused",
      typo.returncode == 1, typo.returncode)
# The refusal itself, not just a non-zero exit. Skipping an unknown name
# instead of refusing it also ends non-zero -- "nothing to restore" -- and two
# earlier versions of this check passed on that: one matched a service name
# that appears in the "restoring ... - only X" banner either way, and one
# matched "home-core" on the line listing what the run had left alone.
check("and it is a refusal, naming what the archive does hold",
      "holds nothing for home-papreless" in (typo.stdout + typo.stderr)
      and "it holds:" in (typo.stdout + typo.stderr), typo.stdout[-300:])

# --only without a value is not "restore everything".
bare = run("restore", backup_id, "--only")
check("--only with nothing after it is a usage error", bare.returncode == 2,
      bare.returncode)
check("and it changed nothing",
      (tmp / "state/home-core/users.json").read_text() == "[]")

# Narrowing still respects --confirm: the guard is not a property of the full
# restore, it is a property of restoring.
unconfirmed = run("restore", backup_id, "--only", "home-core")
check("a narrowed restore without --confirm changes nothing",
      unconfirmed.returncode == 2
      and (tmp / "state/home-core/users.json").read_text() == "[]",
      unconfirmed.stdout[-200:])

run("restore", backup_id, "--confirm")     # back to a whole tree for what follows

# --- a restore does not delete what the backup was told not to copy ----------
#
# A restore *replaces* a path, and four different rules now tell the copy to
# leave something out: REGENERABLE, `bulk_dirs` (a member's `media/`, out of a
# nightly, and named in the archive meta), `skip_dirs` and `skip_globs`. Replacing
# a live tree with an archive that deliberately lacks those deletes every one
# of them -- 241 MB of one member's images, or a plugin's `.git` -- while the
# run says "restored". `--verify` cannot see it: verify restores into a scratch
# tree, where there is nothing live to lose.
#
# A skip means "not worth copying". It has never meant "safe to delete".
print("\nand it keeps what the archive was told to leave out")
(tmp / "state/nanobot/user1/media").mkdir(parents=True, exist_ok=True)
(tmp / "state/nanobot/user1/media/photo.jpg").write_text(
    "a picture the assistant was sent", encoding="utf-8")
(tmp / "state/nanobot/user1/workspace/USER.md").write_text(
    "damaged", encoding="utf-8")

kept = run("restore", backup_id, "--only", "nanobot", "--confirm")
check("the archived half is restored",
      (tmp / "state/nanobot/user1/workspace/USER.md").read_text()
      == "what the assistant learned", kept.stdout[-300:])
check("and the media folder it never carried is still there",
      (tmp / "state/nanobot/user1/media/photo.jpg").read_text()
      == "a picture the assistant was sent",
      "a nightly archive's restore deleted the media it was told not to copy")
check("it says so, rather than keeping it quietly",
      "kept" in kept.stdout and "media" in kept.stdout, kept.stdout[-400:])

check("a directory that is empty by design is not a failed backup",
      "restored empty" not in (made.stdout + made.stderr),
      (made.stdout + made.stderr)[-300:])

# --- state it cannot reach is named, not omitted -----------------------------
#
# A member on `services.nanobot.runtime: kubernetes` keeps workspace and memory
# in a PVC, not under {state}/nanobot. The host directory is still declared and
# still copied, so on that install the archive verifies, restores, and contains
# none of that person's assistant memory -- while the entry reads `absent`,
# which is the same word a service that was never deployed gets.
#
# Capturing PVCs is not done. Looking finished while not doing it is the part
# that would hurt, so the gap goes into the archive and comes back out at
# verify. Driven through the CLI: a warning that never reaches a person is the
# same as no warning.

print("\nit says what it could not reach")

_saved = conf.read_text(encoding="utf-8")
conf.write_text(_saved.replace("    runtime: compose",
                               "    runtime: kubernetes", 1), encoding="utf-8")
_k8s_run = run()
_said = _k8s_run.stdout + _k8s_run.stderr
check("the backup still succeeds -- this is a gap, not a corruption",
      _k8s_run.returncode == 0, _said[-300:])
check("and it warns that nanobot state is not in it",
      "NOT in this backup" in _said and "nanobot" in _said, _said[-400:])

_newest = sorted((tmp / "backups").glob("*.tar.gz*"))[-1]
_ver = run("--verify", _newest.name.split(".tar.gz")[0])
_vsaid = _ver.stdout + _ver.stderr
check("verify repeats it from the archive, not from today's config",
      "NOT in this backup" in _vsaid, _vsaid[-400:])
check("and the archive still verifies clean", _ver.returncode == 0, _vsaid[-300:])

# Back to compose, so nothing downstream inherits it.
conf.write_text(_saved, encoding="utf-8")
# The negative is read off the run at the top of this file rather than by
# making another archive. Every extra create is one more second-resolution
# stamp competing for the same name and one more retention pass over the set
# the later checks read -- and the point here is a warning that is absent, not
# a backup that is present.
check("and a compose install says nothing of the kind",
      "NOT in this backup" not in made.stdout + made.stderr,
      (made.stdout + made.stderr)[-300:])

print("\nthings that must fail rather than pass quietly")
(tmp / "config/smart-home-bot.env").write_text(
    "OPENCODE_API_KEY=k\nBACKUP_ENCRYPTION_KEY=the-wrong-key\n", encoding="utf-8")
wrong = run("--verify")
check("a wrong key is refused, not worked around",
      wrong.returncode != 0 and "decrypt" in (wrong.stdout + wrong.stderr).lower(),
      (wrong.stdout + wrong.stderr)[-200:])

(tmp / "config/smart-home-bot.env").write_text("OPENCODE_API_KEY=k\n", encoding="utf-8")
nokey = run()
check("encryption on with no key refuses before doing any work",
      nokey.returncode != 0 and "BACKUP_ENCRYPTION_KEY" in nokey.stdout + nokey.stderr,
      (nokey.stdout + nokey.stderr)[-200:])
check("and it did not leave a partial archive behind",
      len(list((tmp / "backups").glob("*.partial"))) == 0)

typo = run("--verfiy")
check("a mistyped flag is refused, not treated as a request for a backup",
      typo.returncode == 2, (typo.stdout + typo.stderr)[-200:])

# The scratch tree used to be removed here, in the middle of the file. The
# in-process half below then had no config to read, so backup.py resolved its
# own -- which prefers the deployed copy, so on a machine that actually runs
# this stack every check after this line was answered from the household's
# real config and its real paths. It is removed at the end now.
# --- state on another machine ------------------------------------------------
#
# "Give a role a real address and only that role moves" is a documented
# feature, and this tool ignored it entirely: every path was copied with
# shutil, so on a stack where home-paperless is `role: storage` on its own box,
# the documents, the database and the recordings all read as *absent*, the
# archive verified green because absent paths are skipped, and retention then
# deleted the last backup that still had them. An empty backup that eats its
# own history.
#
# Each entry now carries the role that owns it, and the copy goes through the
# deployer's own Target -- which short-circuits to a local copy when the role
# resolves to this machine, so the single-PC install is unchanged.
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("bk", ROOT / "deploy" / "backup.py")
_bk = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_bk)

# backup.py resolves its config path at import, preferring the deployed copy.
# If that is what it found, everything below is answered from the household's
# real config and its real paths -- so stop rather than report on the wrong
# machine's state.
if not str(_bk.D.CONFIG).startswith(str(tmp)):
    raise SystemExit(
        f"REFUSING TO RUN: backup.py read {_bk.D.CONFIG}, not the scratch "
        f"config at {conf}.\nSet HOME_STACK_CONFIG before importing it.")

# The same question asked of *every* path, not just the config file. Naming
# four of the five above left `paths.plugins` at the example config's value --
# the live /var/lib/home-stack/plugins -- and this suite restores, so it wrote
# a household's real plugin directory back from an archive. A check at the end
# would have been too late: by then it has already been replaced. Anything
# added to PATH_KINDS later is covered without anybody remembering to.
_escaped = {k: v for k, v in (_bk.D.base_paths(cfg) or {}).items()
            if not str(v).startswith(str(tmp))}
if _escaped:
    raise SystemExit(
        "REFUSING TO RUN: these paths are outside the scratch tree and this "
        "suite writes to them:\n" +
        "\n".join(f"  paths.{k} = {v}" for k, v in sorted(_escaped.items())) +
        f"\nPoint every one of them inside {tmp} before importing backup.py.")

# --- the one role that is not under `hosts:` --------------------------------
#
# The VPS is not a role a household assigns services to -- it runs one proxy
# and holds no application data -- so its address is under `cloud.vps`. That is
# a good reason for it to live elsewhere in the config and no reason at all for
# a backup not to know about it. This file built its own target map and left it
# out, so a house with the proxy enabled got `KeyError: 'vps'` from the line
# collecting which roles are remote, before a single byte was copied.
print("\nthe proxy's own role is reachable too")
_vps_cfg = y.load(conf.read_text(encoding="utf-8"))
_vps_cfg.setdefault("cloud", {}).setdefault("vps", {})
_vps_cfg["cloud"]["vps"].update(enabled=True, host="vps.example", user="deploy")
_vps_targets = _bk.targets_for(_vps_cfg)
check("a vps target exists when the proxy is on", "vps" in _vps_targets,
      sorted(_vps_targets))
check("at the address cloud.vps names",
      _vps_targets["vps"].address == "vps.example", _vps_targets["vps"].address)
check("and it is not this machine", not _vps_targets["vps"].is_local)
check("every role a backup would pull from has a target",
      all(e["role"] in _vps_targets for e in _bk.entries(_vps_cfg, include_bulk=True)),
      sorted({e["role"] for e in _bk.entries(_vps_cfg, include_bulk=True)}
             - set(_vps_targets)))
_off = y.load(conf.read_text(encoding="utf-8"))
_off.setdefault("cloud", {}).setdefault("vps", {})["enabled"] = False
check("and there is none when the proxy is off",
      "vps" not in _bk.targets_for(_off), sorted(_bk.targets_for(_off)))


print("\nevery path knows which machine it is on")
_cfg = _bk._cfg()
_picked = _bk.entries(_cfg, include_bulk=True)
check("every entry carries a role", all(e.get("role") for e in _picked),
      [e["path"] for e in _picked if not e.get("role")][:3])

_roles = {e["service"]: e["role"] for e in _picked}
check("and it is the role the manifest gives that service",
      _roles.get("home-paperless") == "storage"
      and _roles.get("home-cameras") == "compute"
      and _roles.get("home-core") == "hub",
      {k: _roles.get(k) for k in ("home-paperless", "home-cameras", "home-core")})

_targets = _bk.targets_for(_cfg)
check("a role pointing at this machine resolves to a local target",
      _targets["hub"].is_local, _targets["hub"].address)

# The failure this exists to stop: a role with a real address must not be
# treated as local, or the backup silently reads this machine's disk instead.
_moved = dict(_cfg)
_moved["hosts"] = dict(_cfg.get("hosts") or {})
_moved["hosts"]["storage"] = {"address": "10.9.9.9", "user": "someone"}
_far = _bk.targets_for(_moved)
check("and a role with a real address does not",
      not _far["storage"].is_local, _far["storage"].address)

check("an entry whose role is not in hosts: is refused, not guessed",
      _raises(lambda: _bk._target_for({"service": "x", "role": "nowhere"}, _targets),
              "hosts"))

# --- the inventory is an inventory ------------------------------------------
#
# CLAUDE.md says docs/backups.md is derived from the manifest's `state:`
# entries and that adding one means adding it there too. That was an
# honour-system rule, and it had already been broken once: the *deployed*
# `{config}/home-stack.yml` -- the copy the admin page edits and a deploy
# reads -- was backed up but appeared nowhere in the file that tells a person
# what they have.
#
# Which is the failure that matters here. Nobody reads a backup tool's source
# to find out what it saved; they read the inventory, and they find out it was
# wrong on the day they are restoring. So the doc has to be checked, not
# trusted -- including the `skip` entries, because "this is deliberately not
# backed up, and here is why" is exactly what somebody restoring needs to
# read before they go looking for it.

print("\nthe household's own service definitions are in the archive")
# A plugin is not a service's state -- it is what *declares* services -- so
# nothing in the manifest could carry it and it was in no archive at all. It
# lives outside the package tree on purpose (sanitize.py never looks there, a
# clone never ships it, a checkout cannot destroy it), and every one of those
# is also a reason it is not in git: the archive is the only copy a household
# has of the services it added itself.
_pl = tempfile.mkdtemp(prefix="plugins-")
os.makedirs(os.path.join(_pl, "home-switches"), exist_ok=True)
with open(os.path.join(_pl, "home-switches", "plugin.yml"), "w") as fh:
    fh.write("contract: 1\nname: home-switches\n")
_pcfg = {**cfg, "paths": {**(cfg.get("paths") or {}), "plugins": _pl}}
_rows = _bk.entries(_pcfg, False)
_hit = [r for r in _rows if r["path"] == _pl]
check("the plugin directory is an entry", len(_hit) == 1, [r["path"] for r in _rows][:4])
check("  kept, not skipped",
      _hit and _hit[0]["policy"] == "essential", _hit and _hit[0]["policy"])
check("  and it is in an ordinary run",
      _hit and _hit[0]["policy"] != "bulk")
# The two were argued about together twice and they are not the same question,
# so both halves are pinned here rather than left to a comment.
#
# `.git` is the only copy of unpushed commits, local branches, stashes and the
# reflog, and a plugin made with `git init` has no remote at all -- the cost of
# being wrong is a household's own service, gone.
#
# `*.log` records what already happened: nothing is rebuilt from it and no
# service fails to come back without it. 10.8 MB of one plugin's 13.0 MB here
# was two unrotated files, re-encrypted and re-uploaded every night.
check("  a plugin's logs are not copied",
      _hit and "*.log" in (_hit[0].get("skip_globs") or []),
      _hit and _hit[0].get("skip_globs"))
check("  but its .git is",
      _hit and not any(".git" in s for s in
                       (_hit[0].get("skip_globs") or [])
                       + (_hit[0].get("skip_dirs") or [])),
      _hit and (_hit[0].get("skip_globs"), _hit[0].get("skip_dirs")))
# And the skip reaches the copy rather than sitting in the record: `kept_out()`
# is what a restore reads to know what it must not delete.
check("  and the archive records the omission",
      _hit and "*.log" in _bk.kept_out(_hit[0]), _hit and _bk.kept_out(_hit[0]))
# A house with no plugins must not get a row for a directory that is not
# there: an entry whose path does not exist is a warning at every backup.
_none = _bk.entries({**cfg, "paths": {**(cfg.get("paths") or {}),
                                    "plugins": os.path.join(_pl, "nope")}}, False)
check("a house with no plugins gets no entry",
      not [r for r in _none if r["service"] == "plugins"])
# A plugin may be named absolutely -- `plugins: [/home/me/my-project]` is the
# documented form -- and those live wherever the household keeps the project.
# Covering only the default directory would back some households up completely
# and others not at all, and both look identical from in here.
_far = tempfile.mkdtemp(prefix="plugin-elsewhere-")
with open(os.path.join(_far, "plugin.yml"), "w") as fh:
    fh.write("contract: 1\nname: elsewhere\n")
_acfg = {**_pcfg, "plugins": [_far]}
_rows2 = [r for r in _bk.entries(_acfg, False) if r["service"] == "plugins"]
check("a plugin outside paths.plugins is captured too",
      any(r["path"] == _far for r in _rows2), [r["path"] for r in _rows2])
# And the one inside is not archived twice: a restore with two copies has no
# way to say which is authoritative, which is what the dedupe below is for.
_inside = os.path.join(_pl, "home-switches")
_bcfg = {**_pcfg, "plugins": [_inside]}
_rows3 = [r["path"] for r in _bk.entries(_bcfg, False) if r["service"] == "plugins"]
check("  and one inside it is not captured twice",
      _rows3 == [_pl], _rows3)

print("\nthe inventory covers every declared path")

_man = _bk.D.load_yaml(_bk.D.MANIFEST)
_man["_plugins"] = []           # a plugin documents its own state, not this file
_doc = (_bk.D.ROOT / "docs" / "backups.md").read_text(encoding="utf-8")

# The manifest writes `{paths.state}`; the doc writes `{state}`, because it is
# prose and the shorter form reads. Same path, two spellings.
def _as_documented(path: str) -> str:
    for long, short in (("{paths.state}", "{state}"),
                        ("{paths.config}", "{config}"),
                        ("{paths.media}", "{media}")):
        path = path.replace(long, short)
    return path

# Straight from the manifest rather than through `all_services()`: the doc
# documents cloud-proxy whether or not this config has the VPS turned on, and
# a check that only looked at enabled services would let a path go
# undocumented for as long as its service was off.
_declared = {}
# Both keys: `optional_services` is where cloud-proxy lives, and a check that
# read only `services:` would quietly exempt every service that is off by
# default -- which is exactly the set nobody remembers to document.
_all_declaring = {**_man.get("services", {}), **_man.get("optional_services", {})}
for _svc, _spec in _all_declaring.items():
    for _unit in _spec.get("units", []):
        for _st in (_unit.get("state") or []):
            _declared.setdefault(_st["path"], _st.get("backup", "essential"))

_undocumented = sorted(_as_documented(p) for p in _declared
                       if _as_documented(p) not in _doc)
check("every state path in the manifest appears in docs/backups.md",
      not _undocumented,
      f"{len(_undocumented)} missing: {_undocumented}")

# And the other direction, narrowed to where a mistake is dangerous. The
# "Do not bother" table names paths that are deliberately *not* state entries
# -- YOLO weights, a cache -- so it is free-form by design. The tables above it
# are promises: every path there is something a person is being told they can
# get back. One of those naming a path nothing declares reads as "we have a
# backup of that" right up until somebody needs it.
import re as _re
_promises = _doc[:_doc.index("### Do not bother")]
_doc_paths = set(_re.findall(r"`(\{(?:state|config|media)\}/[^`]+)`", _promises))
_declared_doc_form = {_as_documented(p) for p in _declared}


def _covered(path: str) -> bool:
    """Named outright, or inside a directory that is -- devices.json is
    backed up because {config}/home-voice is."""
    return any(path == d or path.startswith(d.rstrip("/") + "/")
               for d in _declared_doc_form)


_unbacked = sorted(p for p in _doc_paths if not _covered(p))
check("and every path it promises is one the manifest actually declares",
      not _unbacked, f"{len(_unbacked)} promised but not declared: {_unbacked}")


# --- what a restore is allowed to write over ----------------------------------
# An archive records absolute paths, so a migrated or crafted one could name
# anything. The guard refuses a path outside the household's own roots -- and
# the roots have to include the plugin directories, because a plugin is allowed
# to live anywhere (`plugins: [/home/me/my-project]` is the documented form)
# and `entries()` archives it where it really is. With only `paths.*` as roots
# this raised on the two plugins in this household, and it raises *inside the
# loop that builds the plan*, so one refused entry took the entire restore with
# it -- every service, not just that one.
print("\na restore stays inside the household's own roots")
_rcfg = {"paths": {"config": f"{tmp}/config", "state": f"{tmp}/state",
                   "media": f"{tmp}/media", "backups": f"{tmp}/backups",
                   "plugins": f"{tmp}/plugins"},
         "plugins": [f"{tmp}/elsewhere/my-project"]}
for _d in ("config", "state", "media", "backups", "plugins"):
    Path(f"{tmp}/{_d}").mkdir(parents=True, exist_ok=True)
Path(f"{tmp}/elsewhere/my-project").mkdir(parents=True, exist_ok=True)


def _allowed(path):
    try:
        _bk._validate_restore_path(path, _rcfg)
        return True
    except Exception:
        return False


check("a declared state path is allowed", _allowed(f"{tmp}/state/home-core"))
check("a plugin outside paths.plugins is allowed too -- it is where it lives",
      _allowed(f"{tmp}/elsewhere/my-project"),
      "roots: " + str(_bk._restore_roots(_rcfg)))
check("somewhere else entirely is refused", not _allowed("/etc/passwd"))
check("and it is refused after resolving, not before",
      not _allowed(f"{tmp}/state/../../../etc/hosts"))
check("a relative path is refused outright", not _allowed("state/home-core"))

# --- what is weight rather than data ------------------------------------------
# Registering `home-backups` as a plugin put 34 MB into every archive, of which
# 292 KB was content: the rest was a virtualenv and bytecode. Both are rebuilt
# from something already in the archive, and a virtualenv restored onto another
# machine is worse than absent.
#
# The half that matters is the confirmation. Skipping by name alone would drop a
# household directory that happens to be called `venv`, silently, which is the
# failure this whole file is written against.
print("\na virtualenv is weight; a directory that shares its name is not")
_tree = tmp / "skiptest"
(_tree / "venv" / "lib").mkdir(parents=True)
(_tree / "venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
(_tree / "venv" / "lib" / "big.so").write_bytes(b"x" * 50000)
(_tree / "__pycache__").mkdir()
(_tree / "__pycache__" / "a.pyc").write_bytes(b"x" * 100)
(_tree / "config.yaml").write_text("real: data\n")
(_tree / "data" / "venv").mkdir(parents=True)
(_tree / "data" / "venv" / "notes.txt").write_text("household data\n")

_out = tmp / "skipout"
shutil.copytree(_tree, _out, symlinks=True,
                ignore=_bk.D._skip_ignore(_bk.REGENERABLE))
_got = sorted(str(q.relative_to(_out)) for q in _out.rglob("*"))
check("the virtualenv is left out", "venv" not in _got, _got)
check("  and so is __pycache__", "__pycache__" not in _got, _got)
check("real files are still copied", "config.yaml" in _got, _got)
check("a directory called venv with no pyvenv.cfg is DATA, and is kept",
      "data/venv/notes.txt" in _got, _got)
check("the copy is smaller than the source",
      sum(f.stat().st_size for f in _out.rglob("*") if f.is_file())
      < sum(f.stat().st_size for f in _tree.rglob("*") if f.is_file()))
# The rule has to reach the archive, not just exist. `_copy_entry` is the only
# caller, and passing nothing there would make every check above decorative.
#
# Asserted by watching what it hands to `pull` rather than by grepping its
# source for `skip=REGENERABLE`, which is what this used to do: that broke the
# moment the expression grew a second term, and a test that fails when correct
# code is edited teaches people to edit the test.
class _SpyTarget:
    is_local = True

    def __init__(self):
        self.skip = None

    def pull(self, path, dest, is_dir=True, skip=None):
        self.skip = list(skip or [])
        dest.mkdir(parents=True, exist_ok=True)
        return True


def _skip_for(**extra):
    spy = _SpyTarget()
    entry = {"path": str(_tree), "stored_as": "x", "kind": "dir",
             "policy": "essential", "role": "hub", **extra}
    _bk._copy_entry(entry, tmp / f"spy-{len(extra)}-{sorted(extra)}", {"hub": spy})
    return spy.skip


_skips = _skip_for()
check("and _copy_entry actually passes the regenerable list to pull",
      all(r in _skips for r in _bk.REGENERABLE), _skips)

# The three per-entry lists, which are why the expression grew. `bulk_dirs` is
# "large, static and yours, carried elsewhere"; `skip_dirs` is "a
# tool rebuilds it" and never returns; `skip_globs` names a kind of file rather
# than a directory. All three have to reach `pull` or the declaration is
# decorative -- which is the whole point of this section.
_skips = _skip_for(bulk_dirs=["media"], skip_dirs=["build"],
                   skip_globs=["*.log"])
check("  a bulk_dirs entry reaches it too", "media" in _skips, _skips)
check("  and a skip_dirs entry does", "build" in _skips, _skips)
check("  and a skip_globs entry does", "*.log" in _skips, _skips)
check("  without dropping the regenerable ones",
      all(r in _skips for r in _bk.REGENERABLE), _skips)
# Logs are not *globally* regenerable, and that is the claim being pinned: a
# `*.log` in a service's state directory is sometimes the only record of what
# that service did, and "probably regenerable" is not the same claim as
# "rebuilt from something else in this archive".
#
# A plugin is the one place they are skipped, per entry rather than here -- see
# "a plugin's logs are not copied" above for why that case is different.
check("logs are not treated as regenerable",
      not any("log" in r for r in _bk.REGENERABLE), _bk.REGENERABLE)

print("\nretention keeps a shape, not a count")

# 120 nightly archives, so every tier has something to choose from and the
# overlap between them is real rather than incidental.
import datetime as _dt
_base = _dt.datetime(2026, 5, 5, 4, 0, 0)
_nightly = [_bk.Path(f"/x/{(_base + _dt.timedelta(days=_i)).strftime(_bk.STAMP)}"
                     f".tar.gz.enc") for _i in range(120)]

_flat = _bk.retained(_nightly, 7, 0, 0)
check("both new tiers at zero is exactly the old flat count",
      len(_flat) == 7 and _nightly[-1] in _flat, len(_flat))

_tiered = _bk.retained(_nightly, 2, 4, 6)
check("keep+weekly+monthly spans months at a fraction of the files",
      len(_tiered) == 8, len(_tiered))
check("  the newest is always kept", _nightly[-1] in _tiered)
check("  and something from four months back survives",
      any(_p.name.startswith("2026-05") for _p in _tiered),
      sorted(_p.name[:10] for _p in _tiered))
# The tiers overlap by design: today is this week's and this month's too. If
# they were summed rather than unioned this would be 12.
check("  overlapping tiers are one file, not three",
      len(_tiered) < 2 + 4 + 6, len(_tiered))

# The whole point of retention is that it never runs the directory to empty.
check("no archives means nothing to keep and nothing to delete",
      _bk.retained([], 2, 4, 6) == set())
check("every tier at zero keeps nothing -- and prune() refuses to act on that",
      _bk.retained(_nightly, 0, 0, 0) == set())

print("\nthe mirror refuses a directory it would empty")

# `export_tree` refreshes with `rsync --delete`. Every check here is about the
# one bug that has a cost: a live path *inside* the mirror root gets deleted on
# the first run, and there is no archive of it because that is what the mirror
# was replacing.
_live = {"config": "/var/lib/hs/config", "state": "/mnt/d/state",
         "media": "/mnt/d", "plugins": "/var/lib/hs/plugins",
         "backups": "/mnt/d/backups"}


def _export(path):
    return _bk.export_root({"paths": _live, "plugins": [],
                            "backups": ({"export": {"path": path}} if path
                                        else {})})


def _refused(path, label):
    try:
        _export(path)
        check(label, False, "accepted")
    except _bk.D.DeployError:
        check(label, True)


check("a sibling of the live paths is fine",
      str(_export("/mnt/d/export")) == "/mnt/d/export")
check("unset means no mirror, not a default one", _export(None) is None)
_refused("export", "a relative path is refused")
_refused("/mnt/d", "the directory holding paths.state is refused")
_refused("/mnt/d/", "  and so is the same one with a trailing slash")
_refused("/", "root is refused")
_refused("/var/lib/hs", "a directory holding paths.config is refused")
_refused(os.path.expanduser("~/.local/share/home-stack/x"),
         "inside the deploy tree is refused -- a deploy would --delete it")
_refused(str(_bk.D.ROOT / "export"), "and inside the checkout, for the same reason")
# The message has to name what it found, or the person fixing it is guessing.
try:
    _export("/mnt/d")
except _bk.D.DeployError as _exc:
    check("  and the refusal names the live path it would have eaten",
          "paths.state" in str(_exc) and "/mnt/d/state" in str(_exc), str(_exc))

shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")
