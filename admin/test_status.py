"""The service status page: three questions, answered separately.

Run: python admin/test_status.py

"Outdated" is not one thing, and a dashboard that folds it into one word is a
dashboard that lies in whichever direction is most convenient. A service can be:

    pending   a setting changed and nothing deployed it -- the only one of the
              three that means somebody has to act
    stale     the image tag moved since this container was created, so it is a
              build nobody replaced. Two units building `alfred-nanobot:latest`
              from different contexts do exactly this, and both are "running".
    behind    a file under the service's directory is newer than the container.
              Normal on a machine somebody is working on, which is why it is
              reported last and quietly.

None of them is health, and health is none of them: a container can be up,
healthy and three commits behind, or current and crash-looping. So they are
reported side by side and this test refuses to let them collapse.

The container-to-service match is the other thing worth pinning. The deployer
names a compose project `<service>-<unit>`, and half the services have a dash in
their own name -- splitting `home-core-local` on the last dash gives `home`,
which matches nothing and reports every service as having no containers at all.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ["HOME_STACK_STATE_DIR"] = tempfile.mkdtemp(prefix="status-")

import app as A  # noqa: E402

FAILED = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  <- {detail}"))
    if not ok:
        FAILED.append(label)


# --- the three signals stay three -------------------------------------------

print("a row answers the three questions separately")
_row_keys = {"name", "containers", "running", "unhealthy", "pending", "stale",
             "behind", "age", "completed", "description"}
_rows = A.service_status()
check("  it produced rows at all", bool(_rows),
      "no services enabled, so every check below would pass by vacancy")
if _rows:
    check("  each row carries all three plus health",
          _row_keys <= set(_rows[0]), sorted(_rows[0]))
    check("  pending is its own boolean", isinstance(_rows[0]["pending"], bool))
    check("  stale names the containers, not a count",
          isinstance(_rows[0]["stale"], list), _rows[0]["stale"])
    check("  unhealthy names them too",
          isinstance(_rows[0]["unhealthy"], list), _rows[0]["unhealthy"])

# --- the match that a dash would have broken --------------------------------

print("\nservices with a dash in the name still find their containers")
# `home-core-local` split on the last dash is `home`, which is not a service.
# The projects are built from the manifest's own names instead.
_manifest = A.load_manifest().get("services", {})
_dashed = [n for n in _manifest if "-" in n]
check("  the manifest has such services", bool(_dashed), sorted(_manifest)[:5])
_by_name = {r["name"]: r for r in _rows}
_checked = [n for n in _dashed if n in _by_name and _by_name[n]["containers"]]
check("  and at least one of them matched containers", bool(_checked),
      f"{[n for n in _dashed if n in _by_name]} all reported zero containers, "
      f"which is what a bad project match looks like")

# --- pending comes from the admin page's own record -------------------------

print("\npending is the admin page's own pending-deploy record")
A.save_pending(["home-core"])
try:
    _p = {r["name"]: r["pending"] for r in A.service_status()}
    check("  a service with a saved change is pending", _p.get("home-core") is True, _p)
    check("  and one without is not",
          any(v is False for v in _p.values()), _p)
finally:
    A.save_pending([])

print("\nand a service nobody enabled is not reported at all")
_names = {r["name"] for r in A.service_status()}
_cfg_services = (A.load_config().get("services") or {})
_off = [n for n, v in _cfg_services.items()
        if isinstance(v, dict) and v.get("enabled") is False]
check("  disabled services are absent",
      not (set(_off) & _names),
      f"{sorted(set(_off) & _names)} are switched off but listed")

# --- the helpers survive a machine with no docker ---------------------------

print("\nthe page does not need docker to render")
# The admin container talks to a socket that can be missing, and a status page
# that raises is worse than one that says it can see nothing.
_real = A.subprocess.run
A.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("docker"))
try:
    check("  docker missing gives an empty container list", A._docker_ps() == [])
    check("  and an unreadable image id is empty", A._image_id("x:latest") == "")
    check("  and container facts are empty", A._container_facts("x") == {})
finally:
    A.subprocess.run = _real


# --- a container that finished is not a container that broke -------------------
# Every compose project here has one-shot `init-dirs` containers: they make a
# directory and stop, and `depends_on: service_completed_successfully` is the
# whole point of them. Counting an `Exited (0)` as trouble had this page
# reporting «nanobot 7/8, 1 unhealthy» about a stack with nothing wrong with it.
# A dashboard that cries wolf on a healthy house is one nobody reads on the day
# it is right.

print("\nan Exited (0) is finished, not broken")
check("  a clean exit is clean", A._exited_clean("Exited (0) 3 hours ago"))
check("  a failing one is not", not A._exited_clean("Exited (1) 2 weeks ago"))
check("  and neither is a killed one", not A._exited_clean("Exited (137) 4 months ago"))
check("  a running container is not an exit at all",
      not A._exited_clean("Up 2 hours (healthy)"))
# Fails safe: a state this does not understand is reported rather than hidden,
# because the cost of a false alarm is somebody looking, and the cost of a
# missed one is nobody looking.
check("  an unreadable status counts as not clean", not A._exited_clean(""))
check("  and so does a shape this has never seen",
      not A._exited_clean("Exited (weird)"))

print("\nthe rows come out with the urgent ones first")
_rows = A.service_status()


def _rank(r):
    if r["unhealthy"] or (r["containers"] and not r["running"]):
        return 0
    if r["pending"]:
        return 1
    if r["stale"]:
        return 2
    if not r["containers"]:
        return 3
    return 4


_ranks = [_rank(r) for r in _rows]
check("  never descending", _ranks == sorted(_ranks),
      f"{[(r['name'], _rank(r)) for r in _rows]} -- alphabetical is right for a "
      f"list you look something up in and wrong for one you are checking")

print("\nhow long ago, in words somebody uses")
check("  minutes under an hour", A._since(300) == "5 min", A._since(300))
check("  hours under two days", A._since(3600 * 5) == "5 h", A._since(3600 * 5))
check("  days past that", A._since(3600 * 122) == "5 d", A._since(3600 * 122))
# 122.1 was what this printed. Hours stop being a unit somebody reads at about
# two days, and nothing here needs to be exact.
check("  and never a bare float", "." not in A._since(3600 * 122))

# Everything above has to be above this. Twice in one day a block of checks was
# appended after the exit and printed FAIL into a run that exited 0 -- once in
# admin/test_templates.py, and then again here in the next file, by the same
# hand that had just read the fix. The gate is the last thing in the file.
print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed")
