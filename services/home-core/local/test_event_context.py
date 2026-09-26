"""Answering a chore reminder: Alfred sees the reminder it sent.

Machine events (chore reminders, geofences, relayed notifications) are phrased
in an isolated `ev-*` session and only shown in the chat, so the conversation's
own session never had them. On 2026-09-25 a chore reminder went out at 21:30,
"No puedo" came back at 21:32, and Alfred answered "¿Qué no podés?".
_event_context_block puts what was shown since the person last wrote above
their message.

Plain script, like the other suites here: `python3 test_event_context.py`.
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

tmp = tempfile.mkdtemp(prefix="homecore-eventctx-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32)
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "999000111", "nanobot_id": 2}]')

sys.path.insert(0, dst)
import app as A  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


NOW = 1790382761199          # 2026-09-25 21:32:41 -03
MIN = 60_000
history = [
    {"role": "user", "text": "¿qué tareas tengo?", "ts": NOW - 300 * MIN, "conv": 1},
    {"role": "bot", "text": "Tenés lavar la loza.", "ts": NOW - 299 * MIN, "conv": 1},
    {"role": "bot", "text": "¡Hola! Es hora de lavar la loza (20:00–22:00).", "ts": NOW - 150 * MIN,
     "ev": "ev-task-4"},
    {"role": "bot", "text": "¿Ya lavaste la loza?", "ts": NOW - 120 * MIN},
    {"role": "bot", "text": "Pili salió de casa.", "ts": NOW - 60 * MIN, "ev": "ev-geo-3"},
    {"role": "bot", "text": "¿Te acordás de lavar la loza? Decime \"ya lo hice\" o \"no puedo\".",
     "ts": NOW - 2 * MIN, "ev": "ev-task-4"},
    {"role": "user", "text": "No puedo", "ts": NOW, "conv": 2},
]
A.load_user_history = lambda *a, **k: list(history)
block = A._event_context_block("999000111", "No puedo", now_ms=NOW + 1000)

print("what Alfred is shown")
check("the reminder it sent is above the answer", "Decime \"ya lo hice\"" in block, block)
check("  marked as its own background message, as a chore reminder",
      "background task" in block and "(chore reminder)" in block, block)
check("  newest last, three at most", block.rstrip().endswith("\"no puedo\".")
      and block.count("\n- ") == 3, block)
check("  a location alert is named for what it is", "(location alert)" in block, block)
check("  an older event without a kind still counts", "¿Ya lavaste" not in block
      or "(background message)" in block, block)
check("the conversation Alfred already saw is not repeated", "Tenés lavar la loza" not in block, block)

print("\nwhen there is nothing to add")
history.append({"role": "bot", "text": "¿Por qué no podés?", "ts": NOW + MIN, "conv": 2})
history.append({"role": "user", "text": "estoy afuera", "ts": NOW + 2 * MIN, "conv": 2})
check("after a reply in the conversation, nothing", A._event_context_block(
    "999000111", "estoy afuera", now_ms=NOW + 3 * MIN) == "")
check("inside a profession, nothing: it keeps its own history",
      A._event_context_block("999000111", "No puedo", space="teacher") == "")
old = [{"role": "bot", "text": "reminder", "ts": NOW - 10 * 3600 * 1000, "ev": "ev-task"},
       {"role": "user", "text": "hola", "ts": NOW, "conv": 3}]
A.load_user_history = lambda *a, **k: list(old)
check("a reminder from hours ago is not dragged in",
      A._event_context_block("999000111", "hola", now_ms=NOW + 1000) == "")

print("\nand it reaches the turn")
A.load_user_history = lambda *a, **k: list(history[:7])
text = A._compose_turn_content("999000111", "No puedo", [], [], None)
check("the composed turn carries the reminder above the words",
      text.find("lavar la loza") < text.find("No puedo") and "background task" in text, text[:300])

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    raise SystemExit(1)
print("all checks passed")
