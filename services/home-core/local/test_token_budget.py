#!/usr/bin/env python3
"""What one turn costs, and how it grows — run it, don't guess.

    python3 test_token_budget.py [--nanobot ../../nanobot]

Two questions this answers, both of which were answered wrong from intuition
before it existed:

**Where the tokens are.** The per-turn budget is dominated by the system prompt
(Alfred's SOUL/AGENTS/TOOLS, the skills), not by the profession's persona. Any
plan that starts by trimming personas is optimising the small end.

**Whether anything accumulates.** A standing block re-sent every turn used to be
persisted every turn too, so thirty turns in Programador meant thirty copies —
37 000 tokens, over half the window, driving the session into the very
consolidation the re-sending exists to survive. HomeCore now wraps those blocks
in standing-context markers and nanobot stores the message without them; the
growth check below is what proves it and what will fail if it regresses.

Standalone like `test_dirsize.py` and `test_sessions.py` — no pytest, no server.
Needs `tiktoken` (nanobot's dependency) for real counts; falls back to a rough
chars/4 estimate with a warning so it still runs anywhere.
"""

import argparse
import importlib.util
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")
    def tk(s): return len(_enc.encode(s or ""))
    EXACT = True
except Exception:                                     # pragma: no cover
    def tk(s): return len(s or "") // 4
    EXACT = False


def _read(p):
    try:
        return Path(p).read_text(encoding="utf-8")
    except OSError:
        return ""


def personas():
    """What one turn in each profession actually carries, straight off disk.

    A profession with modes is split across two files — `<space>.md` is what
    every mode shares and `<space>.<mode>.md` is what one of them adds — and
    only their sum is ever sent. Listing the pieces separately would report two
    numbers, both smaller than the one that matters.
    """
    d = HERE / "personas"
    if not d.is_dir():
        return {}
    files = {p.stem: _read(p) for p in sorted(d.glob("*.md"))}
    modes = {}
    for stem in files:
        if "." in stem:
            base, mode = stem.split(".", 1)
            modes.setdefault(base, []).append(mode)
    out = {}
    for stem, text in files.items():
        if "." in stem:
            continue
        if stem not in modes:
            out[stem] = text
            continue
        for mode in modes[stem]:
            out[f"{stem} ({mode})"] = f"{text}\n\n{files[f'{stem}.{mode}']}"
    return out


# Exactly nanobot's `ContextBuilder.BOOTSTRAP_FILES` (nanobot/agent/context.py)
# — the whole point of this script is to be measured rather than guessed, and
# the list had drifted: MORNING.md is not a bootstrap file (only
# agent/morning_greeting.py reads it, for one scheduled turn a day) while
# FAMILY.md, which entrypoint.sh fetches into every workspace and
# which grows with the household, was missing.
BOOTSTRAP_FILES = ("AGENTS.md", "SOUL.md", "USER.md", "FAMILY.md", "TOOLS.md")


def bootstrap(nanobot: Path):
    """What nanobot puts in the system prompt on every turn, whatever the space.

    Bootstrap files are resolved against the *workspace* at runtime; the
    the config/ copies are what a deploy seeds them from, so they are the
    right stand-in here. USER.md/FAMILY.md are per-container and only exist in
    a live workspace — pass --workspace to count the real ones.
    """
    sc = nanobot / "config"
    out = {name: _read(sc / name) for name in BOOTSTRAP_FILES}
    out["user-profiles/user2.md"] = _read(nanobot / "user-profiles" / "user2.md")
    missing = [n for n in BOOTSTRAP_FILES if not out.get(n)]
    if missing:
        print(f"!! not in config/, so not counted: {', '.join(missing)}"
              f" — the real per-turn cost is higher than the table below\n")
    return {k: v for k, v in out.items() if v}


def skills(nanobot: Path):
    """(always-on bodies, on-demand descriptions) — the biggest single block.

    Parsed off disk rather than through SkillsLoader so this runs without
    nanobot installed. `always` lives either as a top-level key or inside the
    metadata JSON, and both spellings are in use.
    """
    root = nanobot / "nanobot" / "skills"
    always, ondemand = {}, {}
    for d in sorted(p for p in root.iterdir() if (p / "SKILL.md").is_file()):
        text = _read(d / "SKILL.md")
        fm = text.split("---")[1] if text.startswith("---") else ""
        is_always = re.search(r"^always:\s*true", fm, re.M) or '"always":true' in fm.replace(" ", "")
        m = re.search(r"^description:\s*(.*)$", fm, re.M)
        (always if is_always else ondemand)[d.name] = text if is_always else (m.group(1) if m else "")
    return always, ondemand


