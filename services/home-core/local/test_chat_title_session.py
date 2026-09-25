"""The chat titler: local by default, and it must not think.

Run: python3 test_chat_title_session.py   (from services/home-core/local)

OpenCode's mail of 2026-09-03: requests missing `x-opencode-session` "may
error" from 09/06, and it wants one stable id per conversation. This caller is
the rare one that has a real answer to that -- it is titling one specific chat,
and `chat_titles.session_id` names it and does not change as the chat grows.

The other half is that the header stays off the Ollama fallback, which has no
use for it: the titler swings to a local model whenever there is no key, and
that path must not start carrying an OpenCode id around.
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

tmp = tempfile.mkdtemp(prefix="homecore-title-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32)
# The default is what this file checks, not the household's choice: since
# 2026-09-10 the deployer exports CHAT_TITLE_MODEL from `assistant.models
# .titles`, and a run inside a configured container would otherwise assert
# that the house's pick equals the code's fallback.
for _v in ("CHAT_TITLE_MODEL", "CHAT_TITLE_URL", "CHAT_TITLE_KEY"):
    os.environ.pop(_v, None)
sys.path.insert(0, dst)

import app  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label
          + ("" if cond else f"   <- {detail!r}"))
    if not cond:
        failures.append(label)


class _Reply:
    ok = True

    @staticmethod
    def json():
        return {"choices": [{"message": {"content": "Una charla"}}]}


def sent_headers(key, session_id, url=None):
    """The headers _generate_title would send, without leaving the box."""
    seen = {}

    def spy(url, json=None, headers=None, timeout=None):
        seen.clear()
        seen.update(headers or {})
        seen["__url__"] = url
        return _Reply()

    real_post, real_key = app.requests.post, app.CHAT_TITLE_KEY
    real_url = app.CHAT_TITLE_URL
    app.requests.post = spy
    app.CHAT_TITLE_KEY = key
    app.CHAT_TITLE_URL = url or ("https://opencode.ai/zen/v1/chat/completions"
                                 if key
                                 else "http://127.0.0.1:11434/v1/chat/completions")
    try:
        app._generate_title("someone", [{"role": "user", "text": "hola"}],
                            session_id)
    finally:
        app.requests.post, app.CHAT_TITLE_KEY = real_post, real_key
        app.CHAT_TITLE_URL = real_url
    return seen


print("by default it does not leave the house")
check("  it talks to the local Ollama",
      '127.0.0.1:11434' in app.CHAT_TITLE_URL or 'ollama' in app.CHAT_TITLE_URL.lower(),
      app.CHAT_TITLE_URL)
check("  and never to OpenCode",
      'opencode.ai' not in app.CHAT_TITLE_URL, app.CHAT_TITLE_URL)
# The point is co-residency, not the digits: the titler must reuse whatever
# model `notifications` and `events` already keep loaded, because a second
# title-sized model on a 12 GB card evicts something that is doing work. The
# name is written out so a change has to be deliberate -- if those roles move,
# this moves with them, and this check is where somebody is told.
check("  on the model the house already keeps resident",
      app.CHAT_TITLE_MODEL == 'ornith-1.5:9b', app.CHAT_TITLE_MODEL)
check("  which is a local model, not a hosted one",
      ':' in app.CHAT_TITLE_MODEL and '/' not in app.CHAT_TITLE_MODEL,
      app.CHAT_TITLE_MODEL)
check("  with no key inherited from OpenCode",
      app.CHAT_TITLE_KEY == '' or app.CHAT_TITLE_KEY != os.environ.get('OPENCODE_API_KEY', 'x'),
      bool(app.CHAT_TITLE_KEY))

print("\nand it does not think, or the answer comes back empty")
# Measured: qwen3.5:9b with max_tokens=24 and no reasoning_effort returns
# content='' after 6.8s. Nothing errors. The conversation just never gets named.
_body = {}
def _spy_post(url, json=None, headers=None, timeout=None):
    _body.clear(); _body.update(json or {})
    return _Reply()
_real = app.requests.post
app.requests.post = _spy_post
try:
    app._generate_title('someone', [{'role': 'user', 'text': 'hola'}], 's1')
finally:
    app.requests.post = _real
check("  reasoning_effort is sent", _body.get('reasoning_effort') == 'none', _body.get('reasoning_effort'))
check("  and the budget it protects is still small", _body.get('max_tokens') == 24)

print("\ntitling a chat through an explicitly configured OpenCode endpoint")
h = sent_headers("a-key", "2026-09-03:1:dev")
check("it sends x-opencode-session", "x-opencode-session" in h, sorted(h))
check("  and it is this conversation's own id",
      h.get("x-opencode-session") == "2026-09-03:1:dev", h.get("x-opencode-session"))
check("  alongside the authorization it always sent",
      h.get("Authorization") == "Bearer a-key")
check("  at Zen, never the flat plan",
      h.get("__url__", "").startswith("https://opencode.ai/zen/v1"), h.get("__url__"))

print("\nthe same chat titled twice sends the same id")
check("  stable across calls",
      sent_headers("a-key", "2026-09-03:1:dev").get("x-opencode-session")
      == h.get("x-opencode-session"))
check("  and two chats do not share one",
      sent_headers("a-key", "2026-09-03:2").get("x-opencode-session")
      != h.get("x-opencode-session"))

print("\nbut the local fallback is left alone")
local = sent_headers("", "2026-09-03:1:dev")
check("  no opencode id goes to Ollama", "x-opencode-session" not in local, sorted(local))
check("  and no authorization either", "Authorization" not in local)

print("\nand another provider on a key of its own is left alone too")
# CHAT_TITLE_URL is configurable and CHAT_TITLE_KEY defaults to
# OPENCODE_API_KEY, so "there is a key" is not the same question as "this is
# OpenCode" -- a household pointing the titler at Together must not have its
# requests tagged with an OpenCode session id.
elsewhere = sent_headers("a-key", "2026-09-03:1:dev",
                         "https://api.together.xyz/v1/chat/completions")
check("  no opencode id goes to together",
      "x-opencode-session" not in elsewhere, sorted(elsewhere))
check("  but the authorization still does",
      elsewhere.get("Authorization") == "Bearer a-key")

print("\nand a titler with no session id still works")
none = sent_headers("a-key", "")
check("  the call is still made", none.get("__url__", "") != "")
check("  without an empty header", "x-opencode-session" not in none, sorted(none))

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")
