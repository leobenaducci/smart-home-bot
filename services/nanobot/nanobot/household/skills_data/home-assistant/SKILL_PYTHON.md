```python
import difflib, json, os, re, time, unicodedata

import requests

# The address comes from the environment, which comes from `dns:`. Every earlier
# attempt at this spelled the hostname into a throwaway script, which is naming
# some *other* household's host. Nothing here may name one.
BASE = (os.environ.get("HOMEASSISTANT_URL") or "").rstrip("/")
TOKEN = os.environ.get("HOMEASSISTANT_TOKEN", "")
BACKUPS = os.path.expanduser("~/.nanobot/workspace/ha-backups")
_REG: dict = {}


def _norm(s):
    """How names are compared: lower case, no accents, punctuation as spaces.

    "lampara paula", "Lámpara  Paula" and "lámpara-paula" are one name to
    the person saying it; HA's own names, entity ids (`light.luz_de_oficina`)
    and area ids (`dormitorio_paula`) all come down to the same words.
    """
    s = unicodedata.normalize("NFKD", str(s or "").lower())
    s = "".join(c if c.isalnum() else " " for c in s if not unicodedata.combining(c))
    return " ".join(s.split())


def _rest(method, path, body=None):
    if not (BASE and TOKEN):
        return {"error": "Home Assistant is not configured here "
                         "(HOMEASSISTANT_URL / HOMEASSISTANT_TOKEN missing)."}
    try:
        r = requests.request(method, f"{BASE}/api/{path.lstrip('/')}",
                             headers={"Authorization": f"Bearer {TOKEN}",
                                      "Content-Type": "application/json"},
                             json=body, timeout=20)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    if not r.ok:
        return {"error": (r.text or "")[:300], "status": r.status_code}
    try:
        return r.json()
    except Exception:
        return {"ok": True}


def _ws(*commands):
    """Run registry commands over the websocket API, in order; one result each.

    The registries -- names, areas, which device an entity sits on -- are not
    in REST at all. Each command is (type, payload); a failed one comes back as
    {"error": ...} in its slot rather than raising, so a batch reports what it
    managed.
    """
    import websocket
    url = re.sub(r"^http", "ws", BASE) + "/api/websocket"
    ws = websocket.create_connection(url, timeout=30)
    try:
        ws.recv()
        ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        for _ in range(10):
            msg = json.loads(ws.recv())
            if msg.get("type") == "auth_ok":
                break
            if msg.get("type") == "auth_invalid":
                raise RuntimeError("Home Assistant rejected HOMEASSISTANT_TOKEN")
        else:
            raise RuntimeError("Home Assistant never confirmed the token")
        out = []
        for n, (cmd, payload) in enumerate(commands, start=1):
            ws.send(json.dumps({"id": n, "type": cmd, **(payload or {})}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == n:
                    out.append(msg.get("result") if msg.get("success", True)
                               else {"error": (msg.get("error") or {}).get("message", "failed")})
                    break
    finally:
        ws.close()
    return out


def _registries():
    """The device, entity and area registries, which REST does not expose.

    They are the only place an automation's `device_id` and its registry-id
    `entity_id` can be turned back into something a person can read, and that
    translation is the whole reason this skill exists: a trigger pointed at
    `03e5fb77fda6178a431d98b031655750` looks fine until you learn it is the
    ZHA *Identify* button and can never be pressed by the switch on the wall.
    """
    if _REG:
        return _REG
    entities, devices, areas = _ws(("config/entity_registry/list", None),
                                   ("config/device_registry/list", None),
                                   ("config/area_registry/list", None))
    # A failed command is an {"error": ...} dict in its slot; iterating that as
    # a registry reads its keys as entities, so it counts as empty instead.
    as_list = lambda v: v if isinstance(v, list) else []
    _REG.update(entities=as_list(entities), devices=as_list(devices), areas=as_list(areas))
    return _REG


def _device_label(dev):
    name = dev.get("name_by_user") or dev.get("name") or dev.get("id")
    model = " ".join(str(x) for x in (dev.get("manufacturer"), dev.get("model")) if x)
    return f"{name} ({model})" if model else str(name)


def _resolve_ref(value):
    """Turn one registry id into something readable, or return None."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        return None
    reg = _registries()
    for ent in reg["entities"]:
        if ent.get("id") == value:
            label = {"entity_id": ent.get("entity_id"),
                     "name": ent.get("name") or ent.get("original_name")}
            if ent.get("entity_category"):
                label["entity_category"] = ent["entity_category"]
            if ent.get("disabled_by"):
                label["disabled_by"] = ent["disabled_by"]
            return label
    for dev in reg["devices"]:
        if dev.get("id") == value:
            return {"device": _device_label(dev),
                    "entities": sorted(e.get("entity_id") for e in reg["entities"]
                                       if e.get("device_id") == value)}
    return {"unresolved": "no device or entity in the registry has this id"}


def _annotate(node):
    """Walk a config and hang a `_resolved` note beside every opaque id."""
    if isinstance(node, list):
        return [_annotate(v) for v in node]
    if not isinstance(node, dict):
        return node
    out = {k: _annotate(v) for k, v in node.items()}
    notes = {}
    for key in ("device_id", "entity_id", "target_entity_id"):
        got = _resolve_ref(node.get(key))
        if got:
            notes[key] = got
    if notes:
        out["_resolved"] = notes
    return out


def _automations():
    states = _rest("GET", "states")
    if isinstance(states, dict):
        return states
    return [{"entity_id": s["entity_id"],
             "id": (s.get("attributes") or {}).get("id"),
             "alias": (s.get("attributes") or {}).get("friendly_name"),
             "state": s.get("state"),
             "mode": (s.get("attributes") or {}).get("mode"),
             "last_triggered": (s.get("attributes") or {}).get("last_triggered")}
            for s in states if s["entity_id"].startswith("automation.")]


def _find_automation(automation):
    """Accept an entity_id, an alias, or the numeric id. Returns a row or an error."""
    rows = _automations()
    if isinstance(rows, dict):
        return rows
    want = str(automation).strip()
    low = _norm(want)
    for row in rows:
        if want in (row["id"], row["entity_id"]) or low == _norm(row["alias"]):
            return row
    hits = [r for r in rows if low and (low in _norm(r["alias"]) or low in _norm(r["entity_id"]))]
    if len(hits) == 1:
        return hits[0]
    if hits:
        return {"error": f"{want!r} matches more than one automation",
                "candidates": [r["alias"] for r in hits]}
    # Before giving up: the thing people name is usually the switch, and the
    # automation is called something else entirely.
    try:
        wired = automations_for(want).get("automations") or []
    except Exception:
        wired = []
    if len(wired) == 1:
        return next(r for r in rows if r["id"] == wired[0]["id"])
    if wired:
        return {"error": f"{want!r} is not an automation, but these mention it",
                "candidates": [r["alias"] for r in wired]}
    return {"error": f"no automation matches {want!r}",
            "hint": "run list_automations to see the names, or automations_for "
                    "to ask what mentions a device"}


# --- reads -----------------------------------------------------------------
def _refs_of(node, found=None):
    """Every device or entity a config mentions, however it spells it.

    `device_id` and the 32-hex `entity_id`, but also `device_ieee` -- a
    zha_event trigger names the radio address and nothing else, so an
    automation driven by a battery remote refers to it by a string that appears
    in no registry field this walks.
    """
    found = set() if found is None else found
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("device_id", "entity_id", "target_entity_id", "device_ieee"):
                for one in (value if isinstance(value, list) else [value]):
                    if isinstance(one, str):
                        found.add(one.lower())
            else:
                _refs_of(value, found)
    elif isinstance(node, list):
        for value in node:
            _refs_of(value, found)
    return found


def _ids_naming(thing):
    """Every id a device or entity called *thing* could be referred to by."""
    low = str(thing).strip().lower()
    key = _norm(thing)
    reg = _registries()
    ids = {low}
    for dev in reg["devices"]:
        names = [_norm(dev.get("name_by_user")), _norm(dev.get("name"))]
        if dev["id"] == thing or (key and any(key in n for n in names)):
            ids.add(dev["id"].lower())
            # The value, never the domain: a pair is ("zha", "a4:c1:38:…"),
            # and putting "zha" in the search set makes it match on the word.
            for pair in (dev.get("connections") or []) + (dev.get("identifiers") or []):
                if len(pair) > 1:
                    ids.add(str(pair[1]).lower())
            for ent in reg["entities"]:
                if ent.get("device_id") == dev["id"]:
                    ids.add(str(ent.get("entity_id") or "").lower())
                    ids.add(str(ent.get("id") or "").lower())
    for ent in reg["entities"]:
        label = _norm(ent.get("name") or ent.get("original_name"))
        if low in (str(ent.get("entity_id") or "").lower(), str(ent.get("id") or "").lower()) \
                or (label and key == label):
            ids.add(str(ent.get("entity_id") or "").lower())
            ids.add(str(ent.get("id") or "").lower())
    return {i for i in ids if i}


def automations_for(thing):
    """Which automations mention this device or entity.

    The question people actually ask is "what does this switch do?", and the
    switch is almost never called what the automation is called: the device
    here is `Switch Cama Nico` and the automation is `Boton Cama Nico`.
    Looking the name up as an automation returns nothing, which reads as
    "nothing is wired to it" -- the wrong answer, and the one that sends you
    back to writing scripts.
    """
    ids = _ids_naming(thing)
    rows = _automations()
    if isinstance(rows, dict):
        return rows
    hits = []
    for row in rows:
        if not row.get("id"):
            continue
        config = _rest("GET", f"config/automation/config/{row['id']}")
        if isinstance(config, dict) and "error" in config:
            continue
        shared = _refs_of(config) & ids
        if shared:
            hits.append({"alias": row["alias"], "entity_id": row["entity_id"],
                         "id": row["id"], "state": row["state"],
                         "last_triggered": row["last_triggered"],
                         "matched_on": sorted(shared)})
    return {"searched": sorted(ids), "automations": hits}



def search_ha(search):
    """Entities, devices and automations whose id or name contains *search*."""
    low = _norm(search)
    states = _rest("GET", "states")
    if isinstance(states, dict):
        return states
    entities = [{"entity_id": s["entity_id"],
                 "name": (s.get("attributes") or {}).get("friendly_name"),
                 "state": s.get("state")}
                for s in states
                if low in _norm(s["entity_id"])
                or low in _norm((s.get("attributes") or {}).get("friendly_name"))]
    try:
        devices = [{"device_id": d["id"], "device": _device_label(d)}
                   for d in _registries()["devices"]
                   if low in _norm(" ".join(str(x or "") for x in
                                            (d.get("name"), d.get("name_by_user"), d.get("model"))))]
    except Exception as exc:
        devices = [{"error": f"{type(exc).__name__}: {exc}"}]
    return {"entities": entities[:60], "devices": devices[:20],
            "automations": [e for e in entities if e["entity_id"].startswith("automation.")]}


def get_entity(entity_id):
    return _rest("GET", f"states/{entity_id}")


def list_automations():
    return {"automations": _automations()}


def get_automation(automation):
    """The full config, with every device and registry id resolved to a name."""
    row = _find_automation(automation)
    if "error" in row:
        return row
    if not row.get("id"):
        return {"error": f"{row['entity_id']} has no id, so it is not editable "
                         "through the config API (it is probably in a YAML package)."}
    config = _rest("GET", f"config/automation/config/{row['id']}")
    if isinstance(config, dict) and "error" in config:
        return config
    return {"entity_id": row["entity_id"], "id": row["id"], "state": row["state"],
            "last_triggered": row["last_triggered"], "config": _annotate(config)}


def _find_device(device):
    """One device by id or by (part of) its name, or an {"error": ...}."""
    reg = _registries()
    low = _norm(device)
    names = lambda d: (_norm(d.get("name_by_user")), _norm(d.get("name")))
    hits = [d for d in reg["devices"]
            if d["id"] == device or (low and any(low in n for n in names(d)))]
    exact = [d for d in hits if d["id"] == device or low in names(d)]
    hits = exact if len(exact) == 1 else hits
    if not hits:
        return {"error": f"no device matches {device!r}"}
    if len(hits) > 1:
        return {"error": f"{device!r} matches more than one device",
                "candidates": [_device_label(d) for d in hits]}
    return hits[0]


def _find_entity(entity):
    """One registry entity by entity_id or by (part of) its name."""
    reg = _registries()
    for e in reg["entities"]:
        if e.get("entity_id") == entity:
            return e
    low = _norm(entity)
    names = lambda e: [_norm(x) for x in (e.get("name"), e.get("original_name"), e.get("entity_id"))]
    hits = [e for e in reg["entities"] if any(low and low in n for n in names(e))]
    exact = [e for e in hits if low in names(e)]
    hits = exact if len(exact) == 1 else hits
    if not hits:
        return {"error": f"no entity matches {entity!r}; search_ha finds entity ids"}
    if len(hits) > 1:
        return {"error": f"{entity!r} matches more than one entity",
                "candidates": sorted(e["entity_id"] for e in hits)[:20]}
    return hits[0]


def _area_name(area_id):
    for a in _registries().get("areas") or []:
        if a.get("area_id") == area_id:
            return a.get("name")
    return None


def describe_device(device):
    """A device and every entity on it, by device_id or by name."""
    dev = _find_device(device)
    if "error" in dev:
        return dev
    reg = _registries()
    return {"device_id": dev["id"], "device": _device_label(dev),
            "area": _area_name(dev.get("area_id")),
            "integration": dev.get("identifiers") and dev["identifiers"][0][0],
            "entities": [{"entity_id": e.get("entity_id"),
                          "registry_id": e.get("id"),
                          "name": e.get("name") or e.get("original_name"),
                          "area": _area_name(e.get("area_id")),
                          "entity_category": e.get("entity_category"),
                          "disabled_by": e.get("disabled_by")}
                         for e in reg["entities"] if e.get("device_id") == dev["id"]]}


def list_ha_lights():
    """Every light Home Assistant has, whatever Assist is allowed to see.

    `GetLiveContext` shows names and state but no entity ids, and only what is
    exposed to Assist; renaming or placing a light needs the id. This is the
    join: id, the name HA shows, its device, area, state and integration.
    """
    reg = _registries()
    states = _rest("GET", "states")
    if isinstance(states, dict):
        return states
    by_id = {s["entity_id"]: s for s in states}
    try:
        exposed = (_ws(("homeassistant/expose_entity/list", None))[0] or {}).get(
            "exposed_entities", {})
    except Exception:
        exposed = {}
    devices = {d["id"]: d for d in reg["devices"]}
    out = []
    for e in reg["entities"]:
        if not str(e.get("entity_id")).startswith("light."):
            continue
        dev = devices.get(e.get("device_id")) or {}
        st = by_id.get(e["entity_id"]) or {}
        attrs = st.get("attributes") or {}
        out.append({
            "entity_id": e["entity_id"],
            "name": attrs.get("friendly_name") or e.get("name") or e.get("original_name"),
            "device": _device_label(dev) if dev else None,
            "area": _area_name(e.get("area_id") or dev.get("area_id")),
            "state": st.get("state"),
            "brightness": attrs.get("brightness"),
            "rgb": attrs.get("rgb_color"),
            "integration": e.get("platform"),
            "assist": bool((exposed.get(e["entity_id"]) or {}).get("conversation")),
        })
    return {"lights": sorted(out, key=lambda r: r["entity_id"])}


def list_automation_backups(automation=None):
    if not os.path.isdir(BACKUPS):
        return {"backups": []}
    names = sorted(os.listdir(BACKUPS), reverse=True)
    if automation:
        row = _find_automation(automation)
        if "error" not in row:
            names = [n for n in names if n.startswith(f"{row['id']}-")]
    return {"backups": [os.path.join(BACKUPS, n) for n in names[:20]]}


# --- writes ----------------------------------------------------------------

def _diff(old, new):
    return "".join(difflib.unified_diff(
        json.dumps(old, indent=2, ensure_ascii=False, sort_keys=True).splitlines(True),
        json.dumps(new, indent=2, ensure_ascii=False, sort_keys=True).splitlines(True),
        fromfile="current", tofile="proposed"))


def set_automation(automation, config, confirmed=False, allow_removals=False):
    """Replace an automation's config. Shows the diff first; writes only when told to.

    Two turns on purpose. `confirmed=False` (the default) reads the current
    config, diffs the proposal against it and writes nothing, so the person can
    be shown exactly what would change before it changes. Call again with
    `confirmed=True` to apply.
    """
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except Exception as exc:
            return {"error": f"config is not valid JSON: {exc}"}
    if not isinstance(config, dict):
        return {"error": "config must be the whole automation object"}

    row = _find_automation(automation)
    if "error" in row:
        return row
    if not row.get("id"):
        return {"error": f"{row['entity_id']} is not editable through the config API"}
    current = _rest("GET", f"config/automation/config/{row['id']}")
    if isinstance(current, dict) and "error" in current:
        return current

    # This endpoint REPLACES the automation -- there is no patch. A config
    # rebuilt from memory that quietly lost `mode` or half its actions writes
    # cleanly and fails later as "the button stopped working", so a dropped
    # top-level key is refused rather than reported.
    config.setdefault("id", row["id"])
    dropped = [k for k in current if k not in config]
    if dropped and not allow_removals:
        return {"error": "the proposed config drops top-level keys the current one has",
                "dropped": dropped,
                "hint": "start from get_automation's config, change what you mean to "
                        "change, and send the whole object back; pass "
                        "allow_removals=True only if removing them is the point."}

    diff = _diff(current, config)
    if not diff:
        return {"ok": True, "unchanged": True}
    if not confirmed:
        return {"pending": True, "diff": diff,
                "note": "Nothing was written. Show this diff, get a yes, then call "
                        "set_automation again with confirmed=True."}

    os.makedirs(BACKUPS, exist_ok=True)
    backup = os.path.join(BACKUPS, f"{row['id']}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(backup, "w", encoding="utf-8") as fh:
        json.dump(current, fh, indent=2, ensure_ascii=False)

    wrote = _rest("POST", f"config/automation/config/{row['id']}", config)
    if isinstance(wrote, dict) and "error" in wrote:
        return {"error": wrote["error"], "backup": backup, "written": False}
    _rest("POST", "services/automation/reload", {})
    return {"ok": True, "backup": backup, "diff": diff}


def restore_automation(backup, confirmed=False):
    """Put back a config `set_automation` saved before it wrote."""
    try:
        with open(backup, encoding="utf-8") as fh:
            config = json.load(fh)
    except Exception as exc:
        return {"error": f"cannot read {backup}: {exc}"}
    aid = config.get("id") or os.path.basename(backup).split("-")[0]
    rows = _automations()
    if isinstance(rows, list) and not any(r.get("id") == aid for r in rows):
        # Deleted, not edited: there is nothing to diff against, so it comes
        # back the way a new one is made.
        return create_automation({**config, "id": aid}, confirmed=confirmed)
    return set_automation(aid, config, confirmed=confirmed, allow_removals=True)


def enable_automation(automation):
    row = _find_automation(automation)
    if "error" in row:
        return row
    return _rest("POST", "services/automation/turn_on",
                 {"entity_id": row["entity_id"]}) or {"ok": True}


def disable_automation(automation):
    row = _find_automation(automation)
    if "error" in row:
        return row
    return _rest("POST", "services/automation/turn_off",
                 {"entity_id": row["entity_id"]}) or {"ok": True}


def rename_entity(entity, name):
    """The name HA and Assist show for one entity. "" puts the integration's back."""
    ent = _find_entity(entity)
    if "error" in ent:
        return ent
    before = ent.get("name") or ent.get("original_name")
    got = _ws(("config/entity_registry/update",
               {"entity_id": ent["entity_id"], "name": (str(name).strip() or None)}))[0]
    _REG.clear()
    if isinstance(got, dict) and "error" in got:
        return got
    after = (got.get("entity_entry") or got or {}) if isinstance(got, dict) else {}
    return {"ok": True, "entity_id": ent["entity_id"], "was": before,
            "now": after.get("name") or after.get("original_name")}


def rename_device(device, name):
    """The device's own name (name_by_user). "" puts the integration's back."""
    dev = _find_device(device)
    if "error" in dev:
        return dev
    before = _device_label(dev)
    got = _ws(("config/device_registry/update",
               {"device_id": dev["id"], "name_by_user": (str(name).strip() or None)}))[0]
    _REG.clear()
    if isinstance(got, dict) and "error" in got:
        return got
    return {"ok": True, "device_id": dev["id"], "was": before, "now": _device_label(got)}


def set_area(target, area):
    """Put an entity or a device in an area, by name. "" takes it out of one.

    An entity id (`light.mora`) moves that entity; anything else is looked up
    as a device first. Areas are not created here: a name HA does not have is
    refused with the list it does, so "Dormitorio Paula" cannot quietly
    become a second area spelled "dormitorio de paula".
    """
    area_id = None
    if str(area).strip():
        low = _norm(area)
        areas = _registries().get("areas") or []
        match = [a for a in areas if low in (_norm(a.get("name")), _norm(a.get("area_id")))
                 or low in (_norm(x) for x in a.get("aliases") or [])]
        if not match:
            return {"error": f"Home Assistant has no area called {area!r}",
                    "areas": sorted(a.get("name") for a in areas)}
        area_id = match[0]["area_id"]
    if re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", str(target)):
        ent = _find_entity(target)
        if "error" in ent:
            return ent
        was = _area_name(ent.get("area_id"))
        got = _ws(("config/entity_registry/update",
                   {"entity_id": ent["entity_id"], "area_id": area_id}))[0]
        what = {"entity_id": ent["entity_id"]}
    else:
        dev = _find_device(target)
        if "error" in dev:
            return dev
        was = _area_name(dev.get("area_id"))
        # An entity with an area of its own does not follow its device. One
        # that sat in the device's old room was only repeating it, so it moves
        # too -- otherwise "move the lamp" moves the device page and leaves the
        # light, the thing Assist and the dashboards go by, where it was. One
        # placed somewhere else (a relay's second channel in another room) was
        # put there on purpose and stays.
        ents = [e for e in _registries()["entities"] if e.get("device_id") == dev["id"]]
        follow = [e for e in ents if e.get("area_id") and e.get("area_id") == dev.get("area_id")
                  and area_id != dev.get("area_id")]
        own = [e for e in ents if e.get("area_id") and e.get("area_id") != dev.get("area_id")]
        got = _ws(("config/device_registry/update",
                   {"device_id": dev["id"], "area_id": area_id}),
                  *[("config/entity_registry/update", {"entity_id": e["entity_id"], "area_id": None})
                    for e in follow])[0]
        what = {"device": _device_label(dev)}
        if follow:
            what["moved_with_it"] = [e["entity_id"] for e in follow]
        if own:
            what["kept_their_own_area"] = {e["entity_id"]: _area_name(e["area_id"]) for e in own}
    _REG.clear()
    if isinstance(got, dict) and "error" in got:
        return got
    return {"ok": True, **what, "was": was, "now": _area_name(area_id)}


def reload_automations():
    return _rest("POST", "services/automation/reload", {}) or {"ok": True}


# --- new devices ---------------------------------------------------------------

def pair_zigbee(seconds=120):
    """Let new Zigbee devices join for a while. Put the device in pairing mode now."""
    seconds = max(30, min(int(seconds), 254))
    got = _ws(("zha/devices/permit", {"duration": seconds}))[0]
    if isinstance(got, dict) and "error" in got:
        return got
    return {"ok": True, "open_for_seconds": seconds,
            "next": "put the device in pairing mode now (usually: hold its button ~5 s "
                    "until it blinks), then new_devices to see it arrive"}


def new_devices(hours=24):
    """Devices Home Assistant added recently, with their area and entities.

    A device that just paired is named after its model (`_TZ3000_gdsvhfao
    TS0001`) and sits in no area; this is where naming and placing it starts.
    """
    since = time.time() - float(hours) * 3600
    reg = _registries()
    out = []
    for d in reg["devices"]:
        if (d.get("created_at") or 0) < since or d.get("entry_type") == "service":
            continue
        out.append({"device_id": d["id"], "device": _device_label(d),
                    "added": time.strftime("%Y-%m-%d %H:%M", time.localtime(d["created_at"])),
                    "area": _area_name(d.get("area_id")),
                    "integration": d.get("identifiers") and d["identifiers"][0][0],
                    "entities": sorted(e.get("entity_id") for e in reg["entities"]
                                       if e.get("device_id") == d["id"])})
    return {"devices": sorted(out, key=lambda r: r["added"], reverse=True),
            "hours": hours}


def identify_device(device):
    """Make a Zigbee device blink or beep, so a person can say which one it is.

    Uses the device's own Identify button. Lights reached through the cloud
    (SmartThings) have none: blink those with the lights skill's flash_light.
    """
    dev = _find_device(device)
    if "error" in dev:
        return dev
    reg = _registries()
    ents = [e for e in reg["entities"] if e.get("device_id") == dev["id"]]
    button = next((e["entity_id"] for e in ents
                   if str(e.get("entity_id")).startswith("button.")
                   and "identify" in str(e.get("entity_id")).lower()), None)
    if not button:
        return {"error": f"{_device_label(dev)} has no Identify button",
                "hint": "a WiZ bulb is identified with the lights skill's flash_light; "
                        "otherwise ask the person to press the device and watch new_devices / its state"}
    got = _rest("POST", "services/button/press", {"entity_id": button})
    if isinstance(got, dict) and "error" in got:
        return got
    return {"ok": True, "device": _device_label(dev), "pressed": button,
            "note": "a sleeping battery device only blinks after it next wakes -- press one of its buttons"}


def device_triggers(device):
    """Every press a button or remote reports, ready to paste into a trigger.

    Never write a button trigger from memory: the `type`/`subtype` pair is the
    device's own vocabulary (`remote_button_short_press` / `button_1`), and a
    wrong one saves cleanly and never fires.
    """
    dev = _find_device(device)
    if "error" in dev:
        return dev
    got = _ws(("device_automation/trigger/list", {"device_id": dev["id"]}))[0]
    if isinstance(got, dict) and "error" in got:
        return got
    presses, other = [], []
    for t in got or []:
        t = {k: v for k, v in t.items() if k != "metadata"}
        # The list reports the old key; automations are written with `trigger`.
        t = {"trigger": t.pop("platform", "device"), **t}
        (presses if str(t.get("type", "")).startswith("remote_") else other).append(t)
    out = {"device": _device_label(dev), "device_id": dev["id"], "presses": presses,
           "other": [f"{t.get('domain')}: {t.get('type')}" for t in other]}
    if not presses:
        out["note"] = ("this device lists no button presses. Many Tuya buttons report them "
                       "only as zha_event -- listen_button while the person presses it "
                       "gives the exact trigger. 'button: pressed' here is the Identify "
                       "button, not a physical press.")
    return out


def _ieee(dev):
    for pair in (dev.get("identifiers") or []) + (dev.get("connections") or []):
        if len(pair) > 1 and pair[0] in ("zha", "zigbee") and re.fullmatch(r"([0-9a-f]{2}:){7}[0-9a-f]{2}", str(pair[1]).lower()):
            return str(pair[1]).lower()
    return None


def listen_button(device=None, seconds=20):
    """Wait while the person presses a Zigbee button; return what each press sent.

    The reliable way to wire a button: whatever the device calls its presses,
    this is what Home Assistant actually receives, with a trigger for each one
    ready to put in create_automation. Ask the person to press every button
    (short, double, long) while this listens.
    """
    import websocket
    ieee = None
    if device:
        dev = _find_device(device)
        if "error" in dev:
            return dev
        ieee = _ieee(dev)
        if not ieee:
            return {"error": f"{_device_label(dev)} is not a Zigbee (ZHA) device"}
    seconds = max(5, min(int(seconds), 45))
    names = {}
    for d in _registries()["devices"]:
        i = _ieee(d)
        if i:
            names[i] = d.get("name_by_user") or d.get("name")
    url = re.sub(r"^http", "ws", BASE) + "/api/websocket"
    ws = websocket.create_connection(url, timeout=5)
    seen, presses = set(), []
    try:
        ws.recv()
        ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        if json.loads(ws.recv()).get("type") != "auth_ok":
            return {"error": "Home Assistant rejected HOMEASSISTANT_TOKEN"}
        ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "zha_event"}))
        end = time.time() + seconds
        while time.time() < end:
            ws.settimeout(max(0.5, end - time.time()))
            try:
                msg = json.loads(ws.recv())
            except Exception:
                continue
            data = (msg.get("event") or {}).get("data") or {}
            if msg.get("type") != "event" or (ieee and str(data.get("device_ieee")).lower() != ieee):
                continue
            key = (data.get("device_ieee"), data.get("command"), json.dumps(data.get("args"), sort_keys=True),
                   data.get("endpoint_id"))
            if key in seen:
                continue
            seen.add(key)
            match = {"device_ieee": data.get("device_ieee"), "command": data.get("command")}
            if data.get("endpoint_id") not in (None, 1):
                match["endpoint_id"] = data["endpoint_id"]
            if data.get("args") not in (None, [], {}):
                match["args"] = data["args"]
            presses.append({"device": names.get(str(data.get("device_ieee")).lower()),
                            "command": data.get("command"), "args": data.get("args"),
                            "trigger": {"trigger": "event", "event_type": "zha_event",
                                        "event_data": match}})
    finally:
        ws.close()
    if not presses:
        return {"presses": [], "listened_seconds": seconds,
                "note": "nothing arrived. A new battery button can take a first press to wake; "
                        "ask for the presses again and listen once more."}
    return {"presses": presses, "listened_seconds": seconds}


def create_area(name):
    """A new Home Assistant area. Only when the person asked for that room."""
    name = str(name).strip()
    if not name:
        return {"error": "an area needs a name"}
    areas = _registries().get("areas") or []
    low = _norm(name)
    same = [a["name"] for a in areas if low in (_norm(a.get("name")), _norm(a.get("area_id")))]
    if same:
        return {"ok": True, "exists": same[0]}
    got = _ws(("config/area_registry/create", {"name": name}))[0]
    _REG.clear()
    if isinstance(got, dict) and "error" in got:
        return got
    return {"ok": True, "created": got.get("name"), "area_id": got.get("area_id")}


def create_automation(config, confirmed=False):
    """A new automation. Shows it first; writes only with confirmed=True."""
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except Exception as exc:
            return {"error": f"config is not valid JSON: {exc}"}
    if not isinstance(config, dict):
        return {"error": "config must be the whole automation object"}
    config = {k: v for k, v in config.items() if k != "_resolved"}
    missing = [k for k in ("alias", "triggers", "actions") if not config.get(k)]
    if missing:
        return {"error": f"an automation needs {', '.join(missing)}",
                "hint": "device_triggers gives a button's triggers; copy one whole"}
    rows = _automations()
    taken = [r for r in (rows if isinstance(rows, list) else [])
             if _norm(r.get("alias")) == _norm(config["alias"])]
    if taken:
        return {"error": f"an automation called {config['alias']!r} already exists",
                "hint": "get_automation it and change it with set_automation, or pick another alias"}
    config.setdefault("description", "")
    config.setdefault("conditions", [])
    config.setdefault("mode", "single")
    aid = config.pop("id", None) or str(int(time.time() * 1000))
    config = {"id": aid, **config}
    shown = _annotate(config)
    if not confirmed:
        return {"pending": True, "automation": shown,
                "note": "Nothing was written. Show this to the person, get a yes, then call "
                        "create_automation again with the same config and confirmed=True."}
    wrote = _rest("POST", f"config/automation/config/{aid}", config)
    if isinstance(wrote, dict) and "error" in wrote:
        return {"error": wrote["error"], "written": False}
    _rest("POST", "services/automation/reload", {})
    for _ in range(10):
        row = next((r for r in _automations() if r.get("id") == aid), None)
        if row:
            return {"ok": True, "id": aid, "entity_id": row["entity_id"], "state": row["state"]}
        time.sleep(0.5)
    return {"ok": True, "id": aid, "note": "written, but it has not shown up yet"}


def delete_automation(automation, confirmed=False):
    """Remove an automation, keeping a backup restore_automation can put back."""
    row = _find_automation(automation)
    if "error" in row:
        return row
    if not row.get("id"):
        return {"error": f"{row['entity_id']} is not editable through the config API"}
    if not confirmed:
        return {"pending": True, "would_delete": row["alias"], "entity_id": row["entity_id"],
                "note": "Nothing was deleted. Get a yes, then call again with confirmed=True."}
    current = _rest("GET", f"config/automation/config/{row['id']}")
    if isinstance(current, dict) and "error" in current:
        return current
    os.makedirs(BACKUPS, exist_ok=True)
    backup = os.path.join(BACKUPS, f"{row['id']}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(backup, "w", encoding="utf-8") as fh:
        json.dump(current, fh, indent=2, ensure_ascii=False)
    got = _rest("DELETE", f"config/automation/config/{row['id']}")
    if isinstance(got, dict) and "error" in got:
        return {"error": got["error"], "backup": backup}
    return {"ok": True, "deleted": row["alias"], "backup": backup}
```

Call the function matching the `action` field. Print the result with
`print(json.dumps(result, indent=2, ensure_ascii=False))`.