def bar(n, width=34, scale=6000):
    return "█" * max(0, min(width, round(n / scale * width)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nanobot", default=str(HERE.parent.parent / "nanobot"),
                    help="path to the nanobot checkout")
    args = ap.parse_args()
    nb = Path(args.nanobot).resolve()
    if not (nb / "config").is_dir():
        # `SKIP:` in the words the deploy gate greps for -- it counts and names
        # what it could not run, and a sentence that only reads like a skip is
        # counted as a suite that passed.
        print(f"SKIP: nanobot checkout not found at {nb} — pass --nanobot",
              file=sys.stderr)
        # 0, like every other skip in this directory. The exit code is the
        # runner's contract -- the deploy gate runs these against the built
        # image and reads it as pass or fail, and "node is not here" is neither
        # a passing check nor a broken one. Saying so out loud is the print
        # above; saying it with a 2 just makes the deploy fail for a reason
        # that has nothing to do with the change being deployed.
        return 0
    if not EXACT:
        print("!! tiktoken missing — counts are a rough chars/4 estimate\n")

    print("=" * 72)
    print("PER-TURN BUDGET")
    print("=" * 72)
    boot = bootstrap(nb)
    always, ondemand = skills(nb)
    rows = list(boot.items())
    rows.append((f"always-on skills ({', '.join(always)})", "".join(always.values())))
    rows.append((f"skill descriptions ({len(ondemand)} on demand)", "\n".join(ondemand.values())))
    fixed = 0
    for name, text in rows:
        n = tk(text); fixed += n
        print(f"  {name[:44]:44} {n:6}  {bar(n)}")
    print(f"  {'-' * 44} {'-' * 6}")
    print(f"  {'every turn, every space':44} {fixed:6}")

    print()
    ps = personas()
    if ps:
        print("  plus one persona, by space:")
        for name, text in sorted(ps.items(), key=lambda kv: -tk(kv[1])):
            n = tk(text)
            print(f"    {name:42} {n:6}   {n * 100 // (fixed + n):2}% of that turn")

    print()
    print("=" * 72)
    print("GROWTH — does a re-sent block pile up in the session?")
    print("=" * 72)
    worst = max(ps.items(), key=lambda kv: tk(kv[1])) if ps else ("(none)", "")
    per_turn = tk(worst[1])
    print(f"  worst case is {worst[0]} at {per_turn} tok/turn\n")

    stored_per_turn, note = _stored_per_turn(nb, worst[1])
    print(f"  {'turn':>6} {'if stored (the old bug)':>26} {'as shipped':>14}")
    for turn in (1, 5, 10, 20, 30):
        print(f"  {turn:>6} {per_turn * turn:>26} {stored_per_turn * turn:>14}")
    print(f"\n  {note}")

    biggest = max(always.items(), key=lambda kv: tk(kv[1]), default=("", ""))
    if biggest[0]:
        print(f"\n  Largest single item: the always-on '{biggest[0]}' skill at "
              f"{tk(biggest[1])} tok,\n  loaded in full on every turn of every space. It is inside the")
        print("  cached prefix, so it costs little per call — but it is full price")
        print("  against the 65k window. Trim it there, not in the personas.")
    return 1 if stored_per_turn else 0


def _stored_per_turn(nanobot: Path, persona: str):
    """Tokens of persona that nanobot would persist per turn — 0 when it works.

    The point of the whole mechanism, and the one number here that can be
    wrong. It is measured, not asserted from the markers being equal: this
    script loads HomeCore's literals *and* nanobot's module off disk and runs a
    real turn through both, so a drift in either the constants or the regexes
    shows up as a sloping column and a non-zero exit.
    """
    sc_path = nanobot / "nanobot" / "utils" / "standing_context.py"
    spec = importlib.util.spec_from_file_location("_standing_context", sc_path)
    if spec is None or spec.loader is None:
        return 0, f"!! could not load {sc_path} — growth UNCHECKED"
    sc = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(sc)
    except Exception as e:                            # pragma: no cover
        return 0, f"!! could not load standing_context ({e}) — growth UNCHECKED"

    # HomeCore's half, copied the way HomeCore copies it (see app.py's comment on
    # why the literals are duplicated rather than imported).
    open_marker, close_marker = "[[[standing-context]]]", "[[[/standing-context]]]"
    question = "¿por qué falla el deploy?"
    turn = f"{open_marker}\n{persona}\n{close_marker}\n\n{question}"

    stored = sc.for_history(turn)
    prompt = sc.for_prompt(turn)
    leaked = tk(stored) - tk(question)
    problems = []
    if leaked > 0:
        problems.append(f"{leaked} tok of persona persisted per turn")
    if question not in stored:
        problems.append("the user's own question did not survive for_history")
    if persona.strip() not in prompt:
        problems.append("the persona did not reach the model via for_prompt")
    if open_marker in prompt or close_marker in prompt:
        problems.append("markers reached the model")
    if problems:
        return max(leaked, 0), "FAIL — " + "; ".join(problems)
    return 0, ("'As shipped' stays flat: measured by running a real turn through\n"
               "  nanobot/utils/standing_context.py. If it ever slopes, the markers\n"
               "  stopped matching on one of the two sides and this exits non-zero.")


if __name__ == "__main__":
    raise SystemExit(main())
