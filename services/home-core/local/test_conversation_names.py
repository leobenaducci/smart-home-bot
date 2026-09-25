"""A message stamped with a conversation's name continues it.

Run: python local/test_conversation_names.py   (needs Flask; skips loudly without it)

2026-09-24: one chat, 08:27-08:57, was filed as five conversations. Silence
(04:00 -> 08:27) opened a conversation named after its first message, whose own
stamp was still the conversation before the silence; the page then stamped the
next message with the name it knew, the grouping took that as a clash and gave
the message a conversation of its own -- named after it, so the next did the
same. Each message named the previous one. The page, grouping its own view,
showed one chat; the history showed five.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

SRC = os.path.dirname(os.path.abspath(__file__))
try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-convnames-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns("__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32, DEBUG_API_KEY="d" * 32)
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write("[]")
sys.path.insert(0, dst)
import app as A  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


H = 3600_000
G = 1_790_233_200_003                      # the 04:00 greeting's conversation
DAY = [  # (ts, role, conv) -- the shape of 2026-09-24, anonymised
    (G + 23_595, "bot", G),
    (G + 4 * H + 1_600_000, "user", G + 23_595),            # 08:27, after the silence
    (G + 4 * H + 1_610_000, "bot", G + 23_595),
    (G + 4 * H + 2_900_000, "bot", G + 23_595),
    (G + 4 * H + 3_000_000, "user", G + 4 * H + 1_600_000),  # names the 08:27 message
    (G + 4 * H + 3_050_000, "bot", G + 4 * H + 1_600_000),
    (G + 4 * H + 3_140_000, "user", G + 4 * H + 3_000_000),  # names the previous one
    (G + 4 * H + 3_200_000, "bot", G + 4 * H + 3_000_000),
    (G + 4 * H + 3_300_000, "user", G + 4 * H + 3_140_000),
]
msgs = [{"ts": ts, "role": r, "conv": c, "text": f"m{i}"} for i, (ts, r, c) in enumerate(DAY)]

print("\nthe server")
segs = A._iter_sessions(msgs)
check("  the greeting, then one conversation after the silence", [len(ms) for _s, ms in segs] == [1, 8],
      [(s["start"], len(ms)) for s, ms in segs])

print("\nthe page, which must group the same way")
src = open(os.path.join(dst, "templates", "chat.html"), encoding="utf-8").read()
i = src.index("function splitConversations(")
j, d = src.index("{", i), 0
for k in range(j, len(src)):
    d += (src[k] == "{") - (src[k] == "}")
    if d == 0:
        break
js = ("const SESSION_GAP=3*60*60*1000;\n" + src[i:k + 1]
      + "\nconsole.log(JSON.stringify(splitConversations(" + json.dumps(msgs) + ").map(c => c.msgs.length)));")
try:
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    check("  the same split", out.stdout.strip() == "[1,8]", out.stdout + out.stderr)
except FileNotFoundError:
    print("  SKIP  node is not installed here")

print("\nsilence still splits")
late = msgs + [{"ts": G + 9 * H, "role": "user", "conv": G + 4 * H + 3_300_000, "text": "later"}]
check("  a message after 3h is a new conversation, whatever it is stamped",
      [len(ms) for _s, ms in A._iter_sessions(late)] == [1, 8, 1])

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("all checks passed")
