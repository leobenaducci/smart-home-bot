"""A person can say an answer was wrong, and it stays said.

Run: python local/test_chat_feedback.py   (needs Flask; skips loudly without it)

Nothing in the chat let anybody say so. The self-improvement pass
(docs/self-improvement.md) reads a turn's failures from the runner's own
codes, and those only see what the runtime noticed: an answer that ran
cleanly and was simply wrong looks, from there, like a success. A 👎 is the
one signal that comes from the person it was wrong for.

What is pinned here is how the rating finds its answer. The page and the
server each file a reply, with their own clocks and a stream in between, so
the page's `ts` is near the stored copy's rather than equal to it -- the
rating is matched by the answer's text and the nearest time, and must land
on that answer and not on its neighbour.
"""
import json
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

tmp = tempfile.mkdtemp(prefix="homecore-feedback-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32)

USER = "999000111"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2}]' % USER)

sys.path.insert(0, dst)
import app as A  # noqa: E402

A.app.config["TESTING"] = True
client = A.app.test_client()
with client.session_transaction() as s:
    s["user"] = USER
    s["csrf_token"] = "tok"
H = {"X-CSRF-Token": "tok"}

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


DAY = "2026-09-29"
T0 = 1790700000000
MIN = 60 * 1000
for role, text, ts in (("user", "prendé la luz de Oficina Tomi", T0),
                       ("bot", "Listo, prendí **Oficina Tomi**.", T0 + 20_000),
                       ("user", "y la de Luz Paula?", T0 + 2 * MIN),
                       ("bot", "Listo, prendí Luz Paula.", T0 + 2 * MIN + 15_000)):
    A.append_user_history(USER, {"role": role, "text": text, "ts": ts}, DAY)


def rate(**body):
    return client.post("/chat/feedback", headers=H, json={"date": DAY, **body})


def stored():
    return {m["text"]: m.get("feedback") for m in A.load_user_history(USER, DAY)}


print("\na rating lands on the answer it names")
# The page saw the first answer 40 s after the server filed it.
r = rate(rating="down", ts=T0 + 60_000, text="Listo, prendí **Oficina Tomi**.",
         note="  no   prendió\nnada  ")
check("the route answers", r.status_code == 200, r.get_json())
fb = stored()
check("on the first answer, not the nearest one",
      (fb["Listo, prendí **Oficina Tomi**."] or {}).get("rating") == "down"
      and fb["Listo, prendí Luz Paula."] is None, fb)
check("with the note, one line", fb["Listo, prendí **Oficina Tomi**."].get("note") == "no prendió nada", fb)
check("and never on the person's own message", fb["prendé la luz de Oficina Tomi"] is None, fb)

print("\nchanging one's mind")
rate(rating="up", ts=T0 + 60_000, text="Listo, prendí **Oficina Tomi**.")
fb = stored()["Listo, prendí **Oficina Tomi**."]
check("👍 replaces 👎, and the note goes with it", fb.get("rating") == "up" and "note" not in fb, fb)
rate(rating="", ts=T0 + 60_000, text="Listo, prendí **Oficina Tomi**.")
check("and '' takes it back", stored()["Listo, prendí **Oficina Tomi**."] is None)

print("\nwhat is refused")
check("an unknown rating", rate(rating="meh", ts=T0, text="x").status_code == 400)
check("no time at all", rate(rating="up", text="x").status_code == 400)
r = rate(rating="up", ts=T0 + 3 * 60 * MIN, text="Listo, prendí Luz Paula.")
check("the right text hours away is somebody else's answer", r.status_code == 404, r.status_code)
r = rate(rating="up", ts=T0 + 2 * MIN + 30_000, text="")
check("no text: the nearest answer within two minutes",
      r.status_code == 200 and (stored()["Listo, prendí Luz Paula."] or {}).get("rating") == "up",
      stored())
rate(rating="down", ts=T0 + 20_000, text="Listo, prendí **Oficina Tomi**.", note="x" * 900)
check("and stored at most 500 characters",
      len(stored()["Listo, prendí **Oficina Tomi**."]["note"]) == 500)

print("\nan answer from just before midnight, rated from the next day")
A.append_user_history(USER, {"role": "bot", "text": "Buenas noches.", "ts": T0 + 10 * MIN}, "2026-09-28")
r = client.post("/chat/feedback", headers=H, json={"date": DAY, "rating": "up",
                                                   "ts": T0 + 10 * MIN, "text": "Buenas noches."})
check("is found under the day it was filed", r.status_code == 200
      and A.load_user_history(USER, "2026-09-28")[0].get("feedback", {}).get("rating") == "up", r.get_json())

print("\nthe page draws it, and brings it back")
page = open(os.path.join(SRC, "templates", "chat.html"), encoding="utf-8").read()
check("every bot answer gets the bar", "feedbackBar(div, timeSpan, textSpan, ts, opts.feedback)" in page)
check("a saved rating is repainted from the history", "feedback: m.feedback" in page)
check("the unfiled notification greeting is not rateable", "noFeedback: true" in page)
# Beside the service in the image (the deployer stages it), in the repo here.
cat = next(p for p in (os.path.join(SRC, "i18n", "en.json"),
                       os.path.join(SRC, "..", "..", "..", "i18n", "en.json"))
           if os.path.exists(p))
en = json.load(open(cat, encoding="utf-8"))
for k in ("up", "down", "ask", "send", "thanks", "failed"):
    check(f"chat.feedback.{k} has English", bool(en.get(f"chat.feedback.{k}")))

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)
