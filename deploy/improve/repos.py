"""Where a fix may be made: the repositories the improver can work in.

Filled on its own, from what the house already declares:

* **the stack itself** -- this checkout, the one that deploys, and the remote
  it was cloned from (its GitHub repository). A fix lands here as a commit in
  the working checkout; publishing it stays a person's (`CLAUDE.md`).
* **every plugin** in `plugins:` that is a git repository -- where a
  household's own services and the skills they serve live. The lights skill,
  for one, is served by a plugin, so "the lights skill points at the wrong
  server" is a fix in that plugin's repository, not in this one.

and `assistant.improve.repos` in the live config for anything else, with
`assistant.improve.exclude` to leave one of the automatic ones out.

Each entry says what the repository *provides* -- services, the assistant
env it contributes (which is how a plugin's skill is found: `HOME_LIGHTS_API_URL`
is the lights skill's), and for the stack the skills it ships -- so an issue can
be routed to the right one by what it is about.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))


def _git(path: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                           timeout=15)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def web_url(remote: str) -> str:
    """Where a person opens the repository, from where git fetches it."""
    m = re.match(r"^(?:ssh://)?git@github\.com[:/]([^/]+)/(.+?)(?:\.git)?$", remote)
    if m:
        return f"https://github.com/{m.group(1)}/{m.group(2)}"
    m = re.match(r"^\S+@vs-ssh\.visualstudio\.com:v3/([^/]+)/([^/]+)/(.+)$", remote)
    if m:
        return f"https://dev.azure.com/{m.group(1)}/{m.group(2)}/_git/{m.group(3)}"
    m = re.match(r"^https://[^@/]*@?(.+?)(?:\.git)?$", remote)
    return f"https://{m.group(1)}" if m else ""


def _repo(name: str, kind: str, path: Path, source: str, provides: dict) -> dict | None:
    top = _git(path, "rev-parse", "--show-toplevel")
    if not top:
        return None  # not a git repository: nowhere a fix could be a commit
    remote = _git(path, "remote", "get-url", "origin")
    return {"name": name, "kind": kind, "path": top, "source": source,
            "remote": remote, "web": web_url(remote),
            "branch": _git(path, "rev-parse", "--abbrev-ref", "HEAD"),
            "clean": _git(path, "status", "--porcelain") == "",
            "provides": provides, "deploys": _deploys(kind, provides)}


def _deploys(kind: str, provides: dict) -> str:
    """What `improve deploy` does for this repository, said where the
    Programmer reads it: a plugin or extension is deployed like any service,
    by the names its plugin.yml declares, and the assistants pick up a skill
    it serves on their next fetch."""
    if kind == "stack":
        return ("the stack services whose files the fix changed (their units' `dir:` in "
                "deploy/manifest.yml), then admin")
    services = provides.get("services") or []
    if not services:
        return "nothing: it declares no services"
    return (f"its services from plugin.yml: {', '.join(services)} "
            f"(`./home-stack deploy <service>` each); a skill it serves reaches the "
            f"assistants within five minutes, when they fetch it again -- no assistant deploy")


def _stack_provides() -> dict:
    skills = sorted(p.name for p in (ROOT / "services" / "nanobot" / "nanobot" / "skills").iterdir()
                    if p.is_dir() and (p / "SKILL.md").exists()) \
        if (ROOT / "services" / "nanobot" / "nanobot" / "skills").is_dir() else []
    services = sorted(p.name for p in (ROOT / "services").iterdir() if p.is_dir()) \
        if (ROOT / "services").is_dir() else []
    return {"services": services, "skills": skills,
            "areas": ["deploy", "admin", "i18n", "docs"]}


def _plugin_provides(doc: dict) -> dict:
    env: list[str] = []
    for spec in (doc.get("contributes") or {}).values():
        if isinstance(spec, dict):
            env += list((spec.get("env") or {}).keys())
    return {"services": sorted((doc.get("services") or {}).keys()), "env": sorted(set(env))}


def discover(cfg: dict) -> list[dict]:
    imp = (cfg.get("assistant") or {}).get("improve") or {}
    exclude = set(imp.get("exclude") or [])
    out: list[dict] = []
    stack = _repo("smart-home-bot", "stack", ROOT, "auto", _stack_provides())
    if stack:
        # The name the remote gives it, when there is one: what a person calls it.
        m = re.search(r"([^/:]+?)(?:\.git)?$", stack["remote"] or "")
        stack["name"] = m.group(1) if m else stack["name"]
        out.append(stack)
    # One at a time: the deployer refuses the whole list over one bad plugin,
    # which is right for a deploy and wrong here -- a plugin that does not load
    # is the deployer's to report, not a reason the others stop being places a
    # fix can go.
    import deploy  # noqa: PLC0415
    plugins = []
    for directory in deploy.plugin_dirs(cfg):
        try:
            plugins += deploy.load_plugins({"plugins": [str(directory)]})
        except Exception as exc:  # noqa: BLE001
            print(f"  note: plugin {directory.name} not read: {exc}", file=sys.stderr)
    for p in plugins:
        r = _repo(p["name"], "plugin", Path(p["root"]), "auto", _plugin_provides(p["doc"]))
        if r:
            out.append(r)
    for extra in imp.get("repos") or []:
        if not isinstance(extra, dict) or not extra.get("path"):
            continue
        r = _repo(str(extra.get("name") or Path(extra["path"]).name), "extra",
                  Path(os.path.expanduser(str(extra["path"]))), "config",
                  {"about": str(extra.get("about") or "")})
        if r:
            out.append(r)
    seen: set[str] = set()
    result = []
    for r in out:
        if r["name"] in exclude or r["path"] in seen:
            continue
        seen.add(r["path"])
        result.append(r)
    return result


def write(d: Path, repos: list[dict]) -> Path:
    path = d / "repos.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"repos": repos}, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path
