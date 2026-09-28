"""The household's devices: which one is meant, who may command it, and
that Alfred is told what the device actually did.

Run: python local/test_devices.py   (needs Flask; skips loudly without it)

Pinned here:

- a device is found by its name, or by words from its name, its owner and its
  kind ("la tablet de Juana"); two that match are a question, not a guess;
- a parent may command anyone's device, anybody else only their own, and a
  device that did not switch remote control on is refused before anything is
  sent;
- an app is matched against what the device reported ("disney" is Disney+,
  "YouTube" is not YouTube Kids), and one it does not have is an error;
- the command names the device, so the owner's other devices ignore it, and
  only a session of the owner can confirm it.

People are the invented household: Tomi and Mora (parents), Juana.
"""
import json
import os
import shutil
import sys
import tempfile
import threading

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-devices-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
_repo_i18n = os.path.join(SRC, "..", "..", "..", "i18n")
if not os.path.isdir(os.path.join(dst, "i18n")) and os.path.isdir(_repo_i18n):
    shutil.copytree(_repo_i18n, os.path.join(dst, "i18n"))
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32, DEBUG_API_KEY="d" * 32,
                  HOMECORE_MEMBERS="user1,user2,user3", HOMECORE_ADMIN_MEMBERS="user1,user2",
                  FAMILY_FILE=os.path.join(dst, "family.json"))

TOMI, MORA, JUANA = "999000111", "999000222", "999000333"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    json.dump([{"username": TOMI, "member": "user1", "nanobot_id": 1},
               {"username": MORA, "member": "user2", "nanobot_id": 2},
               {"username": JUANA, "member": "user3", "nanobot_id": 3}], f)
with open(os.path.join(dst, "family.json"), "w", encoding="utf-8") as f:
    json.dump({"people": [
        {"person": "tomi", "member": "user1", "display_name": "Tomi", "parents": True, "active": True},
        {"person": "mora", "member": "user2", "display_name": "Mora", "parents": True, "active": True},
        {"person": "juana", "member": "user3", "display_name": "Juana", "parents": False, "active": True}],
        "groups": []}, f)

sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_device_db()
A.init_app_devices_db()
A.app.config["TESTING"] = True
A.REMOTE_ACK_WAIT_S = 1
A._ntfy_topic = lambda user: "T" + user
A._tasks_display_name = lambda login: {TOMI: "Tomi", MORA: "Mora", JUANA: "Juana"}.get(login, login)
failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + repr(detail)}")
    if not cond:
        failures.append(label)


class as_:
    """A request as the app sends it: through the chat proxy, which vouches
    for the login server to server."""

    def __init__(self, user):
        self.c, self.h = A.app.test_client(), {"X-Proxy-Secret": "p" * 32, "X-Proxy-User": user}

    def get(self, url):
        return self.c.get(url, headers=self.h)

    def post(self, url, json=None):
        return self.c.post(url, json=json, headers=self.h)


# The device answers the moment a command reaches it, as the app does -- unless
# a test says it is off.
pushes, answering = [], {"on": True, "as": None}


def _push(user, tag, extra=None):
    pushes.append((user, tag, extra or {}))
    if tag == "device_cmd" and answering["on"]:
        e = extra or {}
        reply = {"cmd": e["cmd"], "ok": True,
                 "detail": {"volume": "volume", "open_app": "opened"}.get(e["action"], "ringing")}
        if e["action"] == "volume":
            reply["level"] = e.get("level", 50)
        threading.Timer(0.05, lambda: as_(answering["as"] or user).post("/devices/api/ack", reply)).start()


A._geo_push_control = _push

TABLET, JPHONE, MPHONE = "tablet-0001-aaaa", "jphone-0002-bbbb", "mphone-0003-cccc"
APPS = [{"label": "Disney+", "package": "com.disney.disneyplus"},
        {"label": "YouTube", "package": "com.google.android.youtube"},
        {"label": "YouTube Kids", "package": "com.google.android.apps.youtube.kids"}]

print("registering")
r = as_(JUANA).post("/devices/api/register", {"device_id": TABLET, "name": "Tablet de Juana",
                                             "kind": "tablet", "remote": True, "can_open_apps": True,
                                             "apps": APPS})
check("a device registers itself", r.status_code == 200, r.get_json())
as_(JUANA).post("/devices/api/register", {"device_id": JPHONE, "name": "Celu de Juana", "kind": "phone",
                                          "remote": False, "apps": []})
as_(MORA).post("/devices/api/register", {"device_id": MPHONE, "name": "Pixel", "kind": "phone",
                                         "remote": True, "apps": APPS[:1]})
r = as_(JUANA).post("/devices/api/register", {"device_id": "x", "name": "?"})
check("an id that is not one is refused", r.status_code == 400, r.status_code)

print("\nwho sees what")
mine = {d["name"] for d in as_(JUANA).get("/devices/api/list").get_json()["devices"]}
check("Juana sees her own two", mine == {"Tablet de Juana", "Celu de Juana"}, mine)
every = as_(TOMI).get("/devices/api/list").get_json()["devices"]
check("a parent sees every device, with its owner",
      {(d["name"], d["owner_name"]) for d in every} ==
      {("Tablet de Juana", "Juana"), ("Celu de Juana", "Juana"), ("Pixel", "Mora")}, every)
tab = next(d for d in every if d["id"] == TABLET)
check("and what it can do", tab["remote_control"] and tab["can_open_apps"] and tab["apps"] == 3, tab)

