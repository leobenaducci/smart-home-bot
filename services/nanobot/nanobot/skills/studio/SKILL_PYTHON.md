```python
import subprocess, json, os, math

# Through the portal, as the person: HomeCore forwards /studio/api/* to the
# house's studio with their login.
BASE = os.environ.get("TASKS_API_URL", "https://hub.home:21001/tasks/api").replace("/tasks/api", "/studio/api")
USER_ID = os.environ.get("HOMECORE_USER_ID", "")
TOKEN = os.environ.get("HOMECORE_PROXY_TOKEN", "")

def _curl(method, path, data=None):
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

def _project(name, **items):
    # A project per request: the person finds it in the Studio, and can redo it there.
    p = _curl("POST", "projects", {"name": name[:60]})
    if "id" not in p:
        return None, p
    saved = _curl("PUT", f"projects/{p['id']}", items)
    return saved, None

def _queue(pid, ids):
    r = _curl("POST", f"projects/{pid}/generate", {"items": ids})
    q = r.get("queued") or []
    if not q:
        return r
    first = q[0]
    return {"queued": len(q), "position": first.get("position"),
            "starts_in_minutes": _minutes(first.get("starts_in")),
            "takes_minutes": _minutes(sum((j.get("takes") or 0) for j in q)),
            "project": pid, "open": f"/studio?project={pid}",
            "note": "Queued on the house's card. The person gets a notification when it is ready; it is NOT done yet."}

def make_image(prompt, size="1024x1024"):
    doc, err = _project(prompt, images=[{"prompt": prompt, "size": size}])
    return err or _queue(doc["id"], [doc["images"][0]["id"]])

def make_song(lyrics, style="", seconds=90, language="es", title=""):
    doc, err = _project(title or (style or "Canción"), audio=[{"kind": "song", "title": title, "lyrics": lyrics,
                                                       "style": style, "seconds": int(seconds), "language": language}])
    return err or _queue(doc["id"], [doc["audio"][0]["id"]])

def make_instrumental(style, seconds=60, title=""):
    doc, err = _project(title or style, audio=[{"kind": "instrumental", "title": title, "style": style, "seconds": int(seconds)}])
    return err or _queue(doc["id"], [doc["audio"][0]["id"]])

def make_video(description, seconds=5, dialogue="", sound="", music="", title=""):
    # Shots of up to 15 s, each continuing the one before.
    seconds = max(5, min(300, float(seconds)))
    n = max(1, math.ceil(seconds / 15))
    each = max(5, min(20, round(seconds / n)))
    shots = [{"prompt": description + (f" (part {i + 1} of {n}, continuing the same action)" if n > 1 else ""),
              "seconds": each, "continuity": True, "soundscape": sound, "music": music,
              "dialogue": dialogue if i == 0 else ""} for i in range(n)]
    doc, err = _project(title or description, shots=shots)
    return err or _queue(doc["id"], [s["id"] for s in doc["shots"]])

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
