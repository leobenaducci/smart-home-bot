"""./home-stack improve -- the self-improvement pipeline's house side.

    ./home-stack improve collect                 yesterday's episodes, redacted, into the inbox
    ./home-stack improve collect --day 2026-09-29
    ./home-stack improve collect --days 7        the last seven days
    ./home-stack improve collect --no-model      without the local model's name pass
    ./home-stack improve inbox                   what the inbox holds, by signal

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

TEXT_FIELDS = ("request", "answer", "next", "feedback")


def live_config() -> dict:
    import yaml  # noqa: PLC0415
    import deploy  # noqa: PLC0415 -- resolves the live config as every command does
    return yaml.safe_load(Path(deploy.CONFIG).read_text(encoding="utf-8")) or {}


def improve_dir(cfg: dict) -> Path:
    state = Path((cfg.get("paths") or {}).get("state") or "/var/lib/home-stack/state")
    d = Path(os.environ.get("HOME_STACK_IMPROVE_DIR") or state / "improve")
    for sub in ("", "inbox", "private"):
        (d / sub).mkdir(parents=True, exist_ok=True)
        os.chmod(d / sub, 0o700)
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
    args = ap.parse_args(argv)
    if getattr(args, "day", None):
        date.fromisoformat(args.day)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
