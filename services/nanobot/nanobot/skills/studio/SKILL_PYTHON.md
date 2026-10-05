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
    return {"projects": [{"name": p["name"], "kind": p.get("kind") or "", "open": f"/studio?project={p['id']}"}
                         for p in (r.get("projects") or [])[:15]]}

# -- a project of its own: characters, a storyboard timed to its song -----------
def _project(project):
    """A project by its name (or part of it) or id -- never a guess: none or
    several matching is said, with the names, so the person can pick."""
    want = str(project or "").strip().lower()
    ps = _curl("GET", "projects").get("projects") or []
    exact = [p for p in ps if p["id"] == want or p["name"].strip().lower() == want]
    found = exact or [p for p in ps if want and want in p["name"].lower()]
    if len(found) != 1:
        return None, {"error": ("no project called that" if not found else "several projects match"),
                      "projects": [p["name"] for p in (found or ps)[:15]]}
    doc = _curl("GET", f"projects/{found[0]['id']}")
    return (doc, None) if doc.get("id") else (None, doc)

def _song(doc, song=""):
    audio = [a for a in doc.get("audio") or [] if a.get("takes")]
    want = str(song or "").strip().lower()
    st = (doc.get("settings") or {}).get("soundtrack")
    for a in audio:
        if (want and want in (a.get("title") or "").lower()) or (not want and a["id"] == st):
            return a
    songs = [a for a in audio if a.get("kind", "song") in ("song", "instrumental", "voice")]
    return songs[0] if songs and not want else None

def _cast_ids(pid, names):
    chars = _curl("GET", f"projects/{pid}/characters").get("characters") or []
    by = {c["name"].strip().lower(): c["id"] for c in chars}
    return [by[n.strip().lower()] for n in names or [] if n.strip().lower() in by]

def project_details(project):
    doc, err = _project(project)
    if err:
        return err
    pid = doc["id"]
    chars = _curl("GET", f"projects/{pid}/characters").get("characters") or []
    return {"name": doc["name"], "kind": doc.get("kind") or "", "look": (doc.get("settings") or {}).get("look", ""),
            "songs": [{"title": a.get("title"), "kind": a.get("kind", "song"), "has_versions": bool(a.get("takes"))}
                      for a in doc.get("audio") or []],
            "shots": [{"n": i + 1, "prompt": s.get("prompt", "")[:200], "seconds": s.get("seconds"),
                       "frame": bool(s.get("boards")), "video": bool(s.get("takes"))}
                      for i, s in enumerate(doc.get("shots") or [])],
            "characters": [{"name": c["name"], "look": c.get("look", "")[:200], "scope": c.get("scope")} for c in chars],
            "open": f"/studio?project={pid}"}

def create_character(project, name, look, personality="", portrait=False):
    doc, err = _project(project)
    if err:
        return err
    pid = doc["id"]
    c = _curl("POST", f"projects/{pid}/characters", {"name": name, "look": look, "personality": personality})
    if not c.get("id"):
        return c
    out = {"created": c["name"], "open": f"/studio?project={pid}",
           "note": "In the project's Characters tab; its look goes into every frame and shot it is cast in."}
    if portrait:
        r = _curl("POST", f"projects/{pid}/characters/{c['id']}/portrait", {})
        out["portrait"] = "queued" if not r.get("error") and not r.get("detail") else r
    return out

def clone_project(project, name=""):
    """A copy of an existing project, with its shots, audio, pictures and characters."""
    doc, err = _project(project)
    if err:
        return err
    pid = doc["id"]
    body = {"name": name.strip()[:80]} if name and str(name).strip() else {}
    copy = _curl("POST", f"projects/{pid}/duplicate", body)
    if not copy.get("id"):
        return copy
    return {"cloned": doc["name"], "new_project": copy["name"], "id": copy["id"],
            "open": f"/studio?project={copy['id']}",
            "note": "The copy has its own characters and takes; changing it does not change the original."}

def edit_character(project, character, **fields):
    """Change a character's name, look, personality or voice text."""
    doc, err = _project(project)
    if err:
        return err
    pid = doc["id"]
    chars = _curl("GET", f"projects/{pid}/characters").get("characters") or []
    want = str(character or "").strip().lower()
    found = [c for c in chars if c["name"].strip().lower() == want]
    if not found:
        found = [c for c in chars if want and want in c["name"].lower()]
    if len(found) != 1:
        return {"error": ("no character called that" if not found else "several characters match"),
                "characters": [c["name"] for c in chars[:15]]}
    cid = found[0]["id"]
    allowed = {"name", "look", "personality", "voice_text", "portrait", "look_from"}
    clean = {k: v for k, v in fields.items() if k in allowed and v is not None}
    updated = _curl("PUT", f"projects/{pid}/characters/{cid}", clean)
    if not updated.get("id"):
        return updated
    return {"updated": updated["name"], "open": f"/studio?project={pid}",
            "note": "The change is in the project's Characters tab."}

