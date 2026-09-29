```python
import json, os, subprocess

# HomeCore files the request and answers with the card; auth is this
# instance's own derived proxy token, so a request is always its own user's.
USER_ID = os.environ.get("HOMECORE_USER_ID", "")
TOKEN = os.environ.get("HOMECORE_PROXY_TOKEN", "")
HOMEWEB = os.environ.get("HOMECORE_URL") or os.environ.get(
    "TASKS_API_URL", "").replace("/tasks/api", "")


def _call(method, path, body=None, timeout=30):
    if not USER_ID or not TOKEN or not HOMEWEB:
        return {"error": "This assistant has no HomeCore account, so it cannot file a fix."}
    cmd = ["curl", "-sk", "--max-time", str(timeout), "-X", method,
           "-H", f"X-Proxy-Secret: {TOKEN}", "-H", f"X-Proxy-User: {USER_ID}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    r = subprocess.run(cmd + [f"{HOMEWEB}{path}"], capture_output=True, text=True,
                       timeout=timeout + 5)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"error": (r.stdout or r.stderr or "no answer").strip()[:300]}


def request_fix(problem, context="", **kw):
    """File a fix to Alfred himself; returns the card that opens the Programmer on it."""
    return _call("POST", "/improve/api/requests",
                 {"problem": str(problem), "context": str(context or "")})


def publish_fix(id=None, deploy=False, **kw):
    """Tell a fix request's Programmer conversation to publish (and deploy) it."""
    body = {"deploy": bool(deploy) and str(deploy).lower() not in ("false", "0", "no")}
    if id not in (None, ""):
        body["id"] = int(str(id).lstrip("#"))
    return _call("POST", "/improve/api/requests/publish", body, timeout=40)


def list_fix_requests(**kw):
    """The person's last fix requests, newest first."""
    return _call("GET", "/improve/api/requests")
```
