"""Laya as a first pass over notification triage (_laya_decide, _notif_deliver).

Run: python local/test_laya_triage.py   (needs Flask; skips loudly without it)

What must hold, in the order it would hurt:
  * off is off -- no call, triage exactly as before;
  * a Laya that is down, slow or answers nonsense changes nothing but the
    record: the model still decides;
  * shadow records Laya's answer beside the model's and obeys only the model;
  * cascade skips the model only when Laya is sure it is nothing AND the app
    cannot be replied to -- a reply is never Laya's to skip.
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

tmp = tempfile.mkdtemp(prefix="homecore-laya-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32, DEBUG_API_KEY="d" * 32)
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "user1", "nanobot_id": 2}]')
sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_notif_db()
failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


calls, model_calls = [], []


class Resp:
    def __init__(self, doc):
        self.doc = doc

    def raise_for_status(self):
        pass

    def json(self):
        return self.doc


def laya_says(p):
    def post(url, **kw):
        calls.append((url, kw))
        if p == "down":
            raise OSError("connection refused")
        # The shape laya 0.3.11 answers with, measured.
        return Resp({"model": "laya-multilingual", "answers": {"tell": {
            "type": "noul", "noul": p, "confidence": p, "action": {"act_probability": 1.0}}}})
    return post


A._alfred_notify = lambda u, prompt, **kw: (model_calls.append(prompt), "SILENCE")[1]
A._user_watching = lambda u: True


def rows():
    c = A._notif_conn()
    try:
        return c.execute("SELECT verdict, decided_by, laya_p FROM notif_triage ORDER BY id").fetchall()
    finally:
        c.close()


def run(mode, p, can_reply=False, threshold="0.1"):
    calls.clear(); model_calls.clear()
    os.environ.update(LAYA_MODE=mode, LAYA_URL="http://laya:8000", LAYA_THRESHOLD=threshold)
    A.requests.post = laya_says(p)
    A._notif_deliver("user1", 1, "WhatsApp", "Grupo", "jaja", can_reply, "avisame si el colegio escribe")
    return rows()[-1]


print("\noff is off")
r = run("off", 0.01)
check("  Laya is not called, the model decides", not calls and model_calls and r == ("silent", "model", None), r)

print("\nshadow: recorded, never obeyed")
r = run("shadow", 0.02)
check("  both asked; the model's verdict stands", calls and model_calls and r[:2] == ("silent", "model"), r)
check("  Laya's probability kept beside it", abs(r[2] - 0.02) < 1e-9, r)
check("  the rules travel with the notification",
      calls[0][1]["json"]["state"]["rules"] == "avisame si el colegio escribe")

print("\ncascade")
r = run("cascade", 0.03)
check("  sure it is nothing: silent, and the model is not asked", not model_calls and r[:2] == ("silent-laya", "laya"), r)
r = run("cascade", 0.4)
check("  not sure: the model decides", model_calls and r[:2] == ("silent", "model"), r)
r = run("cascade", 0.01, can_reply=True)
check("  an app it could reply to always goes to the model", model_calls and r[1] == "model", r)

print("\na Laya that is down or wrong changes nothing")
r = run("cascade", "down")
check("  down: the model decides, nothing recorded for Laya", model_calls and r == ("silent", "model", None), r)
r = run("cascade", 7)
check("  an impossible probability is ignored", model_calls and r[2] is None, r)
os.environ["LAYA_URL"] = ""
calls.clear(); model_calls.clear()
A._notif_deliver("user1", 1, "WhatsApp", "G", "x", False, "")
check("  a mode with no URL is off", not calls and model_calls)

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("all checks passed")
