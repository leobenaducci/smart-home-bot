```python
import subprocess, json, os
from urllib.parse import urlencode

# Same HomeCore host/creds as the chores skill; just a different path.
# No default host: the deployer names the portal from `dns:`, and an invented
# name would be some other household's.
BASE = os.environ.get("TASKS_API_URL", "").replace("/tasks/api", "/devices/api")
USER_ID = os.environ.get("HOMECORE_USER_ID", "")
TOKEN = os.environ.get("HOMECORE_PROXY_TOKEN", "")

def _curl(method, path, data=None, params=None):
    if not BASE:
        return {"error": "The portal's address is not configured (TASKS_API_URL missing)."}
    if not USER_ID or not TOKEN:
        return {"error": "This account has no access to the household's devices (HOMECORE_USER_ID/HOMECORE_PROXY_TOKEN missing)."}
    url = f"{BASE}/{path}"
    if params:
        url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
    # A command waits for the device to answer (up to 8 s on the portal).
    cmd = ["curl", "-sk", "--max-time", "15", "-X", method,
           "-H", f"X-Proxy-Secret: {TOKEN}", "-H", f"X-Proxy-User: {USER_ID}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    try: return json.loads(r.stdout)
    except Exception: return r.stdout.strip() or r.stderr.strip()

def list_devices():
    # Every device I can control: my own, and everyone's for a parent.
    return _curl("GET", "list")

def list_apps(device):
    return _curl("GET", "apps", params={"device": device})

def set_volume(device, level=None, step=None):
    # level 0-100, or step "up" / "down" / "mute" / "unmute". Media volume.
    body = {"device": device, "action": "volume"}
    if step:
        body["step"] = str(step).lower()
    else:
        body["level"] = level
    return _curl("POST", "command", body)

def open_app(device, app):
    return _curl("POST", "command", {"device": device, "action": "open_app", "app": app})

def ring_device(device, seconds=45):
    # One device, not all of a person's: "ring the tablet". Through silent too.
    return _curl("POST", "command", {"device": device, "action": "ring", "seconds": int(seconds)})

def stop_ring_device(device):
    return _curl("POST", "command", {"device": device, "action": "ring_stop"})
```
