"""`alfred-skill`: the household's skills as one command, for a harness that
only has a shell.

    python3 -m nanobot.harness.alfred_skill list
    python3 -m nanobot.harness.alfred_skill guide document
    python3 -m nanobot.harness.alfred_skill document create_doc '{"format": "pdf", ...}'

A background task can run on pi (pi.dev), which gives a model read, write,
edit and bash and nothing else -- no MCP, no nanobot tools. Every skill here is
already a Python guide (SKILL_PYTHON.md) that nanobot's runner turns into code
for a `{"skill", "action", ...}` call; this does the same translation, so pi
reaches document, paperless, the file share and the rest without a line of any
skill being rewritten, and a skill fixed for Alfred is fixed here too. Web
search and page reading, which are nanobot tools rather than skills, come as a
`web` entry of their own.

Found the way SkillsLoader finds them -- the workspace, then the skills a
service serves (`.skills-remote/`), then the builtins -- and a skill the
instance's config switches off is off here as well. SkillsLoader itself is not
constructed: it starts the remote-skill refresher thread, which a one-shot
command has no business owning.

What this does NOT decide is who may run what: SubagentManager routes only the
member's own tasks to the harness, never one set off by another member or from
WhatsApp.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from nanobot.agent.runner import _load_skill_python_guide, _static_skill_translation
from nanobot.agent.skills import BUILTIN_SKILLS_DIR

_REMOTE = ".skills-remote"
_FRONT = re.compile(r"^---\n(.*?)\n---\n?", re.S)
TIMEOUT_S = int(os.environ.get("ALFRED_SKILL_TIMEOUT", "180"))


def _settings() -> tuple[Path, set[str]]:
    """(workspace, disabled skill names), from the instance's own config."""
    try:
        from nanobot.config.loader import load_config
        d = load_config().agents.defaults
        return Path(os.path.expanduser(d.workspace)), set(d.disabled_skills or ())
    except Exception:                                      # noqa: BLE001
        return Path.home() / ".nanobot" / "workspace", set()


def _roots() -> list[Path]:
    workspace, _ = _settings()
    return [workspace / "skills", workspace / _REMOTE, Path(BUILTIN_SKILLS_DIR)]


def find(name: str) -> Path | None:
    """The SKILL.md for *name*, by the loader's precedence, or None."""
    _, disabled = _settings()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name or "") or name in disabled:
        return None
    for root in _roots():
        path = root / name / "SKILL.md"
        if path.is_file():
            return path
    return None


def _description(path: Path) -> str:
    m = _FRONT.match(path.read_text(encoding="utf-8"))
    if not m:
        return ""
    for line in m.group(1).splitlines():
        if line.startswith("description:"):
            text = line.split(":", 1)[1].strip()
            if text.startswith('"'):
                try:
                    text = json.loads(text)
                except ValueError:
                    text = text.strip('"')
            return text
    return ""


def list_skills() -> list[dict]:
    """Every skill with a Python guide -- the ones this command can run."""
    _, disabled = _settings()
    seen: dict[str, dict] = {}
    for root in _roots():
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            md = d / "SKILL.md"
            if d.name in seen or d.name in disabled or not md.is_file():
                continue
            if not _load_skill_python_guide(str(md)):
                continue
            actions = re.findall(r"^def ([a-z]\w*)\(", _load_skill_python_guide(str(md)) or "", re.M)
            seen[d.name] = {"skill": d.name, "actions": actions,
                            "description": _description(md)[:300]}
    return [WEB] + list(seen.values())


# Web search and page reading are nanobot *tools*, not skills, so they have no
# Python guide to translate. Offered here under a skill name of their own,
# built from the instance's `tools.web` config: the same provider and proxy
# Alfred searches with, not a second route to the internet.
WEB = {"skill": "web", "actions": ["search", "fetch"],
       "description": "Search the web (search: query, [count]) and read a page as "
                      "Markdown (fetch: url, [maxChars]). Read the pages you cite."}


