```python
import subprocess, json, os, math

# Through the portal, as the person: HomeCore forwards /studio/api/* to the
# house's studio with their login.
# No default host: the deployer names the portal from `dns:`.
BASE = os.environ.get("TASKS_API_URL", "").replace("/tasks/api", "/studio/api")
USER_ID = os.environ.get("HOMECORE_USER_ID", "")
TOKEN = os.environ.get("HOMECORE_PROXY_TOKEN", "")

def _curl(method, path, data=None):
    if not BASE:
        return {"error": "The portal's address is not configured (TASKS_API_URL missing)."}
    if not USER_ID or not TOKEN:
        return {"error": "This account has no access to the Studio (HOMECORE_USER_ID/HOMECORE_PROXY_TOKEN missing)."}
    cmd = ["curl", "-sk", "--max-time", "60", "-X", method,
           "-H", f"X-Proxy-Secret: {TOKEN}", "-H", f"X-Proxy-User: {USER_ID}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    cmd.append(f"{BASE}/{path}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=70)
    try: return json.loads(r.stdout)
    except Exception: return {"error": (r.stdout or r.stderr).strip()[:300]}

def _minutes(s):
    return max(1, round((s or 0) / 60))

def _default():
    # The person's default Studio project: everything asked of Alfred lands
    # there, one place to find loose requests, and each can be redone there.
    d = _curl("GET", "default-project")
    return d.get("id"), (None if d.get("id") else d)

def _add(section, items):
    pid, err = _default()
    if err:
        return err
    r = _curl("POST", f"projects/{pid}/items", {"section": section, "items": items, "generate": True})
    q = r.get("queued") or []
    if not q:
        return r
    first = q[0]
    return {"queued": len(q), "position": first.get("position"),
            "starts_in_minutes": _minutes(first.get("starts_in")),
            "takes_minutes": _minutes(sum((j.get("takes") or 0) for j in q)),
            "project": pid, "open": f"/studio?project={pid}",
            "note": "Queued on the house's card, in the person's default Studio project (Alfred). "
                    "They get a notification when it is ready; it is NOT done yet."}

def make_image(prompt, size="1024x1024"):
    return _add("images", [{"prompt": prompt, "size": size, "title": prompt[:60]}])

def make_song(lyrics, style="", seconds=90, language="es", title=""):
    return _add("audio", [{"kind": "song", "title": title or style[:60], "lyrics": lyrics,
                           "style": style, "seconds": int(seconds), "language": language}])

def make_instrumental(style, seconds=60, title=""):
    return _add("audio", [{"kind": "instrumental", "title": title or style[:60], "style": style,
                           "seconds": int(seconds)}])

def make_video(description, seconds=5, dialogue="", sound="", music="", title=""):
    # Shots of up to 15 s, each continuing the one before -- except the first,
    # which starts fresh: the shot before it in the project is another request.
    seconds = max(5, min(300, float(seconds)))
    n = max(1, math.ceil(seconds / 15))
    each = max(5, min(20, round(seconds / n)))
    shots = [{"prompt": description + (f" (part {i + 1} of {n}, continuing the same action)" if n > 1 else ""),
              "seconds": each, "continuity": i > 0, "soundscape": sound, "music": music,
              "title": (title or description)[:60],
              "dialogue": dialogue if i == 0 else ""} for i in range(n)]
    return _add("shots", shots)

def studio_queue():
    q = _curl("GET", "queue")
    if "error" in q:
        return q
    run = q.get("running")
    return {"card": ("paused" if (q.get("status") or {}).get("paused") else
                     f"busy: {run['what']} for {'you' if run['mine'] else run['owner_name']}, {round(run['progress'] * 100)}%"
                     if run else "free"),
            "waiting": [{"position": j["position"], "who": "you" if j["mine"] else j["owner_name"],
                         "what": j["what"], "starts_in_minutes": _minutes(j.get("starts_in"))}
                        for j in q.get("queued") or []]}

def my_projects():
    r = _curl("GET", "projects")
    return {"projects": [{"name": p["name"], "open": f"/studio?project={p['id']}"} for p in (r.get("projects") or [])[:15]]}
```
