# Python Translation Guide

```python
import subprocess, json, os

# Same HomeCore host and per-user credentials as the other skills; just a
# different path. The token is derived per user, so this can only ever ask on
# behalf of the person whose container it runs in.
BASE = os.environ.get("TASKS_API_URL", "https://hub.home:21001/tasks/api").replace("/tasks/api", "/chat/api")
USER_ID = os.environ.get("HOMECORE_USER_ID", "")
TOKEN = os.environ.get("HOMECORE_PROXY_TOKEN", "")

# HomeCore waits up to 45 s for the other Alfred and then answers `pending`, so
# this call returns well inside the exec tool's own 60 s ceiling. Waiting longer
# here would kill the caller's turn instead of the request.
_TIMEOUT_S = 55


def delegate(to, brief, **kwargs):
    """Ask another profession for a piece and return its answer."""
    if not USER_ID or not TOKEN:
        return {"error": "This account cannot delegate (HOMECORE_USER_ID/HOMECORE_PROXY_TOKEN missing)."}
    if not to or not brief:
        return {"error": "delegate needs `to` (the profession) and `brief` (the commission)."}
    body = {"to": str(to), "brief": str(brief)}
    origin = kwargs.get("from") or kwargs.get("from_") or kwargs.get("origin")
    if origin:
        body["from"] = str(origin)
    cmd = ["curl", "-sk", "--max-time", str(_TIMEOUT_S), "-X", "POST",
           "-H", f"X-Proxy-Secret: {TOKEN}", "-H", f"X-Proxy-User: {USER_ID}",
           "-H", "Content-Type: application/json", "-d", json.dumps(body),
           f"{BASE}/delegate"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT_S + 5)
    except subprocess.TimeoutExpired:
        return {"error": "The other Alfred did not answer in time. Carry on with your own work."}
    try:
        return json.loads(r.stdout)
    except Exception:
        return r.stdout.strip() or r.stderr.strip() or {"error": "no answer"}
```

`delegate` is the only action. `to` is one of `programmer`, `teacher` or
`designer`; `from` is the caller's own profession and is optional.

Returns `{"ok": true, "from": "...", "answer": "..."}` when the other Alfred
finished in time, `{"ok": true, "pending": true, "from": "..."}` when it is
still working (the result arrives in this conversation by itself), or
`{"error": "..."}`. A 409 means a delegation is already running for this
account — answer with what you have rather than retrying.
