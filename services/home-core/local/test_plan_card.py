"""A turn's plan is filed with its reply, whichever copy of the reply lands first.

Run: python local/test_plan_card.py   (needs Flask; skips loudly without it)

2026-09-24: work of several steps runs in the chat with a checklist (nanobot's
plan tool), drawn by the page as a card above the reply. The page redraws it
from history, so the turn's last checklist has to be in the stored reply.
Two things file every reply -- the page and the turn -- and the page's copy
has no plan: the dedup that drops the second copy must not drop the plan.
"""
import os
import shutil
import sys
import tempfile
import time

SRC = os.path.dirname(os.path.abspath(__file__))
try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-plancard-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns("__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32, DEBUG_API_KEY="d" * 32)
USER = "user1"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2}]' % USER)
sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_bgtask_db()
A.init_persona_db()
failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


TODAY = A._tasks_today().isoformat()
PLAN = {"type": "plan", "event": "done", "steps": ["listar luces", "listar HA", "cruzar"],
        "done": {"1": "8 luces", "2": "10 luces en HA", "3": "1 coincide"}, "by": {"1": "qwen3.5:9b"}}
ANSWER = "Solo Luz Paula coincide por nombre."

print("\nthe page's copy first, the turn's second")
now = int(time.time() * 1000)
A.append_user_history(USER, {"role": "bot", "text": ANSWER, "ts": now}, TODAY)
A.append_user_history(USER, {"role": "bot", "text": ANSWER, "ts": now + 50, "plan": PLAN}, TODAY)
msgs = A.load_user_history(USER, TODAY)
bots = [m for m in msgs if m.get("role") == "bot"]
check("  one reply", len(bots) == 1, len(bots))
check("  carrying the plan", bots and bots[0].get("plan") == PLAN, bots)

print("\nthe turn files its last checklist with the reply")
turn = {"user": USER, "text": "Otra respuesta, con plan.", "day": TODAY, "space": None, "conv": None,
        "plan": dict(PLAN, done={"1": "x"}), "cancelled": False}
A._user_watching = lambda u: True
A._turn_deliver(turn)
last = [m for m in A.load_user_history(USER, TODAY) if m.get("role") == "bot"][-1]
check("  the stored reply has the plan", last.get("plan", {}).get("done") == {"1": "x"}, last)

print("\nthe page draws the card live and from history")
page = open(os.path.join(dst, "templates", "chat.html"), encoding="utf-8").read()
check("  a plan step paints the card", "parsed.step.type === 'plan'" in page and "paintPlan(" in page)
check("  a stored reply with a plan redraws it", "m.role === 'bot' && m.plan" in page)
check("  the plan tool's own calls are not listed as tool lines",
      "parsed.step.name === 'plan'" in page)

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("all checks passed")