print("\nfinding the device")
for query in ("Tablet de Juana", "la tablet de Juana", "tableta juana", TABLET):
    dev, _c = A._remote_find(TOMI, query)
    check(f"  {query!r} is the tablet", dev and dev["device_id"] == TABLET, dev and dev["name"])
dev, cands = A._remote_find(TOMI, "juana")
check("  'juana' alone is two devices: a question", dev is None and len(cands) == 2, [c["name"] for c in cands])
dev, _c = A._remote_find(TOMI, "el pixel de mora")
check("  'el pixel de mora' is Mora's phone", dev and dev["device_id"] == MPHONE, dev and dev["name"])

print("\nvolume")
pushes.clear()
r = as_(TOMI).post("/devices/api/command", {"device": "la tablet de Juana", "action": "volume", "level": 20})
out = r.get_json()
check("a parent turns Juana's tablet down", r.status_code == 200 and out["device"] == "Tablet de Juana", out)
check("on Juana's channel, naming the tablet, saying who asked",
      pushes and pushes[-1][0] == JUANA and pushes[-1][1] == "device_cmd"
      and pushes[-1][2]["device_id"] == TABLET and pushes[-1][2]["level"] == 20
      and pushes[-1][2]["by"] == "Tomi", pushes)
check("and the tablet's answer comes back", out.get("confirmed") and out["result"]["level"] == 20, out)
r = as_(TOMI).post("/devices/api/command", {"device": TABLET, "action": "volume", "step": "down"})
check("a step works as well as a level", r.get_json().get("confirmed")
      and pushes[-1][2].get("step") == "down" and "level" not in pushes[-1][2], pushes[-1])
r = as_(TOMI).post("/devices/api/command", {"device": TABLET, "action": "volume", "level": "loud"})
check("a volume that is neither is refused", r.status_code == 400, r.status_code)

print("\nopening an app")
for query, label in (("Disney+", "Disney+"), ("disney", "Disney+"), ("disney plus", "Disney+"),
                     ("youtube", "YouTube"), ("youtube kids", "YouTube Kids")):
    pushes.clear()
    r = as_(TOMI).post("/devices/api/command", {"device": TABLET, "action": "open_app", "app": query})
    check(f"  {query!r} opens {label}", r.status_code == 200 and pushes
          and pushes[-1][2].get("label") == label, (r.get_json(), pushes[-1:] and pushes[-1][2]))
pushes.clear()
r = as_(TOMI).post("/devices/api/command", {"device": TABLET, "action": "open_app", "app": "Netflix"})
check("an app it does not have is an error, and nothing is sent",
      r.status_code == 404 and not pushes, (r.status_code, pushes))
apps = as_(TOMI).get(f"/devices/api/apps?device={TABLET}").get_json()
check("the apps it has can be listed", apps.get("apps") == ["Disney+", "YouTube", "YouTube Kids"], apps)

print("\nwho may")
pushes.clear()
r = as_(JUANA).post("/devices/api/command", {"device": "pixel", "action": "volume", "level": 0})
check("Juana cannot reach Mora's phone (she cannot even see it)", r.status_code == 404 and not pushes,
      (r.status_code, pushes))
r = as_(JUANA).post("/devices/api/command", {"device": "tablet", "action": "volume", "level": 50})
check("but she can turn her own tablet up", r.status_code == 200 and r.get_json()["confirmed"], r.get_json())
pushes.clear()
r = as_(TOMI).post("/devices/api/command", {"device": "celu de juana", "action": "open_app", "app": "x"})
check("a device with remote control off is refused before anything is sent",
      r.status_code == 409 and not pushes and "Celu de Juana" in r.get_json()["error"], (r.status_code, pushes))
r = as_(TOMI).post("/devices/api/command", {"device": TABLET, "action": "wipe"})
check("an action nobody offers is refused", r.status_code == 400, r.status_code)
r = as_(TOMI).post("/devices/api/command", {"device": "juana", "action": "ring"})
check("two matching devices come back as candidates", r.status_code == 404
      and len(r.get_json().get("candidates") or []) == 2, r.get_json())

print("\nconfirmation")
answering["on"] = False
r = as_(TOMI).post("/devices/api/command", {"device": TABLET, "action": "ring", "seconds": 9999})
out = r.get_json()
check("a device that does not answer is 'sent, not confirmed'", r.status_code == 200
      and out["confirmed"] is False and "result" not in out, out)
check("and a ring is capped like any other", pushes[-1][2]["seconds"] == A.GEO_RING_MAX_S, pushes[-1][2])
answering.update(on=True, **{"as": MORA})
r = as_(TOMI).post("/devices/api/command", {"device": TABLET, "action": "ring"})
check("an answer from somebody else's session does not count", r.get_json()["confirmed"] is False, r.get_json())
answering["as"] = None

print("\na device moves with its sign-in")
as_(MORA).post("/devices/api/register", {"device_id": TABLET, "name": "Tablet de Juana", "kind": "tablet",
                                         "remote": True, "apps": APPS})
dev, _c = A._remote_find(TOMI, TABLET)
check("signed in as Mora, it is Mora's", dev and dev["login"] == MORA, dev and dev["login"])
check("and Juana no longer sees it", TABLET not in {d["id"] for d in as_(JUANA).get("/devices/api/list").get_json()["devices"]})

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)
