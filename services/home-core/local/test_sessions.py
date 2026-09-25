"""Exercise _iter_sessions — where one conversation ends and the next begins.
Run: python local/test_sessions.py

The rule lives in two places on purpose (server and chat.html) and has been
wrong twice: once because silence was the only boundary, and once because a
message that named its own conversation was adopted into an unstamped run
instead of breaking out of it — which is how the 6 AM scheduled greeting ended
up tacked onto the machine chatter above it rather than opening a chat of its
own. Importing app.py needs Flask and the rest of the deployed image, so the
function is lifted out by AST, the same way test_dirsize.py does it.
"""
import ast
import sys
from pathlib import Path

APP = Path(__file__).with_name("app.py")

GAP = 3 * 60 * 60 * 1000
MIN = 60 * 1000


def load_iter_sessions():
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_iter_sessions")
    ns = {"CHAT_SESSION_GAP_MS": GAP}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "app.py", "exec"), ns)
    return ns["_iter_sessions"]


_iter_sessions = load_iter_sessions()


def msg(ts, role="user", conv=None, text="x"):
    m = {"ts": ts, "role": role, "text": text}
    if conv is not None:
        m["conv"] = conv
    return m


def starts(msgs):
    return [seg["start"] for seg, _ in _iter_sessions(msgs)]


failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + detail}")
    if not cond:
        failures.append(label)


T0 = 1785700000000

print("silence still splits")
s = starts([msg(T0), msg(T0 + MIN), msg(T0 + MIN + GAP + 1)])
check("two conversations", s == [T0, T0 + MIN + GAP + 1], str(s))

print("\na stamped run continues")
s = starts([msg(T0, conv=T0), msg(T0 + MIN, conv=T0), msg(T0 + 2 * MIN, conv=T0)])
check("one conversation", s == [T0], str(s))

print("\n'Nueva conversación' with no gap")
s = starts([msg(T0, conv=T0), msg(T0 + MIN, conv=T0 + MIN)])
check("splits on the new id", s == [T0, T0 + MIN], str(s))

print("\nunstamped machine events, then a scheduled message of its own")
# The 6 AM greeting: the day so far holds only ev-* replies, which are stored
# unstamped so they inherit the conversation on screen. The greeting names a
# conversation, and must open it instead of being adopted into the run above.
events = [msg(T0, role="bot", text="[HomeCore system] notificación"),
          msg(T0 + MIN, role="bot", text="respuesta al evento")]
greet = msg(T0 + 2 * MIN, role="bot", conv=T0 + 2 * MIN, text="¡Buenos días Alex!")
s = starts(events + [greet])
check("the greeting opens its own conversation", s == [T0, T0 + 2 * MIN], str(s))

print("\nthe reply to that greeting stays with it")
reply = msg(T0 + 3 * MIN, conv=T0 + 2 * MIN, text="gracias")
segs = _iter_sessions(events + [greet, reply])
check("still two conversations", [g["start"] for g, _ in segs] == [T0, T0 + 2 * MIN],
      str([g["start"] for g, _ in segs]))
check("the reply is in the greeting's", len(segs[-1][1]) == 2, str(segs[-1][1]))

print("\nmigration: unstamped history, then the first stamped message")
# _conv_resolve derives the conversation from the run's own start, so a message
# continuing it carries that value and must NOT split (this is the case the
# adopt branch exists for).
s = starts([msg(T0), msg(T0 + MIN), msg(T0 + 2 * MIN, conv=T0)])
check("no boundary at the migration line", s == [T0], str(s))

print("\nan unstamped event inside a stamped conversation")
s = starts([msg(T0, conv=T0),
            msg(T0 + MIN, role="bot", text="[HomeCore system] geo"),
            msg(T0 + 2 * MIN, conv=T0)])
check("inherits the conversation on screen", s == [T0], str(s))

print("\na pinned conversation picked up hours later")
# Same stamp, 3h of silence: the gap wins, and the second entry must not reuse
# the id — /chat/sessions publishes start as the lookup key.
s = starts([msg(T0, conv=T0), msg(T0 + GAP + MIN, conv=T0)])
check("two unique ids", s == [T0, T0 + GAP + MIN], str(s))

print("\nno history at all")
check("empty in, empty out", starts([]) == [], "")

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
