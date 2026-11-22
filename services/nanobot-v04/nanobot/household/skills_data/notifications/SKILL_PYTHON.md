```python
import json, os, subprocess

# HomeCore owns the notification store and both permission gates. The per-app
# "can reply" flag is re-checked there on every call, so this skill cannot talk
# its way into answering an app the user never allowed.
USER_ID = os.environ.get("HOMECORE_USER_ID", "")
TOKEN = os.environ.get("HOMECORE_PROXY_TOKEN", "")
HOMEWEB = os.environ.get("HOMECORE_URL") or os.environ.get(
    "TASKS_API_URL", "https://hub.home:21001/tasks/api").replace("/tasks/api", "")


def _api(method, path, data=None):
    if not USER_ID or not TOKEN:
        return {"error": "This account has no access to the phone notifications."}
    cmd = ["curl", "-sk", "--max-time", "20", "-X", method,
           "-H", f"X-Proxy-Secret: {TOKEN}", "-H", f"X-Proxy-User: {USER_ID}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    cmd.append(f"{HOMEWEB}/chat/notifications/{path}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    try:
        return json.loads(r.stdout)
    except Exception:
        return r.stdout.strip() or r.stderr.strip()


def reply_notification(id, text, **kw):
    """Answer a phone notification through its own reply action."""
    return _api("POST", "reply", {"id": int(id), "text": str(text)})


def list_notifications(limit=20, **kw):
    """Recent notifications relayed from the phone, newest first."""
    return _api("GET", f"recent?limit={int(limit)}")


def search_notifications(query, days=30, limit=20, **kw):
    """Find a relayed message by what it said or who sent it.

    Searches body, title and app together — the phone folds the sender into the
    body ("Jana 2: 17500 carne"), so a name finds that person's messages. This
    is the action for "what did X send me" / "how much was the bill"; the data
    lives here and nowhere else.
    """
    from urllib.parse import urlencode
    q = urlencode({"q": str(query), "days": int(days), "limit": int(limit)})
    return _api("GET", f"search?{q}")
```
