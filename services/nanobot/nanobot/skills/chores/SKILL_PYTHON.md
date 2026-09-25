```python
import subprocess, json, os

BASE = os.environ.get("TASKS_API_URL", "https://hub.home:21001/tasks/api")
USER_ID = os.environ.get("HOMECORE_USER_ID", "")
TOKEN = os.environ.get("HOMECORE_PROXY_TOKEN", "")

FAMILY = {"user3": "user3", "user1": "user1", "user2": "user2", "user4": "user4"}

def _uid(who):
    return FAMILY.get(str(who).strip().lower(), str(who).strip())

def _curl(method, path, data=None):
    if not USER_ID or not TOKEN:
        return {"error": "This account has no access to Tasks (HOMECORE_USER_ID/HOMECORE_PROXY_TOKEN missing)."}
    cmd = ["curl", "-sk", "--max-time", "10", "-X", method,
           "-H", f"X-Proxy-Secret: {TOKEN}", "-H", f"X-Proxy-User: {USER_ID}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    cmd.append(f"{BASE}/{path}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    try: return json.loads(r.stdout)
    except Exception: return r.stdout.strip() or r.stderr.strip()

def list_chores(scope="today", user=None):
    # user: nothing = yours; a first name = that person's; "all" = the whole house.
    # The answer carries `scope_note`, so an empty list says which filter emptied
    # it. Without that the model reads "0 rows" as "nobody did it" -- on 11 Aug
    # 2026 it told the house nobody had cleaned the litter tray when somebody
    # had, because the chore was another member's and the list defaulted to
    # the asker's own. Explaining that in the prompt cost ~700 tokens a turn;
    # the answer explaining itself costs nothing.
    path = f"list?scope={scope}"
    who = "you"
    if user:
        who = "all" if str(user).lower() in ("all", "todos", "familia") else _uid(user)
        path += f"&user={who}"
    out = _curl("GET", path)
    if isinstance(out, dict):
        out["scope_note"] = (
            f"showed {'every member' if who == 'all' else who}'s chores for "
            f"'{scope}'. Empty means none matched THIS filter, not that nobody "
            f"did it. 'today' and 'week' both exclude a finished chore from a "
            f"past day -- use scope='all' for anything before today. Pass "
            f"user='all' for a household question, or a member id for one person.")
    return out
def complete_chore(task_id): return _curl("POST", "complete", {"task_id": int(task_id)})
def revert_chore(task_id): return _curl("POST", "revert", {"task_id": int(task_id)})
def excuse_chore(task_id, reason): return _curl("POST", "excuse", {"task_id": int(task_id), "note": str(reason)})
def excuse_day(user, reason, date=None):
    body = {"user": _uid(user), "note": str(reason)}
    if date: body["date"] = date
    return _curl("POST", "excuse-day", body)
def postpone_chore(task_id, minutes=60): return _curl("POST", "postpone", {"task_id": int(task_id), "minutes": int(minutes)})
# The penalty rules, returned with the balance rather than restated in every
# prompt. "Why were points taken off me?" arrives verbatim from a notification,
# so the answer needs the rules in hand; lines in the history starting
# "Not done:" are the penalty for a chore left undone.
_PENALTY_RULES = [
    "A chore left undone costs half what it was worth, rounded down: one worth "
    "10 costs 5, one worth 1 costs nothing. Doing it always beats not doing it.",
    "The only way to avoid it is a parent accepting the excuse. Use excuse_chore "
    "with the reason BEFORE it closes itself; an excuse nobody reviews is "
    "charged anyway after two days.",
    "If a parent accepts it afterwards the points come back, shown as "
    "'Penalty returned'. Same if they grant the points anyway.",
    "The balance never goes below 0: if it is short, only what was there is taken.",
    "Parents decide reversals. Explain the reason and offer to tell them; never "
    "promise to undo a deduction.",
]

def my_points():
    out = _curl("GET", "points")
    if isinstance(out, dict):
        out["penalty_rules"] = _PENALTY_RULES
    return out
def list_prizes(): return _curl("GET", "prizes")
def redeem_prize(prize_id): return _curl("POST", "redeem", {"prize_id": int(prize_id)})
def list_redemptions(): return _curl("GET", "redemptions")

def add_chore(title, points, assignee, due_date=None, time_start=None, time_end=None, remind_before=None, remind_every=None, icon=None, description=None):
    body = {"title": title, "points": int(points), "assignee": _uid(assignee)}
    if due_date: body["due_date"] = due_date
    if time_start: body["time_start"] = time_start
    if time_end: body["time_end"] = time_end
    if remind_before is not None: body["remind_before"] = int(remind_before)
    if remind_every: body["remind_every"] = int(remind_every)
    if icon: body["icon"] = icon
    if description: body["description"] = description
    return _curl("POST", "tasks", body)

def edit_chore(task_id, **fields):
    if "assignee" in fields: fields["assignee"] = _uid(fields["assignee"])
    return _curl("PUT", f"tasks/{int(task_id)}", fields)

def delete_chore(task_id, confirm=False):
    # Without confirm the server does NOT delete — it returns needs_confirm so
    # you can ask the user first. Pass confirm=True only after they say yes.
    path = f"tasks/{int(task_id)}"
    if confirm: path += "?confirm=1"
    return _curl("DELETE", path)

def add_recurring_chore(title, points, assignees, weekdays, rotate=False, time_start=None, time_end=None, remind_before=None, remind_every=None, icon=None):
    body = {"title": title, "points": int(points), "assignees": [_uid(a) for a in assignees],
            "weekdays": [int(w) for w in weekdays], "rotate": bool(rotate)}
    if time_start: body["time_start"] = time_start
    if time_end: body["time_end"] = time_end
    if remind_before is not None: body["remind_before"] = int(remind_before)
    if remind_every: body["remind_every"] = int(remind_every)
    if icon: body["icon"] = icon
    return _curl("POST", "templates", body)

def list_recurring_chores(): return _curl("GET", "templates")

def edit_recurring_chore(template_id, **fields):
    if "assignees" in fields: fields["assignees"] = [_uid(a) for a in fields["assignees"]]
    if "weekdays" in fields: fields["weekdays"] = [int(w) for w in fields["weekdays"]]
    return _curl("PUT", f"templates/{int(template_id)}", fields)

def review_queue(): return _curl("GET", "review-queue")
def approve_chore(task_id): return _curl("POST", "approve", {"task_id": int(task_id)})
def reject_chore(task_id, note=""): return _curl("POST", "reject", {"task_id": int(task_id), "note": str(note)})

def add_prize(name, cost_points, icon=None, description=None):
    body = {"name": name, "cost_points": int(cost_points)}
    if icon: body["icon"] = icon
    if description: body["description"] = description
    return _curl("POST", "prizes", body)

def fulfill_redemption(redemption_id): return _curl("POST", f"redemptions/{int(redemption_id)}/fulfill", {})
def cancel_redemption(redemption_id): return _curl("POST", f"redemptions/{int(redemption_id)}/cancel", {})
def adjust_points(user, delta, reason): return _curl("POST", "adjust", {"user": _uid(user), "delta": int(delta), "reason": str(reason)})
```

Call the function matching the `action` field. Print the result with `print(json.dumps(result, indent=2, ensure_ascii=False))`.
