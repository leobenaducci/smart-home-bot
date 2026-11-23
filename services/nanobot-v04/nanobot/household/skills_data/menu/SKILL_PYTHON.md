```python
import subprocess, json, os

# Same HomeCore host/creds as the tasks and grocery skills; just a different path.
BASE = os.environ.get("TASKS_API_URL", "https://hub.home:21001/tasks/api").replace("/tasks/api", "/menu/api")
USER_ID = os.environ.get("HOMECORE_USER_ID", "")
TOKEN = os.environ.get("HOMECORE_PROXY_TOKEN", "")

FAMILY = {"user3": "user3", "user1": "user1", "user2": "user2", "user4": "user4"}

def _uid(who):
    return FAMILY.get(str(who).strip().lower(), str(who).strip())

def _curl(method, path, data=None):
    if not USER_ID or not TOKEN:
        return {"error": "This account has no access to the weekly menu (HOMECORE_USER_ID/HOMECORE_PROXY_TOKEN missing)."}
    cmd = ["curl", "-sk", "--max-time", "10", "-X", method,
           "-H", f"X-Proxy-Secret: {TOKEN}", "-H", f"X-Proxy-User: {USER_ID}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    cmd.append(f"{BASE}/{path}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    try: return json.loads(r.stdout)
    except Exception: return r.stdout.strip() or r.stderr.strip()

def list_menu(week=None, **kw):
    """The week's lunches and dinners, its pending requests and repeat dishes."""
    return _curl("GET", f"list?week={week}" if week else "list")

def set_menu(dish, day=None, meal=None, note=None, week=None, request=False, requester=None, **kw):
    """Put a dish on the menu (parents), or ask for one (everyone else — the
    server decides which, based on who is calling)."""
    body = {"dish": str(dish)}
    if day: body["day"] = str(day)
    if meal: body["meal"] = str(meal)
    if note: body["note"] = str(note)
    if week: body["week"] = str(week)
    if request: body["request"] = True
    if requester: body["requester"] = _uid(requester)
    return _curl("POST", "set", body)

def request_dish(dish, day=None, meal=None, note=None, week=None, requester=None, **kw):
    return set_menu(dish, day=day, meal=meal, note=note, week=week,
                    request=True, requester=requester)

def clear_menu(day, meal, **kw):
    return _curl("POST", "clear", {"day": str(day), "meal": str(meal)})

def approve_dish(entry_id=None, day=None, meal=None, id=None, **kw):
    body = {"id": entry_id if entry_id is not None else id}
    if day: body["day"] = str(day)
    if meal: body["meal"] = str(meal)
    return _curl("POST", "approve", body)

def reject_dish(entry_id=None, note=None, id=None, **kw):
    body = {"id": entry_id if entry_id is not None else id}
    if note: body["note"] = str(note)
    return _curl("POST", "reject", body)

def remove_menu_entry(entry_id=None, id=None, **kw):
    return _curl("POST", "remove", {"id": entry_id if entry_id is not None else id})
```

Call the function matching the `action` field. Print the result with `print(json.dumps(result, indent=2))`.
