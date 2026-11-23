```python
import requests, json, os, re, time
from pathlib import Path

# The wall's API, from the environment the deployer supplies. This was a
# literal host and :5000 -- the port *inside* that container, while compose
# publishes it on `web_port` -- so it named a machine this household may not
# have and a port nothing listens on outside it.
BASE = os.environ.get("CAMERA_API_URL", "http://cameras.home:21020").rstrip("/")
# Same convention as the document / file-share / paperless skills: anything the
# user should SEE goes in the workspace media dir and travels to the chat as a
# download: link. HomeCore proxies that at /chat/download/<path>, which is
# same-origin https and under the one prefix the cloud proxy forwards — so the
# image renders on the LAN, over the tailnet and off-VPN alike. A path outside
# the workspace (this used to write to /tmp) is reachable by nothing.
WORKSPACE = os.environ.get("NANOBOT_WORKSPACE", os.path.expanduser("~/.nanobot/workspace"))
MEDIA_DIR = Path(WORKSPACE) / "media"


def list_cameras():
    return requests.get(f"{BASE}/api/cameras", timeout=5).json()


def _resolve(which):
    """Map whatever the user said to (camera_id, display name).

    The API keys cameras by a hex camera_id and also answers to their device
    IP, but the family says "el patio". Resolve by name first, then id, then
    IP — and never guess an address: an identifier we cannot find here is a
    404 from the server and an error the user cannot act on.
    """
    cams = list_cameras()
    if not isinstance(cams, dict) or not cams:
        raise RuntimeError("no camera list available")
    want = str(which or "").strip().lower()
    if not want:                                   # only one camera? just use it
        if len(cams) == 1:
            cid = next(iter(cams))
            return cid, cams[cid].get("name") or cid
        raise ValueError("which camera? " + ", ".join(
            c.get("name") or i for i, c in cams.items()))
    for cid, c in cams.items():
        name = str(c.get("name") or "")
        ip = str(c.get("device_ip") or "")
        if want in (cid.lower(), name.lower(), ip.lower(), ip.split(":")[0].lower()):
            return cid, name or cid
    for cid, c in cams.items():                    # loose match: "patio trasero"
        name = str(c.get("name") or "").lower()
        if name and (want in name or name in want):
            return cid, c.get("name") or cid
    raise ValueError("I can't find that camera; there are: " + ", ".join(
        c.get("name") or i for i, c in cams.items()))


def snapshot(camera=None, camera_ip=None, camera_id=None, name=None):
    cid, label = _resolve(camera or camera_id or camera_ip or name)
    resp = requests.get(f"{BASE}/snapshot/{cid}", timeout=15)
    resp.raise_for_status()
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-").lower() or "cam"
    # Timestamped: a fresh snapshot must not reuse a filename the chat already
    # showed, or the browser paints the cached one.
    filename = f"cam_{slug}_{int(time.time())}.jpg"
    (MEDIA_DIR / filename).write_bytes(resp.content)
    rel = f"media/{filename}"
    link = f"[{label}](download:{rel})"
    return {
        # A ready-to-send reply, not just the ingredients — same shape as the
        # `document` skill, which does get its files through. Telling the model
        # to splice `download_link` into a sentence it writes itself does not
        # work: it announces the photo ("here you go 👇") and omits the link.
        # Fifteen snapshots were captured and served correctly, and not one
        # reached the chat, before this field existed.
        #
        # One line, deliberately. With a newline in it the model sent the first
        # half and dropped the link, so the delivery backstop had to rescue the
        # photo as a third message — "Patio, ahora mismo:", then a closing
        # sentence, then the picture alone. A string with no break in it has no
        # half to send.
        "message": f"{label}, ahora mismo: {link}",
        "download_link": link,
        "path": rel,
        "size_bytes": len(resp.content),
        "camera": label,
    }


def detect(camera=None, camera_ip=None, camera_id=None, name=None, with_photo=True):
    """What the camera can see right now, as data — plus the photo by default.

    A YOLO detector on the camera server, not a vision model: it answers in
    ~0.15s where describe_image took 10-40s, and every finding carries a
    confidence. That number is the point. On a dark patio it has reported a
    person who was not there at 0.31 and a pergola as two beds — the same class
    of mistake the vision model made, except here it arrives labelled
    certainty="baja" instead of as a fluent sentence.
    """
    cid, label = _resolve(camera or camera_id or camera_ip or name)
    resp = requests.get(f"{BASE}/api/detect/{cid}", timeout=60)
    resp.raise_for_status()
    result = resp.json()
    result["camera"] = label
    if with_photo:
        # Nearly every question about a camera is better answered with the
        # picture attached — the family can settle a "baja" themselves in a
        # glance. Same shape snapshot returns, so the reply is written once.
        try:
            shot = snapshot(camera=cid)
            result["message"] = shot["message"]
            result["download_link"] = shot["download_link"]
            result["path"] = shot["path"]
        except Exception as exc:
            result["photo_error"] = str(exc)
    return result


def stream_url(camera=None, camera_ip=None, camera_id=None):
    # Plain http on another host: it cannot be embedded in the chat (the page is
    # https, so the browser blocks it as mixed content) and it is not reachable
    # off the LAN. Give it to someone who asked to open the live feed in a
    # browser at home; for "show me the camera", use snapshot.
    cid, label = _resolve(camera or camera_id or camera_ip)
    return {
        "url": f"{BASE}/stream/{cid}",
        "camera": label,
        "type": "mjpeg",
        "note": "LAN only — cannot be shown inline in the chat; use snapshot for that",
    }


# --- managing the wall ---------------------------------------------------
# Adding and removing go through HomeCore, not straight to the wall. The wall
# guards every mutating route with a session (`@require_auth`), and HomeCore is
# already the thing that turns "this member" into that session: it authenticates
# the member and re-signs the hop with X-Proxy-User / X-Proxy-Secret.
#
# The alternative was handing this container PROXY_SHARED_SECRET, which mints a
# session for ANY user — broader than the per-member credentials the deployer
# already refuses to put on the shared assistant. So the member's own token is
# what travels, and nothing here holds a secret it did not already have.
#
# X-Proxy-Lan is what the wall's house-only gate reads. It is true by
# construction: this container runs in the house. Without it HomeCore answers
# 404 "Available only at home or over the VPN", which reads as the wall being
# down rather than as a refusal.
HOMECORE = os.environ.get("HOMECORE_URL", "https://portal.home:21001").rstrip("/")
_HC_USER = os.environ.get("HOMECORE_USER_ID", "")
_HC_TOKEN = os.environ.get("HOMECORE_PROXY_TOKEN", "")


def _homecore(method, path, **kw):
    """Call the camera wall through HomeCore as this member."""
    if not (_HC_USER and _HC_TOKEN):
        # The shared room assistant has no member token, by policy. Say so
        # plainly rather than returning a 404 it cannot act on.
        return {"error": "This assistant cannot manage cameras — it has no "
                         "member credentials. Ask from a member's own Alfred."}
    headers = {"X-Proxy-User": _HC_USER, "X-Proxy-Secret": _HC_TOKEN,
               "X-Proxy-Lan": "1"}
    # verify=False: the portal is self-signed by the household's own CA, the
    # same way every other caller reaches it.
    r = requests.request(method, f"{HOMECORE}/camaras{path}", headers=headers,
                         timeout=15, verify=False, **kw)
    try:
        body = r.json()
    except Exception:
        body = {"status": "error", "message": r.text[:200]}
    body["http_status"] = r.status_code
    return body


def add_camera(device_ip=None, name=None, stream_type="rtsp", port=None,
               stream_path=None, username=None, password=None,
               fps=None, width=None, height=None, rotation=None, **_):
    """Add a camera to the wall. `device_ip` and `name` are what people give."""
    if not device_ip:
        return {"error": "device_ip is required — the camera's address on the "
                         "home network, e.g. 192.168.88.52"}
    payload = {"device_ip": device_ip, "name": name or device_ip,
               "stream_type": stream_type}
    # Only send what was actually given: the wall has defaults for the rest,
    # and a None here would overwrite them with nothing.
    for k, v in (("port", port), ("stream_path", stream_path),
                 ("username", username), ("password", password),
                 ("fps", fps), ("width", width), ("height", height),
                 ("rotation", rotation)):
        if v is not None:
            payload[k] = v
    return _homecore("POST", "/api/cameras", json=payload)


def rediscover_camera(camera=None, camera_ip=None, camera_id=None, dry=False, **_):
    """Find where a camera actually is now and move it there.

    For the case that breaks every camera at once: the router hands out new
    addresses and the wall keeps dialling the old ones, so a camera that is
    powered on and streaming reads as `error`. The wall scans the subnet and
    asks each RTSP port whether this camera's credentials open it — only the
    right camera can say yes — then repoints it, keeping its id, its motion
    zones and its recording rules.

    Takes the name, as everything here does. `dry=True` reports where it is
    without moving it.
    """
    which = camera or camera_id or camera_ip
    if not which:
        return {"error": "name the camera to look for"}
    # The wall resolves names itself now, so pass it through rather than
    # resolving here: a camera that has moved may not be in the local list the
    # way _resolve() expects, and this is exactly the case that matters.
    q = "?dry=1" if dry else ""
    out = _homecore("POST", f"/api/cameras/{which}/rediscover{q}")
    return out


def remove_camera(camera=None, camera_ip=None, camera_id=None, **_):
    """Remove a camera. Accepts the name people use, its id, or its IP."""
    which = camera or camera_id or camera_ip
    if not which:
        return {"error": "name the camera to remove"}
    # Resolve locally first so the confirmation names what the household calls
    # it, and so a typo fails here with the list of real names rather than as a
    # 404 from the wall.
    cid, label = _resolve(which)
    out = _homecore("DELETE", f"/api/cameras/{cid}")
    out["camera"] = label
    return out
```

Call the function matching the `action` field, passing any extra keys as arguments. Print the result with `print(json.dumps(result, indent=2, ensure_ascii=False))`.
