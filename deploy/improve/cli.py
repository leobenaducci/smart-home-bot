"""./home-stack improve -- the self-improvement pipeline's house side.

    ./home-stack improve collect                 yesterday's episodes, redacted, into the inbox
    ./home-stack improve collect --day 2026-09-29
    ./home-stack improve collect --days 7        the last seven days
    ./home-stack improve collect --no-model      without the local model's name pass
    ./home-stack improve inbox                   what the inbox holds, by signal
    ./home-stack improve repos                   where a fix may be made: this stack and its plugins
    ./home-stack improve start <id> <repo>       a worktree on improve/<id> for request <id>
    ./home-stack improve commit <id> <repo> -m   commit the fix there, after the checks
    ./home-stack improve list [--login L]        fix requests, their worktrees, committed or published
    ./home-stack improve status <id>             the request's worktrees and commits
    ./home-stack improve publish <id> <repo>     fast-forward the repository to the fix (and push a plugin's)
    ./home-stack improve deploy <id> <repo>      deploy what the published fix changed

The Programmer calls the last three as `{paths.state}/improve/bin/improve`.

Nothing here calls a model outside the house. The inbox it writes is what the
evaluator reads (docs/self-improvement.md); the raw history never is.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import collect as C  # noqa: E402
import redact as R  # noqa: E402
import repos as RP  # noqa: E402
import work as W  # noqa: E402
import ship as S  # noqa: E402
import checks as K  # noqa: E402
import measure as M  # noqa: E402

TEXT_FIELDS = ("request", "answer", "next", "feedback")


def live_config() -> dict:
    import yaml  # noqa: PLC0415
    import deploy  # noqa: PLC0415 -- resolves the live config as every command does
    return yaml.safe_load(Path(deploy.CONFIG).read_text(encoding="utf-8")) or {}


def improve_dir(cfg: dict) -> Path:
    state = Path((cfg.get("paths") or {}).get("state") or "/var/lib/home-stack/state")
    d = Path(os.environ.get("HOME_STACK_IMPROVE_DIR") or state / "improve")
    for sub in ("", "inbox", "private", "work"):
        (d / sub).mkdir(parents=True, exist_ok=True)
        os.chmod(d / sub, 0o700)
    W.write_shim(d)
    # What the Programmer reads first, where it can: the map and the rules,
    # refreshed from this checkout, and the lessons, seeded once and then the
    # household's (state -- they may name this house's things).
    (d / "docs").mkdir(exist_ok=True)
    for src, name in ((HERE / "MAP.md", "MAP.md"), (HERE / "RULES.md", "RULES.md"),
                      (HERE.parent.parent / "CLAUDE.md", "CLAUDE.md")):
        if src.exists():
            (d / "docs" / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    lessons = d / "lessons.md"
    if not lessons.exists() and (HERE / "lessons.seed.md").exists():
        lessons.write_text((HERE / "lessons.seed.md").read_text(encoding="utf-8"), encoding="utf-8")
    return d


def salt(d: Path) -> bytes:
    """The key the tokens are hashed with: stable across runs, so a name is the
    same token every night, and local, so a token cannot be reversed by
    hashing a guess."""
    p = d / "private" / "salt"
    if not p.exists():
        p.write_bytes(secrets.token_bytes(32))
        os.chmod(p, 0o600)
    return p.read_bytes()


def pseudonym(login: str, key: bytes) -> str:
    import hashlib  # noqa: PLC0415
    import hmac  # noqa: PLC0415
    return "member-" + hmac.new(key, login.encode(), hashlib.sha256).hexdigest()[:4]


def names_model(cfg: dict):
    """The local server the text roles use, and its model: the same one that
    reviews Studio frames. None when the house has no local text model."""
    models = (cfg.get("assistant") or {}).get("models") or {}
    spec = str(models.get("vision") or models.get("notifications") or "")
    prefix, _, model = spec.partition(":")
    if not prefix.startswith("ollama") or not model:
        return None
    sys.path.insert(0, str(HERE.parent))
    try:
        import ollama_instances  # noqa: PLC0415
        inst = ollama_instances.by_id(cfg)[ollama_instances.id_of_provider(
            ollama_instances.provider_of_prefix(prefix))]
        port = int(inst["port"])
    except Exception:  # noqa: BLE001
        return None
    return R.ollama_names(f"http://127.0.0.1:{port}", model)


def cmd_collect(args) -> int:
    cfg = live_config()
    state = Path((cfg.get("paths") or {}).get("state") or "/var/lib/home-stack/state")
    tz = ZoneInfo((cfg.get("site") or {}).get("timezone") or "UTC")
    d = improve_dir(cfg)
    key = salt(d)
    sanitize = R.sanitizer()
    if sanitize is None:
        print("  warning: deploy/sanitize-rules.local.py is missing, so the invented cast "
              "is not applied; the harvested names and the guard still are")
    model = None if args.no_model else names_model(cfg)
    if not args.no_model and model is None:
        print("  note: no local text model found; the name pass is skipped")
    red = R.Redactor(R.harvest(cfg, state), key, sanitize=sanitize, names_model=model)
    today = datetime.now(tz).date()
    days = [args.day] if args.day else [
        (today - timedelta(days=i)).isoformat() for i in range(args.days, 0, -1)]
    total = 0
    for day in days:
        eps = C.collect(state, day, tz=tz, sample=args.sample)
        out = []
        for ep in eps:
            ep = dict(ep, who=pseudonym(ep.pop("login"), key))
            clean = red.episode(ep, TEXT_FIELDS)
            if clean is not None:
                out.append(clean)
        path = d / "inbox" / f"{day}.jsonl"
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in out),
                       encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        sig = Counter(s for e in out for s in e["signals"])
        print(f"  {day}: {len(out)} episode(s)"
              + (f", {sum(1 for e in out if e.get('sampled'))} sampled" if out else "")
              + (f"  [{', '.join(f'{k} {v}' for k, v in sig.most_common(6))}]" if sig else ""))
        total += len(out)
    if red.withheld:
        print(f"  withheld {red.withheld} episode(s): an identifier survived redaction")
    print(f"inbox: {total} episode(s) in {d / 'inbox'}")
    # Kept current beside the inbox: a plugin added since yesterday is a place
    # a fix may go today.
    RP.write(d, RP.discover(cfg))
    return 0


def cmd_repos(args) -> int:
    cfg = live_config()
    repos = RP.discover(cfg)
    path = RP.write(improve_dir(cfg), repos)
    for r in repos:
        what = r["provides"]
        detail = ", ".join(what.get("services") or []) or what.get("about") or ""
        if what.get("env"):
            detail += f"  (assistant env: {', '.join(what['env'])})"
        print(f"  {r['name']:<18} {r['kind']:<7} {r['path']}")
        print(f"  {'':<18} {r['web'] or r['remote'] or 'no remote'}  on {r['branch'] or '?'}"
              f"{'' if r['clean'] else ', uncommitted changes'}")
        if detail:
            print(f"  {'':<18} {detail[:150]}")
    print(f"{len(repos)} repositor{'y' if len(repos) == 1 else 'ies'}, written to {path}")
    return 0


def cmd_inbox(args) -> int:
    d = improve_dir(live_config())
    files = sorted((d / "inbox").glob("*.jsonl"))
    if not files:
        print("the inbox is empty: run `./home-stack improve collect`")
        return 0
    for f in files[-args.days:]:
        eps = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line]
        sig = Counter(s for e in eps for s in e["signals"])
        print(f"{f.stem}: {len(eps)}  " + ", ".join(f"{k} {v}" for k, v in sig.most_common(8)))
    return 0


def _work(fn):
    try:
        print(fn())
        return 0
    except W.WorkError as exc:
        print(f"improve: {exc}", file=sys.stderr)
        return 1


NOT_FILED = ("there is no fix request #{rid}. Only the person can file one: ask them to press "
             "\"🛠 Make it a fix request\" beside the project selector in this conversation (or to "
             "ask Alfred in the chat), then start with the number it gets. Work under a number "
             "nobody filed has no conversation to read an approval from, so it can never be "
             "published.")


def filed(state: Path, rid: str) -> bool:
    """Whether the portal filed request *rid* (improve.db)."""
    import sqlite3  # noqa: PLC0415
    db = state / "home-core" / "data" / "improve.db"
    if not str(rid).isdigit() or not db.exists():
        return False
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute("SELECT 1 FROM improve_requests WHERE id = ?", (int(rid),)).fetchone() is not None
    finally:
        con.close()


def cmd_start(args) -> int:
    cfg = live_config()
    d = improve_dir(cfg)
    state = Path((cfg.get("paths") or {}).get("state") or "/var/lib/home-stack/state")
    # A request the portal filed, never a number picked by whoever runs this:
    # #11 was started from an ordinary Programmer conversation before anyone
    # had filed it (2026-10-04), the fix was made and committed, and publishing
    # it was then refused for having no conversation to read the yes from.
    # Refused here instead, before any work, with what to do about it.
    if not filed(state, args.id):
        print("improve: " + NOT_FILED.format(rid=args.id), file=sys.stderr)
        return 1
    repos = RP.discover(cfg)
    RP.write(d, repos)
    return _work(lambda: f"worktree: {W.start(d, repos, args.id, args.repo)}\n"
                         f"edit there, then: improve commit {args.id} {args.repo} -m \"...\"")


def cmd_commit(args) -> int:
    cfg = live_config()
    return _work(lambda: W.commit(improve_dir(cfg), RP.discover(cfg), args.id, args.repo,
                                  args.message, cfg))


def requests(state: Path, login: str | None = None, limit: int = 20) -> list[dict]:
    """Fix requests as the portal filed them (improve.db), newest first. Read
    only: the portal owns that database."""
    import sqlite3  # noqa: PLC0415
    db = state / "home-core" / "data" / "improve.db"
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        sql = "SELECT id, username, created_at, status, problem FROM improve_requests"
        args: list = []
        if login:
            sql += " WHERE username = ?"
            args.append(login)
        rows = con.execute(sql + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
    finally:
        con.close()
    return [{"id": r[0], "login": r[1], "created": r[2], "status": r[3], "problem": r[4]}
            for r in rows]


def cmd_list(args) -> int:
    import datetime  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    cfg = live_config()
    state = Path((cfg.get("paths") or {}).get("state") or "/var/lib/home-stack/state")
    d = improve_dir(cfg)
    repos = {r["name"]: r for r in RP.discover(cfg)}
    reqs = requests(state, args.login)
    if not reqs:
        print("no fix requests")
        return 0
    for q in reqs:
        when = datetime.datetime.fromtimestamp(q["created"]).strftime("%Y-%m-%d %H:%M")
        print(f"#{q['id']}  {q['status']:<13} {when}  {' '.join(q['problem'].split())[:110]}")
        for wt in sorted(p for p in (d / "work").glob(f"{q['id']}-*") if p.is_dir()):
            name = wt.name.split("-", 1)[1]
            live = (repos.get(name) or {}).get("path")
            n = len([c for c in W._git(live or wt, "rev-list", f"HEAD..improve/{q['id']}",
                                       check=False).split() if c]) if live else 0
            published = bool(live) and subprocess.run(
                ["git", "-C", live, "merge-base", "--is-ancestor", f"improve/{q['id']}", "HEAD"],
                capture_output=True).returncode == 0 and bool(W._git(
                    live, "log", "--format=%h", "--fixed-strings",
                    f"--grep=Fix request #{q['id']},", "HEAD", "-n", "1", check=False))
            state_ = ("published" if published else
                      f"{n} commit(s) to publish" if n else "nothing committed")
            dirty = len(W._changed(wt))
            print(f"      {name:<16} {state_}{f', {dirty} uncommitted' if dirty else ''}  {wt}")
    return 0


def _state(cfg: dict) -> Path:
    return Path((cfg.get("paths") or {}).get("state") or "/var/lib/home-stack/state")


def cmd_measure(args) -> int:
    try:
        print(M.KINDS[args.what](_state(live_config()), args.days))
        return 0
    except FileNotFoundError as exc:
        print(f"improve: {exc}", file=sys.stderr)
        return 1


def cmd_test(args) -> int:
    cfg = live_config()
    try:
        rec = K.run_tests(improve_dir(cfg), RP.discover(cfg), args.id, args.repo)
    except W.WorkError as exc:
        print(f"improve: {exc}", file=sys.stderr)
        return 1
    if not rec["suites"]:
        print("no test suite covers what changed")
    for s in rec["suites"]:
        print(f"{'ok  ' if s['ok'] else 'FAIL'} {s['suite']} ({s['seconds']}s)"
              + ("" if s["ok"] else "\n      " + "\n      ".join(s["tail"])))
    print("tests pass" if rec["ok"] else "tests FAIL -- improve commit will refuse this change")
    return 0 if rec["ok"] else 1


def cmd_bench(args) -> int:
    cfg = live_config()
    model = args.model or str(((cfg.get("assistant") or {}).get("models") or {}).get("everyday") or "")
    if not model:
        print("improve: no everyday model in the config; pass --model", file=sys.stderr)
        return 1
    state = _state(cfg)
    try:
        rec = K.run_bench(improve_dir(cfg), RP.discover(cfg), args.id, args.repo, args.container,
                          model, args.roles, args.repeat,
                          busy=lambda: S.deploy_running() or S.bench_running(state))
    except W.WorkError as exc:
        print(f"improve: {exc}", file=sys.stderr)
        return 1
    for role, t in rec["table"].items():
        mark = "  worse" if role in rec["worse"] else ""
        print(f"  {role:<12} before {t['before']}/{t['total']}  after {t['after']}/{t['total']}{mark}")
    fails = sorted(set(sum((r["failing"] for r in rec["runs"]["after"]), [])))
    if fails:
        print("  failing after: " + ", ".join(fails))
    print("no role got worse" if rec["ok"] else "a role got worse -- improve commit will refuse this change")
    return 0 if rec["ok"] else 1


def cmd_lesson(args) -> int:
    import datetime  # noqa: PLC0415
    d = improve_dir(live_config())
    text = " ".join(args.text.split())
    if len(text) < 15:
        print("improve: a lesson is a sentence: what went wrong, and what to do instead",
              file=sys.stderr)
        return 1
    with (d / "lessons.md").open("a", encoding="utf-8") as fh:
        fh.write(f"- {text} ({datetime.date.today().isoformat()})\n")
    print("added to lessons.md")
    return 0


def cmd_publish(args) -> int:
    cfg = live_config()
    return _work(lambda: S.publish(improve_dir(cfg), RP.discover(cfg), args.id, args.repo,
                                   _state(cfg)))


def cmd_deploy(args) -> int:
    cfg = live_config()
    state = Path((cfg.get("paths") or {}).get("state") or "/var/lib/home-stack/state")
    return _work(lambda: S.deploy(improve_dir(cfg), RP.discover(cfg), args.id, args.repo, state))


def cmd_status(args) -> int:
    return _work(lambda: W.status(improve_dir(live_config()), args.id))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="home-stack improve")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="episodes, redacted, into the inbox")
    c.add_argument("--day", help="one local day, YYYY-MM-DD")
    c.add_argument("--days", type=int, default=1, help="the last N days before today")
    c.add_argument("--sample", type=float, default=0.08,
                   help="share of episodes without a signal to keep")
    c.add_argument("--no-model", action="store_true", help="skip the local model's name pass")
    c.set_defaults(func=cmd_collect)
    i = sub.add_parser("inbox", help="what the inbox holds")
    i.add_argument("--days", type=int, default=14)
    i.set_defaults(func=cmd_inbox)
    r = sub.add_parser("repos", help="where a fix may be made")
    r.set_defaults(func=cmd_repos)
    s = sub.add_parser("start", help="a worktree for a fix request")
    s.add_argument("id")
    s.add_argument("repo")
    s.set_defaults(func=cmd_start)
    c2 = sub.add_parser("commit", help="commit a fix request's change")
    c2.add_argument("id")
    c2.add_argument("repo")
    c2.add_argument("-m", "--message", required=True)
    c2.set_defaults(func=cmd_commit)
    st = sub.add_parser("status", help="a fix request's worktrees")
    st.add_argument("id")
    st.set_defaults(func=cmd_status)
    ls = sub.add_parser("list", help="fix requests and where each stands")
    ls.add_argument("--login", help="only this person's")
    ls.set_defaults(func=cmd_list)
    ms = sub.add_parser("measure", help="numbers from usage.db")
    ms.add_argument("what", choices=sorted(M.KINDS))
    ms.add_argument("--days", type=int, default=7)
    ms.set_defaults(func=cmd_measure)
    ts = sub.add_parser("test", help="the test suites of what a worktree changed")
    ts.add_argument("id")
    ts.add_argument("repo")
    ts.set_defaults(func=cmd_test)
    bn = sub.add_parser("bench", help="the benchmark before and after a change")
    bn.add_argument("id")
    bn.add_argument("repo")
    bn.add_argument("--roles", default=K.DEFAULT_ROLES)
    bn.add_argument("--repeat", type=int, default=1)
    bn.add_argument("--container", default="nanobot-user1",
                    help="the assistant container to run it in")
    bn.add_argument("--model", default="", help="default: the everyday model")
    bn.set_defaults(func=cmd_bench)
    le = sub.add_parser("lesson", help="add a lesson for the next time")
    le.add_argument("text")
    le.set_defaults(func=cmd_lesson)
    pb = sub.add_parser("publish", help="put a committed fix on its repository")
    pb.add_argument("id")
    pb.add_argument("repo")
    pb.set_defaults(func=cmd_publish)
    dp = sub.add_parser("deploy", help="deploy a published fix")
    dp.add_argument("id")
    dp.add_argument("repo")
    dp.set_defaults(func=cmd_deploy)
    args = ap.parse_args(argv)
    if getattr(args, "day", None):
        date.fromisoformat(args.day)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
