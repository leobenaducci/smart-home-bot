```python
import json, os, subprocess

# HomeCore owns every family member's identity, chat history and phone
# notifications, so an Alfred→Alfred message is posted there rather than sent
# container-to-container. Auth is the same per-user derived proxy token the
# tasks/file-share skills use: this instance can only ever act as its own user.
USER_ID = os.environ.get("HOMECORE_USER_ID", "")
TOKEN = os.environ.get("HOMECORE_PROXY_TOKEN", "")
HOMEWEB = os.environ.get("HOMECORE_URL") or os.environ.get(
    "TASKS_API_URL", "https://hub.home:21001/tasks/api").replace("/tasks/api", "")


def send_family_message(to, text, path=None, **kw):
    """Deliver a message (optionally with a file from your own share folder)
    to another family member's Alfred, and notify their phone."""
    if not USER_ID or not TOKEN:
        return {"error": "This account cannot send messages to others (HOMECORE_USER_ID/HOMECORE_PROXY_TOKEN missing)."}
    body = {"to": str(to), "text": str(text)}
    if path:
        body["path"] = str(path)
    cmd = ["curl", "-sk", "--max-time", "120", "-X", "POST",
           "-H", f"X-Proxy-Secret: {TOKEN}", "-H", f"X-Proxy-User: {USER_ID}",
           "-H", "Content-Type: application/json", "-d", json.dumps(body),
           f"{HOMEWEB}/chat/dm"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=130)
    try:
        return json.loads(r.stdout)
    except Exception:
        return r.stdout.strip() or r.stderr.strip()


def send_message_to(to, text, path=None, **kw):
    """Alias — the model sometimes reaches for this name."""
    return send_family_message(to, text, path, **kw)


def ask_family(to, question, **kw):
    """Ask another member's Alfred a question and get their answer back.

    Their Alfred answers from what only it can see, and replies. Nothing
    reaches the other person: no message in their chat, no notification.

    Timeouts are the caller's, not the server's: the exec tool running this
    dies at 60 s, so anything longer kills THIS turn instead of returning —
    and a killed turn gets retried, starting a second question on someone who
    is still answering the first. HomeCore gives up at 45 s and replies
    answered:false, which arrives in time to be relayed."""
    if not USER_ID or not TOKEN:
        return {"error": "This account cannot ask others (HOMECORE_USER_ID/HOMECORE_PROXY_TOKEN missing)."}
    body = {"to": str(to), "question": str(question)}
    cmd = ["curl", "-sk", "--max-time", "50", "-X", "POST",
           "-H", f"X-Proxy-Secret: {TOKEN}", "-H", f"X-Proxy-User: {USER_ID}",
           "-H", "Content-Type: application/json", "-d", json.dumps(body),
           f"{HOMEWEB}/chat/ask-family"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=55)
    try:
        return json.loads(r.stdout)
    except Exception:
        return r.stdout.strip() or r.stderr.strip()
```
