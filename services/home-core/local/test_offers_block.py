"""A finished job in the Programmer ends with buttons, and the model is told so last.

Run: python local/test_offers_block.py   (needs Flask; skips loudly without it)

The agent file said "end with an `:::ask` block". The standing context, which
is what the model reads *last* on every turn, carried the ask rules -- "only if
it changes what you deliver", "asking is for when being wrong costs redoing the
work" -- and a model reads those, correctly, as "no buttons on an offer". So a
finished change ended in «queda en la rama esperando merge» with nothing to
tap, and "also offer continue working" never happened.

The fix is a block of its own placed *after* the ask rules, so it wins. That
order is the whole point and is what this pins: present in the Programmer,
after the ask block, and absent from a profession that has no project.
"""
import os
import shutil
import sys
import tempfile

SRC = os.path.dirname(os.path.abspath(__file__))
try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-offers-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32)
sys.path.insert(0, dst)
import app as A  # noqa: E402

failures = []


def check(name, ok, why=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else "  -- " + why))
    if not ok:
        failures.append(name)


with A.app.test_request_context("/"):
    prog = A._compose_turn_content("user1", "hola", [], [], "programmer",
                                   project="fracciones")
    other = A._compose_turn_content("user1", "hola", [], [], "teacher")

text = prog if isinstance(prog, str) else "".join(
    p.get("text", "") for p in prog if isinstance(p, dict))
other_text = other if isinstance(other, str) else "".join(
    p.get("text", "") for p in other if isinstance(p, dict))

ask_at = text.find("[Asking without making anybody type]")
offers_at = text.find("[When a piece of work lands]")

check("the Programmer's turn carries the ask rules", ask_at >= 0)
check("and the offers block", offers_at >= 0,
      "a finished job goes back to ending in prose")
check("with the offers block AFTER the ask rules",
      ask_at >= 0 and offers_at > ask_at,
      "placed first it loses to the rule that says not to ask")
check("the block names the carry-on option",
      "Seguir trabajando" in text,
      "the one option every offer must have")
check("and says the ask rule does not apply to finishing",
      "exception to rule 1" in text,
      "without that the two blocks simply contradict each other")
check("a profession with no project gets no offers block",
      "[When a piece of work lands]" not in other_text,
      "the Teacher does not commit, push or deploy anything")
check("standing context stays standing",
      text.count(A.STANDING_OPEN) == 1 and text.find("[When a piece of work lands]")
      < text.find(A.STANDING_CLOSE),
      "outside the markers it would be stored in history on every turn")

# --- and when the model leaves the block out anyway, home-core adds it --------
# Measured: with the instruction in both the agent file and the standing
# context, kimi-k2.7-code still ended in prose. The offer cannot depend on the
# model, so this is the part that actually guarantees a button.

import threading


def fake_turn(backend, space, text, project=None):
    return {'backend': backend, 'space': space, 'text': text, 'project': project,
            'user': 'user1', 'events': [], 'cond': threading.Condition(),
            'done': False, 'id': 't1'}


t = fake_turn('opencode', 'programmer', 'Hecho.\n\n- Rama: alfred/x\n- Commit: abc')
added = A._offers_fallback(t)
check("an opencode turn that ended in prose gets a block appended",
      added.startswith('\n\n:::ask') and t['text'].endswith(':::'),
      "the buttons the agent was asked to end with are still missing")
check("it always offers to carry on", 'Seguir trabajando' in t['text'])
check("and to put the work on master", 'Publicar en master' in t['text'])
check("no deploy offered for a project this side knows nothing about",
      'desplegar' not in t['text'],
      "offering a deploy with no script to run is a button that fails")
check("the block was also streamed to the page",
      any(e.get('text') == added for e in t['events']),
      "appended to history but never shown is a button nobody saw")

t2 = fake_turn('opencode', 'programmer', 'Listo.\n\n:::ask\nq: ¿Sigo?\n- A\n:::')
check("a turn that already has a block is left alone",
      A._offers_fallback(t2) == '' and t2['text'].count(':::ask') == 1,
      "two sets of buttons for one question")

t3 = fake_turn('nanobot', 'programmer', 'Hecho.')
check("a nanobot turn is left alone", A._offers_fallback(t3) == '' and t3['text'] == 'Hecho.',
      "nanobot's own persona handles its offers")

t4 = fake_turn('opencode', 'teacher', 'Hecho.')
check("a space with no project is left alone",
      A._offers_fallback(t4) == '' and t4['text'] == 'Hecho.')

t5 = fake_turn('opencode', 'programmer', '')
check("an empty answer gets no buttons",
      A._offers_fallback(t5) == '',
      "buttons under an error would offer to merge work that does not exist")

print()
if failures:
    print(f"{len(failures)} check(s) failed")
    sys.exit(1)
print("all checks passed")
