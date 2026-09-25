#!/usr/bin/env python3
"""Make a backup, check it is real, and put it back.

    ./home-stack backup                 make one
    ./home-stack backup --list          what exists, how big, whether it verified
    ./home-stack backup --verify <id>   restore it into a scratch tree and check
    ./home-stack restore <id>           put it back, after saying what will change

What goes in is derived from `deploy/manifest.yml`, not from a list kept
alongside it. Every `state:` entry is in unless it says otherwise:

    backup: skip        regenerable. The whisper model cache is gigabytes and
                        re-downloads itself; copying it nightly buys nothing.
    backup: bulk        recordings and scanned documents. Never in an archive:
                        they are hundreds of gigabytes, unchanged between runs,
                        and whatever uploads them is pointed at them directly.
                        Named rather than silently dropped -- `not_archived` in
                        the archive's own meta, `not_mirrored` in the mirror's
                        -- because an archive that omits a member's assistant
                        images reads exactly like one that does not.
    backup: postgres    dumped with pg_dump, not copied. A file-level copy of a
                        running cluster is torn across relation files and
                        pg_wal and generally refuses to start on restore.

Three things this is built to avoid, all of them real:

**Two sources, one destination.** The job this replaces rsynced the config tree
and the state tree into the same directory. Four names exist in both, so four
files were silently overwritten and rsync exited 0. Naming the destination
after the *service* is not enough -- those four pairs belong to one service
each -- so the destination is derived from the source path and asserted unique
before anything is copied. A collision is an error, never a merge.

**A backup nobody has restored.** `--verify` restores into a scratch tree and
checks the result, rather than trusting that a green run produced something.

**Deleting the last good copy to make room.** Retention runs after a new backup
has been written and verified, never before -- and it only ever considers
archives this tool wrote, so pointing `paths.backups` at a directory that
already holds something else cannot delete it.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy"))

import deploy as D  # noqa: E402

MANIFEST_NAME = "backup.json"
CONTENTS = "contents"
KEY_SECRET = "BACKUP_ENCRYPTION_KEY"
# The id is a UTC timestamp, and only files matching it are ours. `prune()`
# deletes what this matches, so it must not match a household's own tarballs:
# the example config invites pointing an existing backup directory at this.
STAMP = "%Y-%m-%dT%H-%M-%S"
ARCHIVE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.tar\.gz(\.enc)?$")


def _cfg() -> dict:
    cfg = D.load_yaml(D.CONFIG)
    D.apply_service_renames(cfg)
    D.apply_config_defaults(cfg)
    return cfg


def backups_root(cfg: dict) -> Path:
    """Where archives live. Absolute, for the same reason state paths are.

    A relative value resolves against the process's working directory, so the
    same command means one directory from the checkout and another from cron --
    and if it lands inside the deploy tree, the next deploy's `rsync --delete`
    takes every archive with it. That is the failure `guard_state_paths()`
    exists for, one level worse.
    """
    root = Path((cfg.get("paths") or {}).get(
        "backups", "/var/lib/home-stack/backups"))
    if not root.is_absolute():
        raise D.DeployError(
            f"paths.backups is {root}, which is relative. It must be an "
            f"absolute path, outside the tree a deploy replaces.")
    return root


# Directories that are rebuilt from something else already in the archive, and
# so are weight rather than data.
#
# `home-backups` is what made this worth having: registering it as a plugin put
# 34 MB into every archive, of which 292 KB was content -- the rest a 24 MB
# virtualenv and 6 MB of unrotated logs. A virtualenv is the clearest case
# there is: it is a build product of `requirements.txt`, it holds compiled
# artefacts for one interpreter on one architecture, and restoring it onto a
# different machine is worse than not having it.
#
# Matched on the name and then **confirmed by looking inside** -- see
# `_skip_ignore` in deploy.py. `venv/` is only skipped when it holds
# `pyvenv.cfg`, because a household directory that merely shares the name is
# data, and silently dropping it is exactly the failure this file exists to
# prevent. That confirmation is only possible for a local copy; a pull over
# rsync matches the name alone, which is the one place this rule is blunter
# than it should be. Every plugin today is `role: hub`, so every plugin copy
# today is local.
#
# Logs are deliberately NOT here. `*.log` in a state directory is sometimes the
# only record of what a service did, and "probably regenerable" is not the same
# claim as "rebuilt from something else in this archive".
REGENERABLE = ["venv", ".venv", "__pycache__"]


def _restore_roots(cfg: dict) -> list[Path]:
    """The configured path roots a restore may write under.

    Archives record absolute paths; before writing them back we check that
    each one still lands under a root this household owns, so a migrated or
    crafted archive cannot overwrite files outside the intended tree.
    """
    roots = []
    for kind in D.PATH_KINDS:
        val = (cfg.get("paths") or {}).get(kind)
        if val:
            roots.append(Path(val).resolve())
    # A plugin is allowed to live anywhere -- `plugins: [/home/me/my-project]`
    # is the documented form -- so `paths.plugins` is not the whole answer.
    # `entries()` already archives those directories at their real location;
    # leaving them out here made every restore in this household abort, not
    # just those two items: the check raises, and it raises inside the loop
    # that builds the plan, so one out-of-root entry took the whole restore
    # with it. Same source as the archive side, so the two cannot disagree.
    try:
        roots += [Path(d).resolve() for d in D.plugin_dirs(cfg)]
    except Exception:  # noqa: BLE001 - a broken plugins: list is the deploy's
        pass           # problem to report, not a reason to block a restore
    return roots


def _validate_restore_path(path: str, cfg: dict) -> Path:
    """Refuse to restore paths that escape the configured roots.

    Returns the resolved path so callers do not re-resolve inconsistently.
    """
    live = Path(path)
    if not live.is_absolute():
        raise D.DeployError(f"{path}: restore path must be absolute")
    resolved = live.resolve()
    roots = _restore_roots(cfg)
    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise D.DeployError(
        f"{path}: refusing to restore outside configured roots "
        f"({', '.join(str(r) for r in roots)})")


def _stored_names(service_paths: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """Where each (service, path) lands inside the archive, uniquely.

    The obvious key -- service plus basename -- is the bug this file exists to
    prevent. `{paths.config}/nanobot` and `{paths.state}/nanobot` are both
    nanobot's, and both end in `nanobot`; so are home-voice's two, mosquitto's
    two, ntfy's two and paperless's two. Qualify a
    contested basename with the tree it came from, and assert the result is
    injective rather than letting `copytree(dirs_exist_ok=True)` merge them.
    """
    counts: dict[tuple[str, str], int] = {}
    for service, path in service_paths:
        key = (service, Path(path).name)
        counts[key] = counts.get(key, 0) + 1

    out: dict[tuple[str, str], str] = {}
    taken: dict[str, str] = {}
    for service, path in service_paths:
        p = Path(path)
        name = p.name if counts[(service, p.name)] == 1 else f"{p.parent.name}-{p.name}"
        stored = f"{service}/{name}"
        if taken.get(stored, path) != path:
            raise D.DeployError(
                f"{path} and {taken[stored]} would both be stored as "
                f"{stored!r}. Two state paths cannot share one place in the "
                f"archive; rename one, or the restore cannot tell them apart.")
        taken[stored] = path
        out[(service, path)] = stored
    return out


def entries(cfg: dict, include_bulk: bool = False) -> list[dict]:
    """Every state path worth keeping, with the service that owns it.

    Deduplicated on the resolved path: two units legitimately mount the same
    file (home-core's portal and its entry page both read users.json), and
    copying it twice into two directories would make the restore ambiguous
    about which copy is authoritative.
    """
    manifest = D.load_yaml(D.MANIFEST)
    # `all_services()` merges plugin-contributed services out of this key, and
    # only deploy.py's main() ever set it. Without this a household's own
    # services are absent from every backup, and absent silently -- they never
    # enter the loop, so they are not even counted as "not on disk yet".
    manifest["_plugins"] = D.load_plugins(cfg)

    picked, seen = [], set()
    for service, spec in (D.all_services(manifest, cfg)).items():
        enabled = ((cfg.get("services") or {}).get(service) or {})
        # The admin page is the only way to turn a disabled service back on, so
        # `enabled: false` does not apply to it -- the same exemption deploy.py
        # makes. Its state is the live config and the credentials file; drop
        # those and the archive cannot bring anything back up.
        if service != "admin" and not enabled.get("enabled", True):
            continue
        for unit in spec.get("units", []):
            # Interpolate the whole entry, not just `path`: `db_container` and
            # friends are as much config as the path is, and a manifest that
            # templates one of them would otherwise reach the shell unresolved.
            for st in D.interpolate(unit.get("state") or [], cfg):
                if st["path"] in seen:
                    continue
                seen.add(st["path"])
                picked.append((service, unit, st))

    # Destination names are computed over every state path, not only the ones
    # this run keeps, so an entry lands in the same place whether or not it is
    # one of the bulk paths this run excludes.
    names = _stored_names([(service, st["path"]) for service, _, st in picked])
    spec_role = {name: spec.get("role", "hub")
                 for name, spec in D.all_services(manifest, cfg).items()}

    # The household's own service definitions. Not any service's state -- they
    # are what *declares* services -- so nothing in the manifest could carry
    # them, and they were in no archive at all.
    #
    # A plugin lives outside the package tree on purpose: sanitize.py never
    # looks there, a clone of this repository never ships it, and a checkout
    # cannot destroy it. Every one of those is a reason it is also not in git,
    # which makes this the only copy a household has. Restoring a machine
    # without it brings back a stack that has forgotten the services this
    # house added.
    # Every configured plugin directory, not just `paths.plugins`. A plugin may
    # be named absolutely -- `plugins: [/home/me/my-project]` is in the
    # documented form -- and those live wherever the household keeps the
    # project. Backing up only the default directory would cover some
    # households completely and others not at all, which is the worst of the
    # three possible behaviours because both look the same from here.
    out_extra, seen_dirs = [], set()
    roots = [(D.base_paths(cfg) or {}).get("plugins")]
    try:
        roots += [str(d) for d in D.plugin_dirs(cfg)]
    except Exception:  # noqa: BLE001 - a broken plugins: list is the deploy's
        pass           # problem to report, not a reason to skip the backup
    for root in roots:
        if not root or root in seen_dirs or not Path(root).is_dir():
            continue
        # A plugin under `paths.plugins` is already inside the entry above it.
        # Copying it twice is what the dedupe further down exists to prevent:
        # a restore then has two copies and no way to say which is
        # authoritative.
        if any(Path(root).resolve().is_relative_to(Path(d).resolve())
               for d in seen_dirs):
            continue
        seen_dirs.add(root)
        # The basename is not unique -- `/home/ana/smart-switches` and
        # `/srv/work/smart-switches` are two different plugins with one name --
        # and `stored_as` is a directory inside the archive. Two entries
        # sharing it means the second copy lands on top of the first and the
        # restore writes one household's plugin into the other's path, quietly.
        # Same problem `_stored_names` solves for state paths, so it is
        # answered the same way: keep the readable name, and only when it is
        # already taken add enough of the path to tell them apart.
        name = "plugins" if root == roots[0] else f"plugin-{Path(root).name}"
        if any(e["stored_as"] == name for e in out_extra):
            digest = hashlib.sha1(str(Path(root).resolve())
                                  .encode("utf-8")).hexdigest()[:8]
            name = f"{name}-{digest}"
        out_extra.append({
            "service": "plugins", "unit": name, "path": str(root),
            "role": "hub", "kind": "dir", "policy": "essential",
            "stored_as": name, "db_container": None,
            "db_user": None, "db_name": None,
            # `*.log` is skipped inside a plugin. `.git` is NOT, and the split
            # is the whole point -- the two were argued about together here
            # twice before, and they are not the same question.
            #
            # `.git` stays because a plugin "is not in git, which makes the
            # archive the only copy a household has of the services it added
            # itself" (docs/backups.md, still true). A plugin made with
            # `git init` has no remote at all, and even with one, unpushed
            # commits, local branches, stashes and the reflog exist only in
            # `.git` -- so "there is a remote" is not the same claim as "this
            # is somewhere else". The cost of being wrong there is a
            # household's own service, gone.
            #
            # `*.log` is the opposite trade and was only ever bundled with it
            # by accident. A log is a record of what already happened; nothing
            # is reconstructed from it and no service fails to come back
            # without it. Measured here: 10.8 MB of one plugin's 13.0 MB was
            # two unrotated files, re-copied, re-compressed, re-encrypted and
            # re-uploaded every night for as long as the archive is kept.
            #
            # Two things make this safe rather than merely cheap. `_skip_ignore`
            # matches a glob against *files* only, so a directory that happens
            # to end in `.log` keeps its contents. And `_rescue()` moves a
            # skipped path back out of the replaced tree on restore -- the rule
            # a skip makes is "not worth copying", never "safe to delete", so
            # restoring a plugin does not shred the logs already on that disk.
            #
            # Rotation in the plugin's own repository is still the better fix,
            # and this does not do it for them: the file that grew to 5.6 MB
            # will grow again. It just stops being this archive's problem.
            "skip_globs": ["*.log"],
        })

    out = list(out_extra)
    for service, unit, st in picked:
        policy = st.get("backup", "essential")
        if policy == "skip":
            continue
        if policy == "bulk" and not include_bulk:
            continue
        out.append({
            "service": service, "unit": unit["name"], "path": st["path"],
            # Which machine this lives on. Everything here used to assume "this
            # one", which is true only while every role points at 127.0.0.1 --
            # and the config explicitly supports moving one. home-paperless is
            # `role: storage`; the cameras, whisper and voice are `compute`.
            "role": spec_role.get(service, "hub"),
            "kind": st.get("kind", "dir"), "policy": policy,
            # Directories inside an otherwise-essential path that are bulk in
            # their own right. `backup:` is per entry, and some entries are one
            # path holding both kinds: the assistants' state is per-member
            # config, memory and credentials -- small, changing, and the whole
            # reason for a nightly archive -- plus a `media/` folder of every
            # image the assistant has ever sent or been sent, which is large,
            # static, and re-tarred, re-compressed and re-encrypted every night
            # for as long as it is kept.
            #
            # Measured on this house: 241 MB of one member's 281 MB was that
            # folder, about two thirds of a 351 MB archive, and seven retained
            # copies of files that had not changed since the day they arrived.
            #
            # Same semantics as `backup: bulk`, applied to a subtree: out of
            # the archive, and listed in its meta as something the archive is
            # not. `include_bulk` is how `not_mirrored` enumerates them; it is
            # not a user-facing option and there is deliberately no way to ask
            # for one of these in an archive.
            "bulk_dirs": [] if include_bulk else list(st.get("bulk_dirs") or []),
            # Never copied either, and for a different reason worth keeping
            # separate. `bulk_dirs` is "large, static, and yours, and something
            # else carries it". This is "a tool rebuilds it", so there is
            # nothing to carry: the same claim REGENERABLE makes globally,
            # made about one entry.
            #
            # Kept separate on purpose. `{state}/alfred-app` is 46 MB of which
            # 44 MB is `app/build`, gradle output; the 568 KB that matters is
            # the app source and its signing keystore. Calling that bulk would
            # have promised something restores a build directory, which is
            # both false and pointless.
            "skip_dirs": list(st.get("skip_dirs") or []),
            "skip_globs": list(st.get("skip_globs") or []),
            "stored_as": names[(service, st["path"])],
            "db_container": st.get("db_container"),
            "db_user": st.get("db_user"), "db_name": st.get("db_name"),
        })
    return out


def targets_for(cfg: dict, dry_run: bool = False) -> dict:
    """One Target per role, exactly as deploy.py builds them.

    `D.build_targets` rather than a second implementation. It was a second
    implementation, and it was missing the one role that is not under
    `hosts:` -- `vps`, whose address lives under `cloud.vps` because it is not
    a role a household assigns services to. A house with the proxy enabled got
    `KeyError: 'vps'` out of `./home-stack backup`, from the line that collects
    which roles are remote, before a single byte was copied.
    """
    out = D.build_targets(cfg, dry_run)
    # A stack with no `hosts:` at all still has a hub: it is this machine.
    out.setdefault("hub", D.Target("hub", {"address": "127.0.0.1"}, dry_run))
    return out


def _target_for(entry: dict, targets: dict):
    role = entry.get("role", "hub")
    if role not in targets:
        raise D.DeployError(
            f"{entry['service']} runs on role {role!r}, which `hosts:` does "
            f"not define. A backup cannot guess which machine its state is on.")
    return targets[role]


def _unreadable(exc: Exception) -> list[str]:
    """The source paths a copy could not read, out of whatever it raised.

    `shutil.copytree` collects per-file failures and raises one `shutil.Error`
    holding `(src, dst, why)` triples, so the useful part -- which file -- is
    inside the exception rather than on it. A plain OSError carries `filename`.
    """
    if isinstance(exc, shutil.Error):
        out = []
        for item in (exc.args[0] if exc.args else []):
            if isinstance(item, (list, tuple)) and item:
                out.append(str(item[0]))
        return out
    name = getattr(exc, "filename", None)
    return [str(name)] if name else []


def _copy_entry(entry: dict, into: Path, targets: dict) -> dict:
    """One entry, into its own place. Returns what was actually copied."""
    # Only meaningful for a local role; used below solely to tell "never
    # deployed" from "deployed but the database is down".
    src = Path(entry["path"])
    dest = into / entry["stored_as"]
    dest.parent.mkdir(parents=True, exist_ok=True)

    if entry["policy"] == "postgres":
        # pg_dump through the running container: the cluster on disk is not
        # copyable while it is up, and stopping the stack to back it up is a
        # trade nobody makes nightly.
        #
        # --clean --if-exists because the dump is replayed into a database that
        # still has its schema. Without it every CREATE in the restore fails on
        # an object that already exists, and the restore is decorative.
        out = dest.with_name(dest.name + ".sql")
        cmd = (f"docker exec {shlex.quote(entry['db_container'])} "
               f"pg_dump --clean --if-exists "
               f"-U {shlex.quote(entry['db_user'])} "
               f"{shlex.quote(entry['db_name'])}")
        # On the machine the database is actually on. `home-paperless` is
        # `role: storage`; point that role somewhere else and a local
        # `docker exec` finds no container and reports "absent" -- a green
        # backup with no database in it.
        target = _target_for(entry, targets)
        if not target.is_local:
            cmd = (f"ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
                   f"{shlex.quote(target.user + '@' + target.address)} "
                   f"{shlex.quote(cmd)}")
        # Streamed to the file as bytes, never through a Python str: a cluster
        # in a non-UTF8 encoding (SQL_ASCII holding Latin-1 is common on older
        # installs) raised UnicodeDecodeError inside subprocess itself, which
        # is not a DeployError and so escaped as a traceback -- and a text-mode
        # capture rewrites CRLF inside quoted literals and holds the whole dump
        # in memory twice over.
        with open(os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                  "wb") as fh:
            result = subprocess.run(cmd, shell=True, stdout=fh,
                                    stderr=subprocess.PIPE)
        if result.returncode != 0:
            out.unlink(missing_ok=True)
            detail = result.stderr.decode("utf-8", "replace").strip()
            # Not running is ordinary on a machine part way through its first
            # deploy, and not a reason to throw away a backup of everything
            # else. But "never deployed" and "deployed and currently down" are
            # different, and the data directory on disk is what tells them
            # apart: if the cluster is there, this archive is missing real data
            # and verify() must not bless it -- otherwise retention deletes the
            # last archive that did contain the database.
            if "No such container" in detail or "is not running" in detail:
                if src.exists():
                    return {"path": entry["path"], "status": "absent",
                            "detail": "the database was not running, and its "
                                      "data directory exists -- this archive "
                                      "has no copy of it"}
                return {"path": entry["path"], "status": "absent"}
            return {"path": entry["path"], "status": "failed",
                    "detail": detail[:200]}
        return {"path": entry["path"], "stored": entry["stored_as"] + ".sql",
                "status": "dumped", "bytes": out.stat().st_size}

    # Wherever it lives. `pull` short-circuits to a local copy when the role
    # resolves to this machine, which is the ordinary single-PC install.
    target = _target_for(entry, targets)
    try:
        got = target.pull(entry["path"], dest, is_dir=(entry["kind"] != "file"),
                          skip=(REGENERABLE + entry.get("bulk_dirs", [])
                                + entry.get("skip_dirs", [])
                                + entry.get("skip_globs", [])))
    except (shutil.Error, PermissionError, OSError) as exc:
        # A file this account cannot read is a *failure*, not an absence. A
        # container that runs as root leaves files nobody else can open --
        # paperless writes its search index 0600 root:root -- and copying
        # everything except those would produce an archive that verifies,
        # restores, and is missing part of a service.
        #
        # Reported rather than raised so `create()` can name every path that
        # went wrong in one message instead of stopping at the first, and so
        # the traceback that used to come out of shutil.copytree does not.
        unreadable = _unreadable(exc)
        detail = (f"cannot read {', '.join(unreadable[:3])}"
                  if unreadable else str(exc))
        return {"path": entry["path"], "status": "failed",
                "detail": f"{detail}. Files written by a container running as "
                          f"root are not readable by this account -- run the "
                          f"backup with sudo -E, or chown them."}
    if not got:
        # Not an error: a service can be enabled and not yet deployed.
        return {"path": entry["path"], "status": "absent"}

    # Measured on what actually arrived, not on the source: for a remote role
    # the source is not on this filesystem to stat, and the size that matters
    # to `verify` is the size of the copy in the archive.
    total = (dest.stat().st_size if dest.is_file()
             else sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()))
    return {"path": entry["path"], "stored": entry["stored_as"],
            "status": "copied", "bytes": total}


def _encrypt(tar: Path, key: str) -> Path:
    enc = tar.with_suffix(tar.suffix + ".enc")
    # -pbkdf2 because the default key derivation is one MD5 pass, and this key
    # is typed by a person. Passed on stdin: a key on argv is world-readable
    # in /proc/<pid>/cmdline for the life of the process.
    result = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
         "-salt", "-in", str(tar), "-out", str(enc), "-pass", "stdin"],
        input=key, text=True, capture_output=True)
    if result.returncode != 0:
        raise D.DeployError(f"encryption failed: {result.stderr.strip()}")
    tar.unlink()
    enc.chmod(0o600)
    return enc


def _decrypt(enc: Path, key: str, into: Path) -> Path:
    tar = into / enc.name[:-len(".enc")]
    result = subprocess.run(
        ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
         "-in", str(enc), "-out", str(tar), "-pass", "stdin"],
        input=key, text=True, capture_output=True)
    # openssl's exit status is not the whole answer. It rejects a wrong key by
    # failing the PKCS#7 padding check, and a wrong key produces valid-looking
    # padding by chance roughly once in 256 -- openssl then exits 0 and writes
    # a file of noise. `_open` met that as "not a gzip file. It is truncated or
    # corrupt", which sends somebody looking for a damaged copy when what they
    # have is the wrong key, on the one path where that distinction is the
    # whole question. Every archive this tool writes is gzip, so the magic
    # number answers it for certain.
    ok = result.returncode == 0
    if ok:
        with open(tar, "rb") as fh:
            ok = fh.read(2) == b"\x1f\x8b"
    if not ok:
        tar.unlink(missing_ok=True)
        raise D.DeployError(
            "could not decrypt this backup. The key in BACKUP_ENCRYPTION_KEY "
            "is not the one it was written with, and nothing can recover it "
            "without the original.")
    return tar


def encryption_key(cfg: dict, required: bool = False) -> str | None:
    """The key, or None when it is not needed. Raises when it is and is unset.

    One reader, one message. The message matters most on the restore path,
    which is where somebody finds out whether the copy they kept off-site was
    the right one.
    """
    if not required and not ((cfg.get("backups") or {}).get("encrypt")):
        return None
    key = (D.load_secrets(D.SECRETS).get(KEY_SECRET) or "").strip()
    if not key:
        raise D.DeployError(
            f"backups.encrypt is on but {KEY_SECRET} is empty. Generate it on "
            f"the admin page under Credentials -- and keep a copy of it "
            f"somewhere this house burning down does not affect, because an "
            f"encrypted backup without its key cannot be recovered.")
    return key


def uncaptured(cfg: dict) -> list[dict]:
    """State this run knowingly cannot reach, with the reason.

    Right now that is one thing, and it is the worst possible shape: a member
    whose assistant runs under `services.nanobot.runtime: kubernetes` keeps
    workspace and memory in a PersistentVolumeClaim, not under
    `{paths.state}/nanobot`. The host directory is still declared, still
    copied, still listed in docs/backups.md -- and on that install it is empty.

    So the archive verifies, restores, and contains none of the person's
    assistant memory, and nothing anywhere says so. The entry reads `absent`,
    which is the same word a service that was never deployed gets.

    Naming it is not fixing it. Capturing a PVC means going through kubectl,
    and that is a real piece of work with its own failure modes; what it must
    not do meanwhile is look finished.
    """
    gaps = []
    nanobot = ((cfg.get("services") or {}).get("nanobot") or {})
    if nanobot.get("runtime") == "kubernetes":
        members = nanobot.get("members") or []
        gaps.append({
            "what": "{paths.state}/nanobot",
            "why": (f"services.nanobot.runtime is kubernetes, so "
                    f"{len(members)} member(s) keep workspace and memory in a "
                    f"PersistentVolumeClaim. This archive does not contain "
                    f"them, and the host directory it did copy is empty."),
            "members": list(members),
        })
    return gaps


def create(out: "D.Out" = None) -> Path:
    out = out or D.out
    cfg = _cfg()
    key = encryption_key(cfg)          # fails before any work, not after
    root = backups_root(cfg)
    # UTC, not local time. The stamp is the archive's whole identity and the
    # only sort key retention has: across a DST fall-back the same local second
    # happens twice, which truncated the earlier archive and inverted the order
    # `prune()` deletes in.
    stamp = time.strftime(STAMP, time.gmtime())
    tar = root / f"{stamp}.tar.gz"
    if tar.exists() or (root / f"{stamp}.tar.gz.enc").exists():
        raise D.DeployError(
            f"a backup called {stamp} already exists. Wait a second and run "
            f"again rather than overwriting it.")
    staging = root / f".{stamp}.partial"
    # Written under a name nothing else looks at, then moved into place. A run
    # killed mid-tar used to leave a truncated archive under its final name,
    # which `--list` showed as a backup, retention counted, and `restore`
    # opened with a raw tarfile.ReadError.
    partial = root / f".{stamp}.tar.gz.partial"
    # 0700 from the moment it exists: this tree holds the credentials file and
    # every password hash in plain text for the length of the run.
    staging.mkdir(parents=True, exist_ok=True)
    staging.chmod(0o700)

    try:
        out.step(f"backup {stamp}")
        picked = entries(cfg)
        # One per role, from the same `hosts:` the deployer reads. On the
        # default single-PC install every one is local and `pull` is a copy.
        targets = targets_for(cfg)
        remote = sorted({e["role"] for e in picked
                         if not targets[e["role"]].is_local})
        if remote:
            out.sub(f"fetching from {', '.join(remote)} over ssh")
        stored, failed = [], []
        for entry in picked:
            # Start from the manifest entry so `db_container` and friends are
            # in the record a restore reads. They used to be dropped here, and
            # restore built `docker exec -i '' psql -U '' -d ''`.
            result = {**entry, **_copy_entry(entry, staging / CONTENTS, targets)}
            stored.append(result)
            if result["status"] == "failed":
                failed.append(result)

        if failed:
            raise D.DeployError(
                "backup abandoned, nothing kept: " +
                "; ".join(f"{f['path']}: {f['detail']}" for f in failed))

        kept = sum(s.get("bytes", 0) for s in stored)
        # Recorded in the archive, not only printed. Whoever restores this is
        # not the person who watched it being made, and "the log said so at
        # the time" is not a property the file has.
        gaps = uncaptured(cfg)
        meta = {
            "id": stamp,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime()),
            "encrypted": key is not None,
            "bytes": kept,
            "entries": stored,
            "services": sorted({s["service"] for s in stored}),
            "uncaptured": gaps,
            # What this archive deliberately does not hold. `uncaptured` above
            # is state a run *could not* reach; this is state it was told not
            # to take -- and the difference does not matter to whoever restores
            # it, who needs both named in the file rather than in the log of a
            # run they did not watch. The meta used to carry `with_media`,
            # which at least said whether the bulk paths were in; dropping the
            # flag without this left the archive silent about them.
            "not_archived": [
                {"path": e["path"], "service": e["service"],
                 "why": "bulk -- point the uploader at it directly"}
                for e in entries(cfg, include_bulk=True) if e["policy"] == "bulk"],
        }
        (staging / MANIFEST_NAME).write_text(json.dumps(meta, indent=2),
                                             encoding="utf-8")

        out.sub(f"{len(stored)} path(s), {kept / 1e6:.1f} MB")
        absent = [s for s in stored if s["status"] == "absent"]
        if absent:
            out.sub(f"{len(absent)} not on disk yet (enabled but never deployed)")
        for gap in gaps:
            out.warn(f"NOT in this backup: {gap['what']} -- {gap['why']}")
        for item in meta["not_archived"]:
            out.sub(f"not archived (bulk): {item['path']}")

        # It holds the credentials file and every password hash either way, so
        # it is 0600 from creation rather than after the last byte is written.
        with open(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                          0o600), "wb") as fh:
            with tarfile.open(fileobj=fh, mode="w:gz") as archive:
                archive.add(staging, arcname=stamp)
        os.replace(partial, tar)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        partial.unlink(missing_ok=True)

    final = _encrypt(tar, key) if key else tar
    _listing(final).write_text(",".join(meta.get("services", [])),
                               encoding="utf-8")
    out.ok(f"{final.name} ({final.stat().st_size / 1e6:.1f} MB"
           f"{', encrypted' if key else ''})")
    if not key:
        out.warn("not encrypted -- this file contains every credential in the "
                 "house and every password hash. backups.encrypt turns it on.")
    return final


EXPORT_STACK = "stack"


def export_root(cfg: dict) -> Path | None:
    """Where the mirror lives, or None when no household asked for one.

    The mirror is refreshed with `rsync --delete`, so this path is the one
    place in this file where getting a directory wrong deletes something. The
    checks below are in order of how badly each one ends.
    """
    raw = ((cfg.get("backups") or {}).get("export") or {}).get("path")
    if not raw:
        return None
    root = Path(raw)
    if not root.is_absolute():
        raise D.DeployError(
            f"backups.export.path is {root}, which is relative. Same reason "
            f"paths.backups must be absolute: the same command then means one "
            f"directory from the checkout and another from cron.")
    root = Path(os.path.normpath(root))

    # The fatal direction. A live path *inside* the mirror root means the next
    # refresh runs --delete across it: `export: /mnt/data/smart-bot` would
    # hold `cameras/`, `state/` and `backups/`, and the first run would empty
    # every one of them of anything the mirror did not put there.
    live = []
    for kind in D.PATH_KINDS:
        val = (cfg.get("paths") or {}).get(kind)
        if val:
            live.append(("paths." + kind, Path(val)))
    try:
        live += [(f"plugins: {d}", Path(d)) for d in D.plugin_dirs(cfg)]
    except Exception:  # noqa: BLE001 - a broken plugins: list is the deploy's
        pass           # problem to report, not a reason to refuse an export
    for name, path in live:
        path = Path(os.path.normpath(path))
        if root == path or path.is_relative_to(root):
            raise D.DeployError(
                f"backups.export.path is {root}, which contains {name} "
                f"({path}). The mirror is refreshed with --delete, so this "
                f"would empty it on the first run. Point it at a directory of "
                f"its own.")

    # The other direction, and only for the one tree that gets --delete'd from
    # outside. Being under `paths.media` is ordinary -- that is where a
    # household has room -- but being under the deploy directory means the next
    # `home-stack deploy` takes the mirror with it, which is the failure
    # `guard_state_paths()` exists for, one level over.
    deploy_dir = Path(os.path.normpath(
        os.path.expanduser("~/.local/share/home-stack")))
    for bad in (deploy_dir, Path(os.path.normpath(ROOT))):
        if root == bad or root.is_relative_to(bad):
            raise D.DeployError(
                f"backups.export.path is {root}, inside {bad}. A deploy "
                f"replaces that tree with rsync --delete and would take the "
                f"mirror with it.")
    return root


def _mirror_entry(entry: dict, into: Path, targets: dict) -> dict:
    """One entry into its place in the mirror, updated rather than rebuilt.

    The difference from `_copy_entry` is the whole point of the mirror: the
    destination already exists and holds last run's copy, so this is an rsync
    with `--delete` rather than a copy into a fresh directory. What did not
    change is not rewritten, which is what lets an uploader pointed at this
    tree ship only the difference.
    """
    src = Path(entry["path"])
    dest = into / entry["stored_as"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = _target_for(entry, targets)

    if entry["policy"] == "postgres":
        # Written to a neighbour and renamed, never in place: a dump
        # interrupted half way through is a file the uploader would happily
        # ship, and `pg_dump` writing straight to the destination is exactly
        # how you get a truncated one with a current mtime.
        out_file = dest.with_name(dest.name + ".sql")
        tmp = out_file.with_name("." + out_file.name + ".partial")
        cmd = (f"docker exec {shlex.quote(entry['db_container'])} "
               f"pg_dump --clean --if-exists "
               f"-U {shlex.quote(entry['db_user'])} "
               f"{shlex.quote(entry['db_name'])}")
        if not target.is_local:
            cmd = (f"ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
                   f"{shlex.quote(target.user + '@' + target.address)} "
                   f"{shlex.quote(cmd)}")
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                  "wb") as fh:
            result = subprocess.run(cmd, shell=True, stdout=fh,
                                    stderr=subprocess.PIPE)
        if result.returncode != 0:
            tmp.unlink(missing_ok=True)
            detail = result.stderr.decode("utf-8", "replace").strip()
            # The last good dump stays where it is. A mirror has no history, so
            # replacing it with nothing is the one irreversible thing this
            # function could do -- and "the database was down for one run" must
            # not look the same as "this household has no database".
            if "No such container" in detail or "is not running" in detail:
                if src.exists():
                    return {"path": entry["path"], "status": "absent",
                            "detail": "the database was not running, and its "
                                      "data directory exists -- the mirror "
                                      "still holds the previous dump"}
                return {"path": entry["path"], "status": "absent"}
            return {"path": entry["path"], "status": "failed",
                    "detail": detail[:200]}
        os.replace(tmp, out_file)
        return {"path": entry["path"], "stored": entry["stored_as"] + ".sql",
                "status": "dumped", "bytes": out_file.stat().st_size}

    # The same probe `pull` makes before its rsync, and for the same reason: a
    # service can be enabled and not yet deployed, and a role can be a machine
    # that is currently off. Both are ordinary answers, not failures.
    #
    # Without this the optional VPS takes the whole mirror down every night --
    # `cloud-proxy` is declared, `hosts.vps` is set, and an ssh that cannot
    # connect makes rsync exit with a broken pipe. `create()` has never had
    # that problem because `pull` probes first; this is that probe.
    if target.is_local:
        if not src.exists():
            return {"path": entry["path"], "status": "absent"}
    else:
        probe = target.run(f"test -e {shlex.quote(entry['path'])}",
                           check=False, capture=True)
        if probe.returncode != 0:
            return {"path": entry["path"], "status": "absent"}

    skip = (REGENERABLE + entry.get("bulk_dirs", [])
            + entry.get("skip_dirs", []) + entry.get("skip_globs", []))
    is_dir = entry["kind"] != "file"
    args = ["rsync", "-a", "--delete"]
    for name in skip:
        args += ["--exclude", name]
    if target.is_local:
        source = str(src) + ("/" if is_dir else "")
    else:
        args += ["-e", "ssh " + " ".join(
            shlex.quote(a) for a in D.deploy_identity())
            + " -o BatchMode=yes -o StrictHostKeyChecking=accept-new"]
        source = f"{target.user}@{target.address}:{entry['path']}"
        if is_dir:
            source += "/"
    if is_dir:
        dest.mkdir(parents=True, exist_ok=True)
    args += [source, str(dest)]
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        return {"path": entry["path"], "status": "failed",
                "detail": (detail[-1] if detail else "rsync failed")[:200]}

    total = (dest.stat().st_size if dest.is_file()
             else sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()))
    return {"path": entry["path"], "stored": entry["stored_as"],
            "status": "copied", "bytes": total}


def export_tree(out: "D.Out" = None) -> Path:
    """Refresh the mirror: the same inventory, in place, with no tarball.

    An archive is a new 62 MB file every night whether or not anything changed,
    and an uploader pointed at a directory of them ships all 62 MB of it. This
    writes the same content to the same paths every run, so the difference an
    uploader has to carry is the difference that actually happened -- a few MB
    on an ordinary day.

    **This tree is plaintext.** It holds `smart-home-bot.env`, `users.json` and
    every bcrypt hash in the house, exactly as the archive does, and unlike the
    archive nothing here encrypts it. That is deliberate: per-file encryption
    is what keeps an upload incremental (one changed file, one changed blob),
    and a tarball is the shape that cannot do it. What it means is that this
    directory is a local staging area and **not the thing to point an uploader
    at** -- encrypt it per file first, and upload the ciphertext.

    `bulk` entries are not mirrored. `{media}/cameras` is 36 GB and
    `{media}/paperless` is already a directory of finished files: copying them
    to a second place on the same disk buys nothing an uploader could not get
    by reading the originals. Point it at those two paths directly; this tree
    is the part that has no directory of its own.
    """
    out = out or D.out
    cfg = _cfg()
    root = export_root(cfg)
    if root is None:
        raise D.DeployError(
            "no backups.export.path is set, so there is no mirror to refresh. "
            "Set it to an absolute directory of its own -- see docs/backups.md.")

    stack = root / EXPORT_STACK
    # 0700 from the moment it exists, and re-asserted every run: this holds the
    # credentials file and every password hash in plain text, permanently
    # rather than for the length of a run the way `create()`'s staging does.
    #
    # The parent is usually a mount only root can write -- that is where a
    # household has room -- so "create it once, own it as the deploying user"
    # is an install step, and `install.sh` does it alongside `paths:`. Getting
    # here means it did not, and the answer is one command rather than a
    # traceback about os.mkdir.
    try:
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        stack.mkdir(parents=True, exist_ok=True)
        stack.chmod(0o700)
    except OSError as exc:
        raise D.DeployError(
            f"cannot prepare the mirror at {root}: {exc.strerror}. Create it "
            f"once, owned by the account that runs the backup:\n"
            f"  sudo install -d -o $(id -un) -g $(id -gn) -m 700 {root}\n"
            f"`./home-stack install` does this alongside paths: on a fresh "
            f"machine; an export path added to an existing install has to be "
            f"made the same way.") from exc

    out.step(f"mirror {root}")
    picked = entries(cfg)
    targets = targets_for(cfg)
    remote = sorted({e["role"] for e in picked
                     if not targets[e["role"]].is_local})
    if remote:
        out.sub(f"fetching from {', '.join(remote)} over ssh")

    stored, failed = [], []
    for entry in picked:
        result = {**entry, **_mirror_entry(entry, stack, targets)}
        stored.append(result)
        if result["status"] == "failed":
            failed.append(result)

    if failed:
        # Unlike `create()`, the previous mirror is still on disk and still
        # good. Refusing to write the metadata is what stops a half-refreshed
        # tree from being described as a complete one.
        raise D.DeployError(
            "mirror not updated cleanly, metadata left at the previous run: " +
            "; ".join(f"{f['path']}: {f['detail']}" for f in failed))

    kept = sum(s.get("bytes", 0) for s in stored)
    gaps = uncaptured(cfg)
    meta = {
        "id": time.strftime(STAMP, time.gmtime()),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime()),
        "kind": "mirror",
        "encrypted": False,
        "bytes": kept,
        "entries": stored,
        "services": sorted({s["service"] for s in stored}),
        "uncaptured": gaps,
        # Named here rather than left implicit: whoever finds this tree needs
        # to know the two large things it is not, and that they have their own
        # directories already.
        "not_mirrored": [
            {"path": e["path"], "why": "bulk -- point the uploader at it directly"}
            for e in entries(cfg, include_bulk=True) if e["policy"] == "bulk"],
    }
    tmp = stack / f".{MANIFEST_NAME}.partial"
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    os.replace(tmp, stack / MANIFEST_NAME)

    out.sub(f"{len(stored)} path(s), {kept / 1e6:.1f} MB in {stack}")
    absent = [s for s in stored if s["status"] == "absent"]
    if absent:
        out.sub(f"{len(absent)} not on disk yet (enabled but never deployed)")
    for gap in gaps:
        out.warn(f"NOT in this mirror: {gap['what']} -- {gap['why']}")
    for item in meta["not_mirrored"]:
        out.sub(f"not mirrored (bulk): {item['path']}")
    out.warn(f"{stack} is PLAINTEXT -- it holds the credentials file and "
             f"every password hash. Encrypt per file before uploading it.")
    out.ok(f"mirror refreshed ({kept / 1e6:.1f} MB)")
    return root


def _marker(archive: Path) -> Path:
    """The neighbour file recording that an archive verified."""
    return archive.with_suffix(archive.suffix + ".verified")


def _listing(archive: Path) -> Path:
    """The neighbour file naming the services inside an archive.

    Outside the archive on purpose. It is what lets somebody choose "restore
    just the documents" from a list, and reading it out of the archive would
    mean decrypting a file to find out what is in it -- on every page load, for
    every archive.

    Nothing depends on it: a missing sidecar means the archive offers no
    per-service choice, not that it is unusable. `restore --only` still reads
    the real manifest from inside, which is the copy that decides.
    """
    return archive.with_suffix(archive.suffix + ".services")


def _archives(cfg: dict) -> list[Path]:
    root = backups_root(cfg)
    if not root.is_dir():
        return []
    return sorted([p for p in root.iterdir() if ARCHIVE_RE.match(p.name)])


def _pick(archives: list[Path], which: str | None) -> Path:
    """The archive `which` names, or the newest.

    An exact id wins outright. A prefix that matches more than one is refused
    rather than resolved: `restore 2026-08-21` used to silently take the
    *oldest* run of that day and overwrite the day's newer state with it.
    """
    if which is None:
        return archives[-1]
    exact = [a for a in archives if a.name.split(".tar.gz")[0] == which]
    if exact:
        return exact[0]
    match = [a for a in archives if a.name.startswith(which)]
    if not match:
        raise D.DeployError(
            f"no backup matching {which!r}. `--list` shows what there is.")
    if len(match) > 1:
        raise D.DeployError(
            f"{which!r} matches {len(match)} backups (" +
            ", ".join(a.name for a in match[:4]) +
            "...). Name one exactly.")
    return match[0]


def _open(archive: Path, cfg: dict, into: Path) -> Path:
    """Unpack into *into*, decrypting first when it is encrypted."""
    tar = archive
    if archive.name.endswith(".enc"):
        tar = _decrypt(archive, encryption_key(cfg, required=True), into)
    try:
        with tarfile.open(tar, "r:gz") as t:
            # Pinned rather than left to the default, which changes to the
            # strict `data` filter in 3.14 -- and these archives legitimately
            # contain absolute symlinks, because state trees are captured with
            # symlinks=True.
            t.extractall(into, filter="tar")
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise D.DeployError(
            f"{archive.name} will not open ({exc}). It is truncated or "
            f"corrupt; `--list` shows what else there is.") from exc
    unpacked = into / archive.name.split(".tar.gz")[0]
    if not (unpacked / MANIFEST_NAME).is_file():
        raise D.DeployError(f"{archive.name} has no {MANIFEST_NAME} in it")
    return unpacked


def verify(which: str | None = None, out: "D.Out" = None,
           archive: Path | None = None,
           only: list[str] | None = None) -> int:
    """Unpack a backup somewhere harmless and check what came out.

    This is the whole point of the exercise. A backup that has never been
    restored is a belief, not a backup -- and the specific ways these fail are
    quiet ones: an archive that unpacks to nothing, a pg_dump that captured an
    error message instead of a schema, a user store that is `[]` because the
    file was copied while it did not exist yet.

    `only` narrows it to named services. The question that asks for is "the
    documents came back wrong -- is the copy I would restore any good?", and
    it is the same question `restore --only` answers destructively. A narrowed
    run deliberately does two things differently: it does not apply the
    whole-archive checks (the user store lives in home-core, and demanding it
    while checking home-paperless would fail every narrowed run), and it does
    not write the `.verified` marker, because "home-paperless restores" is not
    the claim that marker makes.
    """
    out = out or D.out
    cfg = _cfg()
    if archive is None:
        archives = _archives(cfg)
        if not archives:
            out.fail("no backups to verify")
            return 1
        archive = _pick(archives, which)

    out.step(f"verifying {archive.name}"
             + (f" ({', '.join(only)} only)" if only else ""))
    problems = []
    root = backups_root(cfg)
    # Scratch space beside the archives, not in /tmp. /tmp is tmpfs on a
    # systemd host -- half of RAM -- and a large archive unpacks to
    # hundreds of gigabytes.
    with tempfile.TemporaryDirectory(prefix=".verify-", dir=root) as tmp:
        unpacked = _open(archive, cfg, Path(tmp))
        meta = json.loads((unpacked / MANIFEST_NAME).read_text(encoding="utf-8"))

        items = meta["entries"]
        if only:
            held = sorted({e["service"] for e in items})
            # A name this archive never held is an error, not an empty run. A
            # narrowed verify that silently checks nothing reports success,
            # which is the worst possible answer to "is my copy any good?" --
            # the same failure a table keyed on the wrong id makes.
            missing = [s for s in only if s not in held]
            if missing:
                out.fail(f"{archive.name} holds no {', '.join(missing)}. "
                         f"It holds: {', '.join(held)}")
                return 1
            items = [e for e in items if e["service"] in only]

        for item in items:
            if item["status"] == "absent":
                # A path that has never been deployed is genuinely absent. A
                # database that was merely stopped is not -- that is an
                # incomplete backup, and blessing it lets retention delete the
                # last archive that did contain the database.
                if item.get("detail"):
                    problems.append(f"{item['path']}: {item['detail']}")
                continue
            stored = unpacked / CONTENTS / item["stored"]
            if not stored.exists():
                problems.append(f"{item['path']}: named in the manifest, "
                                f"not in the archive")
                continue
            if item["status"] == "dumped":
                # pg_dump's header is in the first few dozen bytes; a dump that
                # is really an error message is the failure this catches.
                with stored.open(encoding="utf-8", errors="ignore") as fh:
                    head = fh.read(4096)
                if "PostgreSQL database dump" not in head:
                    problems.append(f"{item['path']}: the dump does not look "
                                    f"like one ({stored.stat().st_size} bytes)")
                continue
            # Empty is only wrong if something was supposed to be there.
            # `{state}/mosquitto` is empty by design -- the broker runs
            # `persistence false` -- and `{config}/ssh` is empty on a
            # single-PC install; failing on those made every run exit 1 and
            # stopped retention from ever running.
            if (item.get("bytes") and stored.is_dir()
                    and not any(stored.rglob("*"))):
                problems.append(f"{item['path']}: restored empty")

        # The two files that decide whether a *whole-archive* restore is worth
        # anything, and so exactly the two a narrowed run must not demand:
        # users.json is home-core's and the credentials file is the admin's,
        # and neither says anything about whether home-paperless came back.
        users = _find(unpacked, meta, "users.json") if not only else None
        if only:
            pass
        elif users is None:
            problems.append("no user store in this archive: nobody could sign "
                            "in to a house restored from it")
        else:
            try:
                people = json.loads(users.read_text(encoding="utf-8"))
                out.sub(f"user store: {len(people)} account(s)")
                if not people:
                    out.warn("the user store restored empty -- correct only if "
                             "this household genuinely has no accounts")
            except json.JSONDecodeError:
                problems.append("users.json is not readable JSON")
        env = _find(unpacked, meta, "smart-home-bot.env") if not only else None
        if only:
            out.sub(f"checked {len(items)} path(s) of "
                    f"{', '.join(sorted({e['service'] for e in items}))}")
        elif env is not None:
            keys = [l for l in env.read_text(encoding="utf-8").splitlines()
                    if l.strip() and not l.startswith("#") and "=" in l]
            out.sub(f"credentials: {len(keys)} key(s)")
        else:
            problems.append("no credentials file: a restore from this cannot "
                            "bring any service back up")

    for problem in problems:
        out.fail(problem)
    if problems:
        return 1
    # Said again here, from the archive rather than from today's config. The
    # person verifying is asking "am I covered?", and this is the honest half
    # of the answer -- it does not make the archive invalid, so it does not
    # fail, but it must not be silent either.
    for gap in (meta.get("uncaptured") or []):
        out.warn(f"NOT in this backup: {gap['what']} -- {gap['why']}")
    # The bulk paths, from the same file. Read from the archive rather than
    # recomputed, so an old archive that predates the field says nothing
    # instead of claiming today's answer applied to it.
    for item in (meta.get("not_archived") or []):
        out.sub(f"not archived (bulk): {item['path']}")
    if only:
        # No marker, and the wording says why. `--list` shows "verified" as a
        # property of the whole archive, and a run that unpacked six of its
        # twenty-seven paths has not established that -- writing the marker
        # here would let one narrow check make a broken archive look green.
        out.ok(f"{archive.name}: {', '.join(only)} restores, and what came "
               f"out is what went in (narrowed run -- the archive as a whole "
               f"is still marked "
               f"{_marker(archive).exists() and 'verified' or 'unverified'})")
        return 0
    out.ok(f"{archive.name} restores, and what came out is what went in")
    # Record it on the archive's neighbour file, so --list can say so.
    _marker(archive).write_text(time.strftime("%Y-%m-%dT%H:%M:%S+0000",
                                              time.gmtime()), encoding="utf-8")
    return 0


def _find(unpacked: Path, meta: dict, basename: str) -> Path | None:
    """The archived copy of a file, found through the manifest.

    Not by hardcoded `contents/home-core/users.json`: the directory inside the
    archive is the *service* name, and services in this stack are renameable
    (`renamed_from:` in the manifest, `SERVICE_RENAMES` in deploy.py). A
    rename used to make the user-store check evaporate silently and the
    credentials check fail on every run.
    """
    for item in meta["entries"]:
        if item["status"] == "absent" or not item.get("stored"):
            continue
        if Path(item["path"]).name != basename:
            continue
        candidate = unpacked / CONTENTS / item["stored"]
        if candidate.is_file():
            return candidate
    return None


def _stamp_of(archive: Path) -> time.struct_time:
    """The moment an archive was started, from its name.

    `ARCHIVE_RE` already guaranteed the shape, so this cannot be handed a
    household's own tarball -- which matters because the caller deletes what
    this sorts.
    """
    return time.strptime(archive.name.split(".tar.gz")[0], STAMP)


def retained(archives: list[Path], keep: int, weekly: int,
             monthly: int) -> set[Path]:
    """Which archives survive: recent ones, then one a week, then one a month.

    A flat count is the wrong shape once a mirror exists. The mirror is what
    covers "yesterday looked fine and today does not"; what an archive is for
    is the failure nobody noticed for a while -- a table quietly keyed on the
    wrong id, a member deleted in March. Seven dailies answer the first
    question twice over and the second one not at all, and they cost the same
    62 MB every night regardless.

    So: `keep` recent runs whatever their spacing, plus the newest run in each
    of the last `weekly` weeks, plus the newest in each of the last `monthly`
    months. The tiers overlap on purpose -- an archive that is both this
    week's and this month's is one file, and the union is what survives.

    Weeks are ISO (`%G-W%V`), so the week a year ends in is one bucket rather
    than two. Both are computed from the name's UTC stamp, which is why
    `create()` refuses to use local time: across a DST fall-back the same
    local hour happens twice and the sort that decides deletions inverts.
    """
    if not archives:
        return set()
    newest_first = sorted(archives, key=_stamp_of, reverse=True)
    survivors = set(newest_first[:max(keep, 0)])
    for span, limit in (("%G-W%V", weekly), ("%Y-%m", monthly)):
        if limit <= 0:
            continue
        pick: dict[str, Path] = {}
        for archive in newest_first:
            pick.setdefault(time.strftime(span, _stamp_of(archive)), archive)
        survivors |= set(list(pick.values())[:limit])
    return survivors


def prune(out: "D.Out" = None) -> None:
    """Drop what no tier claims, only ever after a new one exists and verified."""
    out = out or D.out
    cfg = _cfg()
    conf = cfg.get("backups") or {}
    keep = int(conf.get("keep") or 0)
    weekly = int(conf.get("keep_weekly") or 0)
    monthly = int(conf.get("keep_monthly") or 0)
    archives = _archives(cfg)
    if not archives:
        return
    # Every tier at zero means "no retention configured", which must not be
    # read as "delete everything". The old flat default was 7; an install that
    # never set the two new keys keeps behaving the way it always did.
    if keep <= 0 and weekly <= 0 and monthly <= 0:
        keep = 7
    survivors = retained(archives, keep, weekly, monthly)
    # Belt and braces around the one bug this function can have. Retention
    # deleting its way to an empty directory is not recoverable, and a bad
    # config is not a reason to have no backups at all.
    if not survivors:
        out.warn("retention would keep nothing; keeping the newest and "
                 "changing nothing else")
        return
    for old in archives:
        if old in survivors:
            continue
        old.unlink()
        # Both neighbours, or retention leaves a directory of sidecars for
        # archives that are gone -- and `catalogue()` reads them by name, so
        # a stray one is a row for a backup nobody can restore.
        for beside in (_marker(old), _listing(old)):
            beside.unlink(missing_ok=True)
        out.sub(f"removed {old.name}")


def catalogue(cfg: dict) -> list[dict]:
    """What is in `paths.backups`, newest last, without opening anything.

    Everything here comes from the filename and the two neighbour files, so a
    listing costs no decryption and no unpacking -- which matters because the
    admin page renders this on every load and an encrypted archive would
    otherwise want the key to say its own size.

    `services` is the exception and comes from the sidecar written beside the
    archive when it was made. Without it the page cannot offer "restore just
    this one", and reading it out of the archive would mean decrypting.
    """
    rows = []
    for archive in _archives(cfg):
        marker = _marker(archive)
        try:
            services = [x for x in _listing(archive).read_text(
                encoding="utf-8").split(",") if x]
        except OSError:
            services = []
        rows.append({
            "id": archive.name.split(".tar.gz")[0],
            "name": archive.name,
            "bytes": archive.stat().st_size,
            "encrypted": archive.name.endswith(".enc"),
            "verified": (marker.read_text(encoding="utf-8").strip()
                         if marker.exists() else ""),
            "services": services,
        })
    return rows


def cmd_list(out: "D.Out" = None) -> int:
    out = out or D.out
    cfg = _cfg()
    rows = catalogue(cfg)
    if not rows:
        print(f"no backups in {backups_root(cfg)}")
        return 0
    print(f"{'backup':34} {'size':>9}  {'encrypted':9} verified")
    print("-" * 72)
    for row in rows:
        print(f"{row['name']:34} {row['bytes'] / 1e6:8.1f}M  "
              f"{'yes' if row['encrypted'] else 'no':9} "
              f"{row['verified'] or 'never'}")
    return 0


def kept_out(item: dict) -> list[str]:
    """What an archive deliberately did not copy, for one entry.

    The three per-entry lists plus the global one, in the same order `pull` was
    given them. Read from the *archive's* record rather than from today's
    manifest, because those are two different questions: this one is "what is
    missing from the copy I am about to put back", and only the archive knows.
    An older archive carries none of these keys and answers REGENERABLE, which
    is what it was made with.
    """
    return (REGENERABLE + list(item.get("bulk_dirs") or [])
            + list(item.get("skip_dirs") or [])
            + list(item.get("skip_globs") or []))


def _rescue(replaced: Path, live: Path, keep: list[str]) -> list[str]:
    """Move back what the archive never carried, before the old copy is deleted.

    A restore *replaces* a path: the archived tree takes its place and the live
    one is removed. That is right for everything the archive holds and silently
    destructive for everything it was told to leave out -- and this package now
    tells it to leave out four kinds of thing. Without this, a restore from an
    ordinary nightly archive deleted every member's `media/` folder, because a
    nightly is exactly the archive that does not carry it; `--verify` cannot
    see it, because verify restores into a scratch tree where there is nothing
    live to lose.

    So: the rule a skip makes is "not worth copying", never "safe to delete".
    Anything matching one of those names is moved out of the replaced tree and
    into the same relative place under the restored one, and only if nothing
    the archive brought is already sitting there -- the archive always wins.
    Both trees are siblings, so this is a rename rather than a copy, and a
    subtree that cannot be moved is left where it is for the caller to report.
    """
    if not keep or not replaced.is_dir() or replaced.is_symlink():
        return []
    globs = [p for p in keep if any(c in p for c in "*?[")]
    exact = {p for p in keep if p not in globs}
    moved, stack = [], [replaced]
    while stack:
        here = stack.pop()
        try:
            children = sorted(here.iterdir())
        except OSError:
            continue
        for child in children:
            if child.name in exact or any(fnmatch.fnmatch(child.name, g)
                                          for g in globs):
                rel = child.relative_to(replaced)
                dest = live / rel
                if dest.exists() or dest.is_symlink():
                    continue
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(child, dest)
                    moved.append(str(rel))
                except OSError:
                    pass
                continue
            if child.is_dir() and not child.is_symlink():
                stack.append(child)
    return moved


def _put_back_remote(stored: Path, live: Path, target) -> None:
    """The same swap, on the machine that owns the path.

    Same shape as the local one and for the same reason: ship the replacement
    to a name beside the real one, then move it into place, so a transfer that
    dies half way leaves the live copy untouched. `mv` within a directory is
    atomic on any sane filesystem, which is what makes this safe to interrupt.
    """
    parent = str(Path(live).parent)
    incoming = f"{parent}/.{Path(live).name}.restoring"
    target.run(f"mkdir -p {shlex.quote(parent)} && "
               f"rm -rf {shlex.quote(incoming)}")
    if stored.is_dir():
        target.run(f"mkdir -p {shlex.quote(incoming)}")
        target.push(stored, incoming)
    else:
        target.push_file(stored, incoming)
    target.run(f"rm -rf {shlex.quote(str(live))} && "
               f"mv {shlex.quote(incoming)} {shlex.quote(str(live))}")


def _put_back(stored: Path, live: Path,
              keep: "list[str] | None" = None) -> "tuple[str | None, list[str]]":
    """Replace *live* with *stored*, without destroying it first.

    The oldest form was rmtree-then-copytree, so anything that failed in
    between -- ENOSPC, EACCES, a symlinked live path, which rmtree refuses
    outright -- left nothing at all, and the only remaining copy was in a
    scratch directory about to be deleted.

    Staging the replacement beside it and then swapping fixed most of that,
    but not the swap: it still did `rmtree(live)` before the move, and rmtree
    is per-file. A directory whose contents belong to a container's uid --
    `{config}/mosquitto` is 1883 on a default install -- gets half emptied and
    then raises, and what is left is neither the old copy nor the new one.

    So nothing is deleted until both copies exist. The live tree is *renamed*
    aside, which needs permission on the parent rather than on the contents and
    is one atomic operation; the replacement takes its place; only then is the
    old one removed, and failing to remove it is not a failed restore -- it is
    a directory to delete by hand, which the caller says out loud.
    """
    live.parent.mkdir(parents=True, exist_ok=True)
    incoming = live.parent / f".{live.name}.restoring"
    replaced = live.parent / f".{live.name}.replaced"
    for scratch in (incoming, replaced):
        if scratch.is_dir() and not scratch.is_symlink():
            shutil.rmtree(scratch, ignore_errors=True)
        else:
            scratch.unlink(missing_ok=True)

    if stored.is_dir():
        shutil.copytree(stored, incoming, symlinks=True)
    else:
        shutil.copy2(stored, incoming)

    had = live.is_symlink() or live.exists()
    if had:
        os.replace(live, replaced)
    try:
        os.replace(incoming, live)
    except OSError:
        # Put it back the way it was rather than leaving the path empty.
        if had:
            os.replace(replaced, live)
        raise
    rescued = []
    if had:
        # Before anything is deleted: what the archive was told not to copy is
        # not the archive's to remove. See `_rescue`.
        rescued = _rescue(replaced, live, keep or [])
        if replaced.is_dir() and not replaced.is_symlink():
            shutil.rmtree(replaced, ignore_errors=True)
        else:
            replaced.unlink(missing_ok=True)
    # Whatever could not be removed stays; it is a stale copy taking up space,
    # not a broken restore, and saying "restore failed" about it would send
    # somebody looking for the wrong problem.
    return (str(replaced) if replaced.exists() else None), rescued


def restore(which: str, confirm: bool = False, out: "D.Out" = None,
            only: list[str] | None = None) -> int:
    """Put a backup back, after saying exactly what it will overwrite.

    Deliberately not clever. It does not merge, it does not guess which copy is
    newer, and it will not run without --confirm: the failure mode of a restore
    is silently replacing live state with older state, and every guard in this
    package exists because some version of that happened.

    `only` narrows it to one service, or a few. That is the shape a restore
    usually has: the documents came back wrong, or one person's assistant
    memory is gone, and putting the whole archive back would drag every other
    service to the same hour -- including the user store and the credentials
    file, which is how a restore of one thing signs the whole house out.

    A name the archive does not carry is refused rather than skipped. "It
    restored nothing and said it was fine" is the failure this whole file is
    written against, and a typo in a service name is exactly how you would get
    there.
    """
    out = out or D.out
    cfg = _cfg()
    archives = _archives(cfg)
    if not archives:
        out.fail(f"no backups in {backups_root(cfg)}")
        return 1
    archive = _pick(archives, which)

    root = backups_root(cfg)
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=root) as tmp:
        unpacked = _open(archive, cfg, Path(tmp))
        meta = json.loads((unpacked / MANIFEST_NAME).read_text(encoding="utf-8"))

        targets = targets_for(cfg)
        held = sorted({item.get("service") for item in meta["entries"]
                       if item.get("service")})
        wanted = None
        if only:
            wanted = {name.strip() for name in only if name.strip()}
            unknown = sorted(wanted - set(held))
            if unknown:
                out.fail(f"{archive.name} holds nothing for "
                         f"{', '.join(unknown)}.")
                out.sub("it holds: " + ", ".join(held))
                return 1

        out.step(f"restoring {archive.name}"
                 + (f" - only {', '.join(sorted(wanted))}" if wanted else ""))
        plan, skipped = [], []
        for item in meta["entries"]:
            if item["status"] == "absent":
                continue
            if wanted is not None and item.get("service") not in wanted:
                skipped.append(item)
                continue
            stored = unpacked / CONTENTS / item["stored"]
            _validate_restore_path(item["path"], cfg)
            plan.append((item, stored))
            live = Path(item["path"])
            state = "replaces" if live.exists() else "creates"
            if item["status"] == "dumped":
                state = "replays into"
            out.sub(f"{state:12} {item['path']}")

        # Said out loud, every time. A narrowed restore that quietly left the
        # rest alone reads exactly like a full one, and the difference is which
        # hour the rest of the house is at.
        if skipped:
            out.sub(f"{'leaves':12} {len(skipped)} path(s) in "
                    + ", ".join(sorted({i['service'] for i in skipped
                                        if i.get('service')})))
        if not plan:
            out.fail("nothing to restore: every path this archive holds for "
                     "those services was absent when it was made.")
            return 1

        if not confirm:
            out.warn("nothing done. This overwrites live state with the copy "
                     "above; re-run with --confirm when that is what you want.")
            return 2
        out.warn("the services reading these paths are still running. Stop "
                 "them first if you can; a process holding a database open "
                 "can write over part of what lands here.")

        for item, stored in plan:
            live = Path(item["path"])
            if item["status"] == "dumped":
                missing = [k for k in ("db_container", "db_user", "db_name")
                           if not item.get(k)]
                if missing:
                    out.fail(f"{item['path']}: this archive does not record "
                             f"{', '.join(missing)}, so the dump cannot be "
                             f"replayed. Restore it by hand with psql.")
                    return 1
                # ON_ERROR_STOP because psql's default is to report every
                # failed statement on stderr and still exit 0 -- so the
                # returncode check below could not fail. --single-transaction
                # so a dump that does not apply leaves the database as it was
                # rather than half-replayed.
                db_target = _target_for(item, targets)
                cmd = (f"docker exec -i {shlex.quote(item['db_container'])} "
                       f"psql -v ON_ERROR_STOP=1 --single-transaction "
                       f"-U {shlex.quote(item['db_user'])} "
                       f"-d {shlex.quote(item['db_name'])}")
                # On the machine the database is on, for the same reason the
                # dump was taken there. Replaying into a local container that
                # does not exist would fail loudly; replaying into a *different*
                # household's local container would not.
                if not db_target.is_local:
                    cmd = (f"ssh -o BatchMode=yes "
                           f"-o StrictHostKeyChecking=accept-new "
                           f"{shlex.quote(db_target.user + '@' + db_target.address)} "
                           f"{shlex.quote(cmd)}")
                with stored.open("rb") as fh:
                    result = subprocess.run(cmd, shell=True, stdin=fh,
                                            capture_output=True)
                if result.returncode != 0:
                    detail = result.stderr.decode("utf-8", "replace").strip()
                    out.fail(f"{item['path']}: {detail[:200]}")
                    out.warn("stopped part way. Paths listed above this point "
                             "are restored; the rest are untouched.")
                    return 1
                out.ok(f"replayed {item['path']}")
                continue
            target = _target_for(item, targets)
            keep = kept_out(item)
            try:
                if target.is_local:
                    leftover, rescued = _put_back(stored, live, keep)
                else:
                    # No rescue on the remote path: the swap happens over ssh
                    # in one `rm -rf && mv`, so there is no moment here to move
                    # anything out of the old tree. Said out loud rather than
                    # left to be discovered, because the paths this loses are
                    # the large ones -- a member's `media/` folder, a plugin's
                    # `.git` -- and they are lost silently otherwise.
                    leftover, rescued = _put_back_remote(stored, live, target), []
                    if keep != REGENERABLE:
                        out.warn(f"{item['path']} is on {item.get('role')}, so "
                                 f"what this archive left out is deleted rather "
                                 f"than kept: "
                                 + ", ".join(k for k in keep
                                             if k not in REGENERABLE))
            except (shutil.Error, PermissionError, OSError) as exc:
                # The same class of failure as on the way in, and the same
                # answer: name the file, say why, and stop. A traceback out of
                # shutil in the middle of a restore tells somebody nothing
                # about which paths are now which version.
                names = _unreadable(exc)
                out.fail(f"{item['path']}: "
                         + (f"cannot replace {', '.join(names[:3])}"
                            if names else str(exc)))
                out.warn("this path is unchanged, and so is everything below "
                         "it in the list. Paths already reported as restored "
                         "are restored. Files owned by a container's own user "
                         "are why -- run the restore with sudo -E.")
                return 1
            out.ok(f"restored {item['path']}")
            if rescued:
                # Named, not counted: "kept 3 paths" is not something anybody
                # can check, and these are the paths the archive never had.
                out.sub(f"{'kept':12} {', '.join(rescued[:4])}"
                        + (f" (+{len(rescued) - 4} more)"
                           if len(rescued) > 4 else "")
                        + " -- not in this archive, left where they were")
            if leftover:
                out.warn(f"the copy that was there is still at {leftover}; "
                         f"it could not be removed. Delete it when you are "
                         f"satisfied with what replaced it.")

    touched = sorted({item.get("service") for item, _ in plan
                      if item.get("service")}) or meta.get("services", [])
    out.warn("restored on disk, not in the containers. Deploy the services "
             "this touched so they read it: ./home-stack deploy "
             + " ".join(touched))
    return 0


USAGE = """  ./home-stack backup                 make one
  ./home-stack backup --export        refresh the mirror, write no archive
  ./home-stack backup --auto          mirror now, archive only when one is due
  ./home-stack backup --list          what exists, how big, whether it verified
  ./home-stack backup --verify [id]   unpack it into a scratch tree and check
  ./home-stack backup --verify [id] --only home-paperless   just those services
  ./home-stack restore <id>           what it would overwrite, and nothing else
  ./home-stack restore <id> --confirm put it back
  ./home-stack restore <id> --only home-paperless   just that service
"""

FLAGS = {"--list", "--verify", "--confirm", "--only",
         "--export", "--auto", "-h", "--help"}


def main(argv: list[str]) -> int:
    args = list(argv)
    # Every path below can fail for a reason a person needs to read -- a
    # missing config, a missing key, an unreadable archive -- and a traceback
    # is not that. One handler, around all of them: `--list` and the
    # post-create verify used to sit outside it and print stack traces.
    try:
        if args and args[0] in ("-h", "--help"):
            print(USAGE)
            return 0
        # Hoisted out of the `restore` branch, because `--verify` takes it too
        # now: "does the archive I would restore hold a good copy of *this*
        # service" is the question somebody actually has, and answering it used
        # to mean unpacking all 27 entries to look at one.
        #
        # Comma-separated, because a restore of "the documents" is sometimes
        # two services -- home-paperless and its database live under one name
        # here, but a household's own service and the one it depends on need
        # not.
        only = None
        if "--only" in args:
            i = args.index("--only")
            if len(args) <= i + 1 or args[i + 1].startswith("-"):
                print(USAGE, file=sys.stderr)
                return 2
            only = [s for s in args[i + 1].split(",") if s.strip()]
            if not only:
                print(USAGE, file=sys.stderr)
                return 2
            # Its value is not a flag and must not be read as a positional.
            args = args[:i] + args[i + 2:]

        if args and args[0] == "restore":
            unknown = [a for a in args[2:] if a.startswith("-")
                       and a not in FLAGS]
            if len(args) < 2 or args[1].startswith("-") or unknown:
                print(USAGE, file=sys.stderr)
                return 2
            return restore(args[1], confirm="--confirm" in args, only=only)
        if "--list" in args:
            return cmd_list()
        if "--verify" in args:
            i = args.index("--verify")
            which = (args[i + 1] if len(args) > i + 1
                     and not args[i + 1].startswith("-") else None)
            return verify(which, only=only)
        # Anything left that is not a flag we know is a typo, and a typo used
        # to take a real backup and then prune.
        unknown = [a for a in args if a not in FLAGS]
        if unknown:
            D.out.fail(f"unknown argument: {unknown[0]}")
            print(USAGE, file=sys.stderr)
            return 2
        if "--export" in args:
            export_tree()
            return 0
        return _run_backup(auto="--auto" in args)
    except D.DeployError as exc:
        D.out.fail(str(exc))
        return 1


def _archive_due(cfg: dict) -> bool:
    """Whether `--auto` should write a full archive this run.

    The mirror runs every night and costs almost nothing, because only what
    changed moves. A full costs the same 62 MB every time, so it runs on its
    own clock: `backups.full_every_days` since the newest one, 7 by default.

    Zero or unset means every run, which is what this did before there was a
    mirror -- an install that upgrades and changes nothing keeps its old
    nightly cadence rather than quietly dropping to weekly.
    """
    every = int((cfg.get("backups") or {}).get("full_every_days") or 0)
    if every <= 0:
        return True
    archives = _archives(cfg)
    if not archives:
        return True
    newest = max(archives, key=_stamp_of)
    age = time.time() - time.mktime(_stamp_of(newest)) + time.timezone
    return age >= every * 86400


def _run_backup(auto: bool = False) -> int:
    cfg = _cfg()
    # The mirror first, and its failure is not fatal to the archive. They are
    # two independent copies: an rsync that could not reach one path is a
    # reason to shout, not a reason to also skip the night's archive.
    if export_root(cfg) is not None:
        try:
            export_tree()
        except D.DeployError as exc:
            D.out.fail(f"mirror not refreshed: {exc}")
            if auto:
                # In --auto the mirror is the nightly copy and the archive is
                # occasional, so a broken mirror must not exit 0 just because
                # no archive happened to be due tonight.
                return 1

    if auto and not _archive_due(cfg):
        every = int((cfg.get("backups") or {}).get("full_every_days") or 0)
        D.out.ok(f"mirror is current; next full archive is due "
                 f"{every} day(s) after the newest one")
        return 0

    archive = create()
    # Verify the archive that was just written -- by identity, not by picking
    # the newest name in the directory, which is not necessarily this one.
    # Retention runs only after that: a run that produced an unreadable archive
    # must not be the reason the last good one is deleted.
    if verify(archive=archive) != 0:
        D.out.fail("the backup just written does not verify; keeping "
                   "everything and changing nothing else")
        return 1
    prune()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