def _web(action: str, args: dict) -> tuple[int, str]:
    import asyncio
    from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
    try:
        from nanobot.config.loader import load_config
        web = load_config().tools.web
    except Exception:                                      # noqa: BLE001
        from nanobot.config.schema import WebToolsConfig
        web = WebToolsConfig()
    if action == "search" and args.get("query"):
        tool = WebSearchTool(config=web.search, proxy=web.proxy)
        call = tool.execute(query=str(args["query"]), count=args.get("count"))
    elif action == "fetch" and args.get("url"):
        tool = WebFetchTool(proxy=web.proxy)
        call = tool.execute(url=str(args["url"]), maxChars=args.get("maxChars") or 20000)
    else:
        return 2, json.dumps({"error": "web: search needs {\"query\"}, fetch needs {\"url\"}"})
    try:
        out = asyncio.run(asyncio.wait_for(call, TIMEOUT_S))
    except Exception as exc:                               # noqa: BLE001
        return 1, json.dumps({"error": f"web.{action}: {type(exc).__name__}: {exc}"})
    return 0, out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)


def run(skill: str, action: str, args: dict) -> tuple[int, str]:
    """Run one action. (exit code, what it printed)."""
    if skill == "web":
        return _web(action, args)
    path = find(skill)
    if not path:
        names = ", ".join(s["skill"] for s in list_skills())
        return 2, json.dumps({"error": f"no skill called {skill!r}", "skills": names})
    guide = _load_skill_python_guide(str(path))
    if not guide:
        return 2, json.dumps({"error": f"{skill} has no Python guide; read it with "
                                       f"`alfred-skill guide {skill}`"})
    # The call's own skill and action win: `args` naming another action would
    # otherwise replace it -- a "list_documents" call carrying
    # {"action": "delete_document"} ran the delete, past every check made on
    # the name it was called with.
    if {"skill", "action"} & set(args):
        return 2, json.dumps({"error": "arguments may not contain 'skill' or 'action'"})
    code = _static_skill_translation({**args, "skill": skill, "action": action}, guide)
    if not code:
        actions = re.findall(r"^def ([a-z]\w*)\(", guide, re.M)
        return 2, json.dumps({"error": f"{skill} has no action {action!r}", "actions": actions})
    try:
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return 124, json.dumps({"error": f"{skill}.{action} took longer than {TIMEOUT_S}s"})
    out = (done.stdout or "").strip()
    if done.returncode != 0:
        err = (done.stderr or "").strip().splitlines()[-5:]
        return done.returncode, json.dumps({"error": f"{skill}.{action} failed",
                                            "detail": "\n".join(err), "output": out[:2000]})
    return 0, out


USAGE = """usage:
  alfred-skill list                          the skills and their actions
  alfred-skill guide <skill>                 how to use one (read it before the first call)
  alfred-skill <skill> <action> '<json>'     run an action; arguments as one JSON object
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] == "list":
        for s in list_skills():
            print(f"{s['skill']}: {', '.join(s['actions'])}\n    {s['description']}")
        return 0
    if argv[0] == "guide":
        if len(argv) > 1 and argv[1] == "web":
            print(WEB["description"] + "\n\n  alfred-skill web search '{\"query\": \"...\", \"count\": 5}'"
                  "\n  alfred-skill web fetch '{\"url\": \"https://...\"}'")
            return 0
        path = find(argv[1]) if len(argv) > 1 else None
        if not path:
            print(USAGE, file=sys.stderr)
            return 2
        print(path.read_text(encoding="utf-8"))
        return 0
    if len(argv) < 2:
        print(USAGE, file=sys.stderr)
        return 2
    raw = argv[2] if len(argv) > 2 else "{}"
    try:
        args = json.loads(raw)
    except ValueError as exc:
        print(json.dumps({"error": f"the arguments are not valid JSON: {exc}"}))
        return 2
    if not isinstance(args, dict):
        print(json.dumps({"error": "the arguments must be one JSON object"}))
        return 2
    code, out = run(argv[0], argv[1], args)
    print(out)
    return code


if __name__ == "__main__":
    sys.exit(main())
