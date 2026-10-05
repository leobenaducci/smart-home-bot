"""The rules a fix is held to that can be checked by code (docs/RULES.md has
all of them):

* **No leak.** What a commit adds is compared with this machine's real
  credentials, the user store's logins, e-mails and phones, and key-shaped
  strings -- the publish gate's own values (deploy/publish_check.py) -- in
  every repository, a plugin's included. It says which kind of value and
  where, never the value.
* **A code change comes with a test.** Changing code and no test is refused;
  documentation, prompts and configuration are not code.
* **Publishing and deploying are the person's to ask for.** Read from the fix
  request's own conversation: their latest message after the fix's last commit
  must ask for it, in words, or be a yes to the Programmer asking about it. The
  Programmer cannot approve itself, and a new commit needs a new approval.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import work as W  # noqa: E402

CODE = re.compile(r"\.(py|js|mjs|ts|html|sh|php|go|rs|c|cpp|ino)$")
TEST = re.compile(r"(^|/)(tests?/|test_[^/]*|[^/]*_test\.[a-z]+$|[^/]*\.test\.[a-z]+$)")
PUBLISH = re.compile(r"\b(public|publiqu|publish|merge)", re.I)
DEPLOY = re.compile(r"\b(despleg|desplieg|deploy)", re.I)
YES = re.compile(r"^\s*(s[ií]|yes|dale|ok|okay|de una|hacelo|adelante|claro)\b", re.I)
SPACE_DIRS = ("programmer", "programador")
# The first line of a fix request's opening message (the portal's
# `_improve_prompt`): never an answer from the person.
OPENING = re.compile(r"Fix request #\d+, (made from this conversation|asked of Alfred):")


def _added(path: Path) -> list[tuple[str, str]]:
    """(file, line) for what the worktree adds against its HEAD, untracked
    files included -- a new file is added lines too."""
    import publish_check  # noqa: PLC0415
    out = publish_check.added_lines(W._git(path, "diff", "HEAD", "--no-color", "--text",
                                           check=False))
    for status, name in W._changed(path):
        if status.strip() == "??" and (path / name).is_file():
            try:
                text = (path / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            out += [(name, line) for line in text.splitlines()]
    return out


def leaks(path: Path, cfg: dict) -> list[str]:
    """Which kinds of real value the change adds, and where."""
    import publish_check  # noqa: PLC0415
    paths = cfg.get("paths") or {}
    envs = [W.ROOT / "secrets" / "smart-home-bot.env"]
    if paths.get("config"):
        envs.append(Path(paths["config"]) / "smart-home-bot.env")
    needles = {**publish_check.env_values(envs),
               **publish_check.user_values(str(paths.get("state") or ""))}
    lines = _added(path)
    found = []
    for value, label in needles.items():
        where = sorted({f for f, line in lines if value in line})
        if where:
            found.append(f"{label} in {', '.join(where[:5])}")
    for pattern, label in publish_check.KEY_SHAPES:
        rx = re.compile(pattern)
        where = sorted({f for f, line in lines if rx.search(line)})
        if where:
            found.append(f"{label} in {', '.join(where[:5])}")
    return found


def needs_test(files: list[str]) -> str | None:
    code = [f for f in files if CODE.search(f) and not TEST.search(f)]
    if code and not any(TEST.search(f) for f in files):
        return (f"it changes code ({', '.join(code[:4])}) and no test: add or change the "
                f"test that shows the fix works")
    return None


def _conversation(state: Path, login: str, conv: int) -> list[dict]:
    """The fix request's Programmer conversation, across the days it spans."""
    msgs: list[dict] = []
    base = state / "home-core" / "history" / login
    for d in SPACE_DIRS:
        for f in sorted((base / d).glob("*.json")) if (base / d).is_dir() else []:
            try:
                day = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            msgs += [m for m in day if isinstance(m, dict) and m.get("conv") == conv]
    return sorted(msgs, key=lambda m: m.get("ts") or 0)


def approved(state: Path, login: str, conv: int, since_ms: int, what: str) -> bool:
    """Whether the person's latest word in the conversation, after *since_ms*,
    asks for *what* ("publish" or "deploy")."""
    want = PUBLISH if what == "publish" else DEPLOY
    # A request's opening message is filed as the person's (it is their
    # request), and its rules say "publish and deploy only when I say so" --
    # which reads as asking for both. Alfred's requests open before any
    # commit, so it never counted; a conversation made a request after its
    # fix was committed (#11, 2026-10-05) would have approved itself.
    msgs = [m for m in _conversation(state, login, conv) if (m.get("ts") or 0) > since_ms
            and not (m.get("role") == "user" and OPENING.match(m.get("text") or ""))]
    last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=None)
    if last_user is None:
        return False
    said = msgs[last_user].get("text") or ""
    if want.search(said):
        return True
    # "Sí" to the Programmer's own "¿Lo publico?" -- the question it answers.
    asked = next((m.get("text") or "" for m in reversed(msgs[:last_user])
                  if m.get("role") == "bot"), "")
    return bool(YES.search(said)) and bool(want.search(asked[-600:]))


def request_of(state: Path, rid: str) -> tuple[str, int] | None:
    """(login, conv) of fix request *rid*, from the portal's database."""
    import sqlite3  # noqa: PLC0415
    db = state / "home-core" / "data" / "improve.db"
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT username, conv FROM improve_requests WHERE id = ?",
                          (int(rid),)).fetchone()
    finally:
        con.close()
    return (row[0], int(row[1])) if row and row[1] else None


def require_approval(state: Path, rid: str, wt: Path, what: str) -> None:
    """Refuse unless the person asked for *what* after the fix's last commit."""
    who = request_of(state, rid)
    if not who:
        raise W.WorkError(f"fix request #{rid} has no conversation to read an approval from")
    # When the fix was written (author date), not when it was last applied:
    # publishing rebases a fix whose checkout moved on, which re-dates every
    # commit, and the person's yes to the change would then read as given
    # "before" it -- a second yes for the same fix. What a rebase changes is
    # what it was tested with, and publish asks for the tests again for that.
    last_commit = int(W._git(wt, "log", "-1", "--format=%at", check=False) or 0) * 1000
    if not approved(state, who[0], who[1], last_commit, what):
        word = "publish" if what == "publish" else "deploy"
        raise W.WorkError(f"not asked to {word}: the person has not asked for it in #{rid}'s "
                          f"conversation since the last commit. Show them the change and ask.")
