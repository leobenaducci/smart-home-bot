```python
import subprocess, json, os

# Same HomeCore host/creds as the chores skill; just a different path.
BASE = os.environ.get("TASKS_API_URL", "https://hub.home:21001/tasks/api").replace("/tasks/api", "/grocery/api")
USER_ID = os.environ.get("HOMECORE_USER_ID", "")
TOKEN = os.environ.get("HOMECORE_PROXY_TOKEN", "")

FAMILY = {"user3": "user3", "user1": "user1", "user2": "user2", "user4": "user4"}

def _uid(who):
    return FAMILY.get(str(who).strip().lower(), str(who).strip())

def _curl(method, path, data=None):
    if not USER_ID or not TOKEN:
        return {"error": "This account has no access to the shopping list (HOMECORE_USER_ID/HOMECORE_PROXY_TOKEN missing)."}
    cmd = ["curl", "-sk", "--max-time", "10", "-X", method,
           "-H", f"X-Proxy-Secret: {TOKEN}", "-H", f"X-Proxy-User: {USER_ID}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    cmd.append(f"{BASE}/{path}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    try: return json.loads(r.stdout)
    except Exception: return r.stdout.strip() or r.stderr.strip()

def list_groceries():
    return _curl("GET", "list")

def add_grocery(name, qty=None, category=None, note=None, request=False, requester=None):
    body = {"name": str(name)}
    if qty: body["qty"] = str(qty)
    if category: body["category"] = str(category)
    if note: body["note"] = str(note)
    if request: body["request"] = True
    if requester: body["requester"] = _uid(requester)
    return _curl("POST", "add", body)

def request_grocery(name, qty=None, category=None, note=None, requester=None):
    return add_grocery(name, qty=qty, category=category, note=note, request=True, requester=requester)

def mark_bought(name=None, item_id=None):
    body = {"bought": True}
    if item_id is not None: body["id"] = int(item_id)
    elif name: body["name"] = str(name)
    return _curl("POST", "toggle", body)

def remove_grocery(name=None, item_id=None):
    body = {}
    if item_id is not None: body["id"] = int(item_id)
    elif name: body["name"] = str(name)
    return _curl("POST", "remove", body)

def approve_grocery(item_id):
    return _curl("POST", "approve", {"id": int(item_id)})

def reject_grocery(item_id, note=None):
    body = {"id": int(item_id)}
    if note: body["note"] = str(note)
    return _curl("POST", "reject", body)

def clear_bought():
    return _curl("POST", "clear-bought", {})
```

Call the function matching the `action` field. Print the result with `print(json.dumps(result, indent=2, ensure_ascii=False))`.
