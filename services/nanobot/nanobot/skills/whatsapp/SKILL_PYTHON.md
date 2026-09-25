```python
import json, os, subprocess
from urllib.parse import urlencode

# HomeCore owns the message store and the per-chat gates, the same way it owns
# the phone-notification store. Read permission is re-checked there on every
# call, so this skill cannot read a chat the user muted.
USER_ID = os.environ.get("HOMECORE_USER_ID", "")
TOKEN = os.environ.get("HOMECORE_PROXY_TOKEN", "")
HOMEWEB = os.environ.get("HOMECORE_URL") or os.environ.get(
    "TASKS_API_URL", "https://hub.home:21001/tasks/api").replace("/tasks/api", "")


def _api(method, path, data=None):
    if not USER_ID or not TOKEN:
        return {"error": "This account has no WhatsApp connected."}
    cmd = ["curl", "-sk", "--max-time", "20", "-X", method,
           "-H", f"X-Proxy-Secret: {TOKEN}", "-H", f"X-Proxy-User: {USER_ID}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    cmd.append(f"{HOMEWEB}/chat/whatsapp/{path}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    try:
        return json.loads(r.stdout)
    except Exception:
        return r.stdout.strip() or r.stderr.strip()


def search_messages(query, days=90, limit=20, **kw):
    """Find a WhatsApp message by what it said, who wrote it, or which chat.

    The three are matched together on purpose: in a group the handle people
    remember is the chat's name, in a one-to-one it is the person's.
    """
    return _api("GET", "search?" + urlencode(
        {"q": str(query), "days": int(days), "limit": int(limit)}))


def list_messages(chat=None, limit=20, **kw):
    """Recent messages, newest first — from one chat, or from all of them."""
    q = {"limit": int(limit)}
    if chat:
        q["chat"] = str(chat)
    return _api("GET", "recent?" + urlencode(q))


def list_chats(**kw):
    """Every chat seen so far, with what Alfred may do with each."""
    return _api("GET", "chats")


def approve_reply(chat, **kw):
    """The user said to send the reply that was held for approval.

    Releases exactly ONE message to that chat, for a few minutes. HomeCore
    refuses if the chat is 'off', and consumes the approval as soon as
    something is sent — a second message needs a second yes.
    """
    return _api("POST", "approve", {"chat_id": str(chat)})
```
