```python
import subprocess, json, os
from urllib.parse import urlencode

# Same HomeCore host/creds as the chores skill; just a different path.
BASE = os.environ.get("TASKS_API_URL", "https://hub.home:21001/tasks/api").replace("/tasks/api", "/geo/api")
USER_ID = os.environ.get("HOMECORE_USER_ID", "")
TOKEN = os.environ.get("HOMECORE_PROXY_TOKEN", "")

FAMILY = {"user3": "user3", "user1": "user1", "user2": "user2", "user4": "user4"}

def _uid(who):
    return FAMILY.get(str(who).strip().lower(), str(who).strip())

def _curl(method, path, data=None, params=None):
    if not USER_ID or not TOKEN:
        return {"error": "This account has no access to Places (HOMECORE_USER_ID/HOMECORE_PROXY_TOKEN missing)."}
    url = f"{BASE}/{path}"
    if params:
        url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
    cmd = ["curl", "-sk", "--max-time", "10", "-X", method,
           "-H", f"X-Proxy-Secret: {TOKEN}", "-H", f"X-Proxy-User: {USER_ID}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    try: return json.loads(r.stdout)
    except Exception: return r.stdout.strip() or r.stderr.strip()

def _map_md(res):
    # Markdown for the map image of a GPS result. HomeCore renders the map
    # itself (/chat/map composites the tiles server-side), so the family's
    # coordinates never reach a tile provider from anyone's phone.
    # Only for results that carry real coordinates — where_is has none.
    try:
        lat, lng = float(res["lat"]), float(res["lng"])
    except (TypeError, KeyError, ValueError):
        return None
    q = {"lat": f"{lat:.6f}", "lng": f"{lng:.6f}"}
    label = res.get("place") or res.get("name")
    if label:
        q["label"] = str(label)
    acc = res.get("acc")
    if acc:
        try: q["acc"] = int(float(acc))
        except (TypeError, ValueError): pass
    return "![map](/chat/map?" + urlencode(q) + ")"

# Both notes used to be prose in SKILL.md, ~330 tokens in every prompt whether
# or not anybody asked where somebody was. The answer carries them instead.
_MAP_NOTE = ("Paste the `map` string verbatim on its own line, AFTER saying where "
             "they are and since when. Never edit it or build the URL yourself. "
             "No `map` field means there are no coordinates -- do not invent one.")
_STALE_NOTE = ("This fix is {h} old, so the phone is not reporting -- location "
               "permission or battery. Say that plainly in one line. It does NOT "
               "mean they have been there that long.")

def _with_map(res):
    # track_status has no `found` key; it just carries lat/lng when a fix exists.
    if isinstance(res, dict) and res.get("found") is not False and "lat" in res:
        md = _map_md(res)
        if md:
            res["map"] = md
            res["map_note"] = _MAP_NOTE
    if isinstance(res, dict):
        try:
            age = float(res.get("age_s") or 0)
        except (TypeError, ValueError):
            age = 0
        if age >= 3600:
            h = f"{age/86400:.0f} day(s)" if age >= 86400 else f"{age/3600:.0f} hour(s)"
            res["age_note"] = _STALE_NOTE.format(h=h)
    return res

def where_is(person=None, user=None):
    # Coarse last-known place of a family member (or everyone if omitted).
    who = person or user
    return _curl("GET", "whereabouts", params={"user": who} if who else None)

def save_place(name, lat, lng, radius=None):
    body = {"name": str(name), "lat": float(lat), "lng": float(lng)}
    if radius is not None: body["radius"] = int(radius)
    return _curl("POST", "places", body)

def list_places():
    return _curl("GET", "places")

def delete_place(place_id):
    return _curl("DELETE", f"places/{int(place_id)}")

def add_reminder(text, place, target_user=None, direction="enter", notify_user=None, recurring=False):
    body = {"text": str(text), "place": place,
            "direction": ("exit" if str(direction).lower() == "exit" else "enter")}
    if target_user: body["target_user"] = _uid(target_user)
    if notify_user: body["notify_user"] = _uid(notify_user)
    if recurring: body["recurring"] = True
    return _curl("POST", "reminders", body)

def list_reminders():
    return _curl("GET", "reminders")

def cancel_reminder(reminder_id):
    return _curl("DELETE", f"reminders/{int(reminder_id)}")

def get_location(target_user=None):
    # Last known location of a person (self, or another — admin only).
    # Returns {found, lat, lng, acc?, ts, age_s, place} (place = saved lugar it
    # falls inside, or null). 404 {found:false} if none stored yet.
    # `map` is the markdown for the map image — paste it as-is in the reply.
    path = "location" if not target_user else f"location/{_uid(target_user)}"
    return _with_map(_curl("GET", path))

def locate(target_user=None):
    # FRESH "where are you right now": wakes the phone for one new fix and waits
    # ~12s. Self, or another person (admin only). Returns {found, fresh, lat,
    # lng, ts, age_s, place, map}. Use this when the user wants the CURRENT
    # location; use get_location for the last known one without waking the phone.
    return _with_map(_curl("POST", "locate", {"user": _uid(target_user)} if target_user else {}))

def track(target_user, minutes=30, interval_s=60):
    # Follow someone for a while: their phone reports a fix every interval_s
    # seconds for `minutes` minutes, then stops on its own. Admin only for others.
    return _curl("POST", "track", {"user": _uid(target_user),
                                    "minutes": int(minutes), "interval_s": int(interval_s)})

def share_location(with_user, minutes=60, interval_s=60):
    # The other direction: send MY OWN location to someone for a while. Their
    # phone is not touched; mine reports every interval_s seconds for `minutes`
    # and stops on its own, and they get each update narrated plus a push
    # saying the share started. Anyone may do this — it is my own location, so
    # no admin check: a kid telling a parent where they are is the main case.
    # Use this for "share my location with X", "let Sam see where I am".
    return _curl("POST", "track", {"share_with": _uid(with_user),
                                    "minutes": int(minutes), "interval_s": int(interval_s)})

def stop_sharing():
    # Stop sharing my own location (same row as share_location).
    return _curl("POST", "track/stop", {})

def stop_track(target_user=None):
    return _curl("POST", "track/stop", {"user": _uid(target_user)} if target_user else {})

def track_status(target_user=None):
    path = "track" if not target_user else f"track/{_uid(target_user)}"
    return _with_map(_curl("GET", path))

def ring_phone(target_user=None, seconds=45):
    # "Where did I leave my phone": makes it ring loudly even in silencio — the
    # sound goes out on the alarm stream, which silent mode doesn't touch. The
    # phone shows who asked and a Detener button, and it stops on its own.
    # Self, or another person (admin only). seconds is capped at 120.
    body = {"seconds": int(seconds)}
    if target_user:
        body["user"] = _uid(target_user)
    return _curl("POST", "ring", body)

def stop_ring(target_user=None):
    return _curl("POST", "ring/stop", {"user": _uid(target_user)} if target_user else {})
```

Call the function matching the `action` field. Print the result with `print(json.dumps(result, indent=2, ensure_ascii=False))`.