def song_timing(project, song="", shot_seconds=8):
    """Where the cuts fall on the song and the words sung in each. The Studio
    listens once (about a minute on the card); until then this says so."""
    doc, err = _project(project)
    if err:
        return err
    a = _song(doc, song)
    if not a:
        return {"error": "this project has no song with a version yet", "project": doc["name"]}
    pid = doc["id"]
    import time
    for _ in range(8):
        r = _curl("POST", f"projects/{pid}/items/{a['id']}/analyze", {})
        if r.get("analysis") or r.get("failed") or r.get("error") or r.get("detail"):
            break
        time.sleep(5)
    if not r.get("analysis"):
        if r.get("failed") or r.get("error") or r.get("detail"):
            return {"error": r.get("failed") or r.get("error") or r.get("detail")}
        return {"listening": True, "note": "The Studio is listening to the song on the card. "
                "Tell the person, and call song_timing again in a minute or two."}
    c = _curl("POST", f"projects/{pid}/items/{a['id']}/cuts", {"shot_seconds": float(shot_seconds)})
    if "cuts" not in c:
        return c
    return {"song": a.get("title"), "seconds": round(c["duration"]), "bpm": round(c.get("tempo") or 0),
            "shot_seconds": float(shot_seconds),
            "shots": [{"n": i + 1, "from": round(x["start"], 1), "to": round(x["end"], 1),
                       "section": x.get("section", ""),
                       "sung": x.get("words", "") if x.get("sung") else "(music only)"}
                      for i, x in enumerate(c["cuts"])],
            "next": "Write one English visual description per shot, following what is sung in it, "
                    "then call set_storyboard with exactly this many shots and the same shot_seconds."}

def set_storyboard(project, shots, song="", shot_seconds=8, look="", replace=False):
    """The storyboard: shots timed to the song's cuts, a still frame drawn for
    each. No video -- the person looks at the frames in the Storyboard tab,
    changes what they want, and generates the videos from there."""
    doc, err = _project(project)
    if err:
        return err
    pid = doc["id"]
    a = _song(doc, song)
    cuts = []
    if a:
        c = _curl("POST", f"projects/{pid}/items/{a['id']}/cuts", {"shot_seconds": float(shot_seconds)})
        cuts = c.get("cuts") or []
    if cuts and len(cuts) != len(shots):
        return {"error": f"the song has {len(cuts)} shots at {shot_seconds} s each and {len(shots)} were given; "
                         "call song_timing and write exactly one per shot"}
    new = []
    for i, s in enumerate(shots):
        s = s if isinstance(s, dict) else {"prompt": str(s)}
        item = {"prompt": str(s.get("prompt") or "")[:1200], "cast": _cast_ids(pid, s.get("cast") or []),
                "continuity": bool(s.get("continues")) and i > 0, "seconds": float(s.get("seconds") or shot_seconds),
                "title": f"{doc['name']} {i + 1}"}
        if cuts:
            item.update(start=cuts[i]["start"], seconds=cuts[i]["seconds"], exact=True)
        new.append(item)
    settings = {**({"soundtrack": a["id"]} if a else {}), **({"look": look} if look else {})}
    if replace:
        body = {"shots": new, **({"settings": settings} if settings else {})}
        saved = _curl("PUT", f"projects/{pid}", body)
        if not saved.get("id"):
            return saved
        ids = [s["id"] for s in saved.get("shots") or []]
    else:
        if settings:
            _curl("PUT", f"projects/{pid}", {"settings": settings})
        r = _curl("POST", f"projects/{pid}/items", {"section": "shots", "items": new})
        ids = r.get("items") or []
        if not ids:
            return r
    q = _curl("POST", f"projects/{pid}/storyboard", {"items": ids})
    return {"shots": len(ids), "frames_queued": len(q.get("queued") or []), "timed_to_song": bool(cuts),
            "open": f"/studio?project={pid}",
            "note": "Only the still frames are being drawn, about a minute each. The person reviews them in the "
                    "project's Storyboard tab, changes or redraws any, and generates the videos from there. "
                    "Do NOT start the videos unless they ask."}

def make_shot_videos(project, without_frames=False):
    """The videos of every shot that has none yet, each starting from its
    chosen storyboard frame. Long: minutes per 5 seconds. Only when asked."""
    doc, err = _project(project)
    if err:
        return err
    pid = doc["id"]
    todo = [s for s in doc.get("shots") or [] if not s.get("takes")]
    if not todo:
        return {"error": "every shot already has a video; the person can redo one from the page"}
    # A video starts from its frame, chosen when it is queued: queued before
    # the frame is drawn, it starts from nothing the person has seen.
    bare = [i + 1 for i, s in enumerate(doc.get("shots") or []) if not s.get("takes") and not s.get("boards")]
    if bare and not without_frames:
        return {"error": f"shots {bare} have no storyboard frame yet (still being drawn, or never asked for). "
                         "Wait for the frames, or pass without_frames: true only if the person wants the videos anyway."}
    ids = [s["id"] for s in todo]
    r = _curl("POST", f"projects/{pid}/generate", {"items": ids})
    q = r.get("queued") or []
    if not q:
        return r
    return {"queued": len(q), "starts_in_minutes": _minutes(q[0].get("starts_in")),
            "takes_minutes": _minutes(sum((j.get("takes") or 0) for j in q)), "open": f"/studio?project={pid}",
            "note": "Queued; they get a notification as the shots finish. NOT done yet."}
```
