"""Which folder a path may resolve to, and what a delete is allowed to reach.

Run: python web_server/test_recording_paths.py

The stale-tray delete must not destroy the archived copy.

Two phones, one clip. Phone B presses Guardar and the clip moves to its month
folder. Phone A's tray is stale and still offers Borrar for `to_review/X.mp4`.
Before the fix, the resolver missed in to_review/, fell back to hunting the
whole archive by basename, found the copy the household had just chosen to
keep, and deleted it — returning success.
"""
import os, shutil, subprocess, sys, tempfile, types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for heavy in ("cv2",):
    sys.modules.setdefault(heavy, types.ModuleType(heavy))
import numpy  # noqa

root = tempfile.mkdtemp(prefix="cams-dl-")
os.environ["RECORDINGS_DIR"] = root
import recording  # noqa: E402

ok = []
def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- '+str(detail)}")
    ok.append(cond)

os.makedirs(os.path.join(root, "2026-08"), exist_ok=True)
os.makedirs(os.path.join(root, "to_review"), exist_ok=True)
os.makedirs(os.path.join(root, "pending"), exist_ok=True)

kept = os.path.join(root, "2026-08", "2026-08-08_10-00-00_patio.mp4")
with open(kept, "wb") as fh: fh.write(b"x" * 4096)

print("a stale tray card for a clip that was already kept")
gone = recording.delete_recording("to_review/2026-08-08_10-00-00_patio.mp4", "video")
check("the delete does not report success", not gone, gone)
check("and the archived copy is still there", os.path.exists(kept))

print("\nthe staging folders are not the shelf")
with open(os.path.join(root, "to_review", "2026-08-08_11-00-00_patio.mp4"), "wb") as fh:
    fh.write(b"x" * 4096)
with open(os.path.join(root, "pending", "2026-08-08_12-00-00_patio.mp4"), "wb") as fh:
    fh.write(b"x" * 4096)
listed = [r["path"] for r in recording.list_recordings(rec_type="video")]
check("to_review is not listed as a recording",
      not any("to_review" in p for p in listed), listed)
check("nor is pending", not any("pending" in p for p in listed), listed)
check("the real recording still is", any("2026-08" in p for p in listed), listed)
print("\na start-in-progress reservation does not crash the listing")
recording._active_recorders["patio"] = {"starting": True}
try:
    listed = [r["path"] for r in recording.list_recordings(rec_type="video")]
    survived = True
except KeyError:
    listed, survived = [], False
finally:
    recording._active_recorders.pop("patio", None)
check("list_recordings survives a live reservation", survived)
check("and still returns the shelf", any("2026-08" in p for p in listed), listed)

print("\ndeleting a tray clip that really is there takes its sidecar too")
clip = os.path.join(root, "to_review", "2026-08-08_11-00-00_patio.mp4")
with open(clip + ".json", "w") as fh: fh.write('{"what":"solo la luz"}')
check("it deletes", recording.delete_recording("to_review/2026-08-08_11-00-00_patio.mp4", "video"))
check("the clip is gone", not os.path.exists(clip))
check("and no orphan sidecar is left", not os.path.exists(clip + ".json"))

print("\nand a delete cannot leave the recordings tree")
outside = os.path.join(os.path.dirname(root), "precious.mp4")
with open(outside, "wb") as fh: fh.write(b"x" * 4096)
_real_resolve = recording._resolve_path
recording._resolve_path = lambda rel: outside
try:
    check("a path that resolves outside is refused",
          recording.delete_recording("../precious.mp4", "video") is False)
    check("and the file it pointed at is still there", os.path.exists(outside))
finally:
    recording._resolve_path = _real_resolve
    if os.path.exists(outside): os.remove(outside)

# --- the media routes are reachable without a session -------------------------
# Three callers hold no session and cannot get one: the ESP32 wall panels
# (services/proxy/esp32/control/README.md), HomeCore's tile poller, and
# Alfred's camera-feed skill. `require_auth` answers a **302 to the login
# page**, so none of them would report an error -- the panels go blank and the
# skill's raise_for_status() passes on the login page's 200 and writes HTML
# into a .jpg. Putting these behind a session means giving those three a
# credential they can carry first.
print("\nthe media routes are not behind a session")
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "web_server.py"), encoding="utf-8").read().splitlines()
_guards = {}
for _i, _ln in enumerate(_src):
    if not _ln.startswith("@app.route("):
        continue
    _j, _dec = _i + 1, []
    while _j < len(_src) and _src[_j].startswith("@"):
        _dec.append(_src[_j].strip()); _j += 1
    _guards[_ln.strip()] = _dec
for _r in ("@app.route('/snapshot/<camera_key>')",
           "@app.route('/stream/<camera_key>')",
           "@app.route('/h264/<camera_key>')"):
    check(f"{_r.split(chr(39))[1]:24s} answers without one",
          _r in _guards and "@require_auth" not in _guards[_r], _guards.get(_r))
# Only meaningful if it can still tell a protected route from an open one.
check("  and the check can see a route that is protected",
      "@require_auth" in _guards.get("@app.route('/recordings')", []),
      _guards.get("@app.route('/recordings')"))

shutil.rmtree(root, ignore_errors=True)
print()
print("all checks passed" if all(ok) else f"{ok.count(False)} FAILED")
raise SystemExit(0 if all(ok) else 1)
