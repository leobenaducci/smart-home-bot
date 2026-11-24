```python
import subprocess, json, os
from urllib.parse import urlencode

# Same HomeCore host/creds as the chores skill; just a different path.
BASE = os.environ.get("TASKS_API_URL", "https://hub.home:21001/tasks/api").replace("/tasks/api", "/family/api")
USER_ID = os.environ.get("HOMECORE_USER_ID", "")
TOKEN = os.environ.get("HOMECORE_PROXY_TOKEN", "")


def _curl(method, path, data=None, params=None):
    if not USER_ID or not TOKEN:
        return {"error": "This account has no access to the family directory (HOMECORE_USER_ID/HOMECORE_PROXY_TOKEN missing)."}
    url = f"{BASE}/{path}"
    if params:
        url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
    cmd = ["curl", "-sk", "--max-time", "10", "-X", method,
           "-H", f"X-Proxy-Secret: {TOKEN}", "-H", f"X-Proxy-User: {USER_ID}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    try:
        return json.loads(r.stdout)
    except Exception:
        return r.stdout.strip() or r.stderr.strip()


def list_family():
    return _curl("GET", "list")


def get_profile(person=None, user=None):
    return _curl("GET", "profile", params={"person": person or user})


def search_family(query=None, q=None):
    return _curl("GET", "search", params={"q": query or q})


def set_fact(person, label=None, value=None, key=None):
    body = {"person": str(person), "label": str(label or key or ""), "value": str(value or "")}
    return _curl("POST", "set-fact", body)


def add_note(person, note):
    return _curl("POST", "add-note", {"person": str(person), "note": str(note)})


def set_profile(person, full_name=None, relationship=None, birthdate=None, timezone=None):
    body = {"person": str(person)}
    if full_name is not None: body["full_name"] = str(full_name)
    if relationship is not None: body["relationship"] = str(relationship)
    if birthdate is not None: body["birthdate"] = str(birthdate)
    if timezone is not None: body["timezone"] = str(timezone)
    return _curl("POST", "set-profile", body)


def remove_fact(person, label=None, id=None, key=None):
    body = {"person": str(person)}
    if id is not None: body["id"] = int(id)
    else: body["label"] = str(label or key or "")
    return _curl("POST", "remove-fact", body)


def remove_note(id, person=None):
    body = {"id": int(id)}
    if person is not None: body["person"] = str(person)
    return _curl("POST", "remove-note", body)
```

Call the function matching the `action` field. Print the result with `print(json.dumps(result, indent=2, ensure_ascii=False))`.
