"""The recording window, per camera.

Run: python web_server/test_recording_window.py

The window used to be one setting for the whole house, which is the wrong
shape for it: the patio is worth recording overnight and the living room is
not, and with a single window you either record the living room all night or
lose the patio.

The awkward part is what a *cleared* setting means. A merge cannot express "go
back to the house default" — the key it would have to remove is the one it
keeps — so clearing replaces the record, and a replace that dropped `camera_id`
made `get_registry_camera_settings` re-key the record to a fresh id and orphan
it. That looked exactly like a save that silently did nothing, and it is what
the last two checks here exist for.
"""
import os, sys, tempfile, types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# is_recording_enabled imports get_timezone from recording; recording imports cv2.
fake = types.ModuleType("recording"); fake.get_timezone = lambda: None
sys.modules.setdefault("recording", fake)
for heavy in ("cv2", "numpy"):
    sys.modules.setdefault(heavy, types.ModuleType(heavy))
import settings_manager as SM

# The config dir goes in the constructor. `SettingsManager` builds every file
# path in __init__, so assigning `config_dir` afterwards changes nothing and the
# writes land in the repo's own config/ — which is how an earlier run of this
# test overwrote the house's real recording window.
tmp = tempfile.mkdtemp()
sm = SM.SettingsManager(config_dir=tmp)


# `is_recording_enabled` does `from datetime import datetime` inside itself, so
# patching the module does nothing. Test against the real clock instead: pick
# windows relative to the hour it actually is, which is what the code reads.
from datetime import datetime
NOW = datetime.now().hour
IN = (NOW, (NOW + 1) % 24)              # a window this hour is inside
OUT = ((NOW + 2) % 24, (NOW + 3) % 24)  # one it is not
# Real camera keys are 32-hex camera_ids; `get_registry_camera_settings`
# re-keys anything else to a fresh one on read, so a made-up name would be
# migrated out from under the test and prove nothing.
PATIO = "a" * 32
LIVING = "b" * 32
ok = []
def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- '+str(detail)}")
    ok.append(cond)

sm.save_global_settings({'recording_enabled': True,
                         'recording_start_hour': OUT[0], 'recording_end_hour': OUT[1]})
print(f"it is {NOW}:xx, and the house window is {OUT[0]}-{OUT[1]}")
check("the house says no", sm.is_recording_enabled() is False)
check("a camera with no window of its own follows the house",
      sm.is_recording_enabled(PATIO) is False)

sm.set_camera_recording_window(PATIO, start_hour=IN[0], end_hour=IN[1])
check("a camera whose own window includes now says yes",
      sm.is_recording_enabled(PATIO) is True)
check("and the house is unchanged", sm.is_recording_enabled() is False)
check("another camera still follows the house",
      sm.is_recording_enabled(LIVING) is False)

print("\nturning one camera off does not touch the others")
sm.set_camera_recording_window(PATIO, start_hour=IN[0], end_hour=IN[1], enabled=False)
check("that camera is off", sm.is_recording_enabled(PATIO) is False)

print("\nclearing it goes back to the house window")
sm.set_camera_recording_window(PATIO, start_hour=None, end_hour=None, enabled=None)
check("no leftovers", sm.get_camera_recording_window(PATIO) == {},
      sm.get_camera_recording_window(PATIO))
check("and it follows the house again", sm.is_recording_enabled(PATIO) is False)

print("\nhalf a window is ignored rather than half-applied")
sm.set_camera_recording_window(LIVING, start_hour=IN[0])
check("still the house window", sm.is_recording_enabled(LIVING) is False)

print("\nan hour that is not an hour is refused")
try:
    sm.set_camera_recording_window(LIVING, start_hour=99, end_hour=IN[1])
    check("refused", False, "it accepted 99")
except ValueError as e:
    check("refused, and says why", "entre 0 y" in str(e), e)

# 24 is a fine *end* — the house default is 0-24 — but as a start it is an
# hour the clock never reaches, so `24 → 0` reads like a whole night and
# silently means "never record".
try:
    sm.set_camera_recording_window(LIVING, start_hour=24, end_hour=0)
    check("a window starting at 24 is refused", False, "it accepted 24")
except ValueError as e:
    check("a window starting at 24 is refused", "0 y 23" in str(e), e)
sm.set_camera_recording_window(LIVING, start_hour=0, end_hour=24)
check("but 24 is still a valid end",
      sm.get_camera_recording_window(LIVING).get('recording_end_hour') == 24,
      sm.get_camera_recording_window(LIVING))
sm.set_camera_recording_window(LIVING, start_hour=None, end_hour=None)

# A camera added by hand lives in cameras.json, not in the registry overlay.
# Its window used to be written to one store and read from the other, so it
# was saved, echoed back to the page, and then ignored by the recorder.
print("\na camera added by hand keeps its own window too")
HAND = "c" * 32
sm.save_cameras({HAND: {'camera_id': HAND, 'name': 'Patio', 'device_ip': '10.0.0.9'}})
sm.set_camera_recording_window(HAND, start_hour=IN[0], end_hour=IN[1])
check("the window comes back",
      sm.get_camera_recording_window(HAND) ==
      {'recording_start_hour': IN[0], 'recording_end_hour': IN[1]},
      sm.get_camera_recording_window(HAND))
check("and the recorder honours it", sm.is_recording_enabled(HAND) is True)
check("without losing the rest of the camera",
      sm.get_cameras()[HAND].get('device_ip') == '10.0.0.9', sm.get_cameras()[HAND])
sm.set_camera_recording_window(HAND, start_hour=None, end_hour=None, enabled=None)
check("and clearing it goes back to the house",
      sm.get_camera_recording_window(HAND) == {} and sm.is_recording_enabled(HAND) is False,
      sm.get_camera_recording_window(HAND))

# Reading the registry overlay used to write its records over cameras.json,
# which took every hand-added camera with it.
print("\nreading the registry overlay leaves the camera list alone")
sm.update_registry_camera_setting('192.168.1.77', {'name': 'Timbre'})
sm._registry_cache = None
sm._cameras_cache = None
sm.get_registry_camera_settings()
sm._cameras_cache = None
check("the hand-added camera is still there", HAND in sm.get_cameras(), sm.get_cameras())

# The getter/setter pair kept drifting a field at a time: the window resolved
# the store by hand while the duration wrote registry-only, so on a hand-added
# camera the duration was saved where its own getter never looked. Both go
# through `set_camera_setting` now.
print("\nevery per-camera setting writes where its getter reads")
sm._cameras_cache = None
# Set both, so the test says something about them coexisting — an earlier check
# above deliberately cleared this camera's window.
sm.set_camera_recording_window(HAND, start_hour=IN[0], end_hour=IN[1])
sm.set_camera_recording_duration(HAND, 42)
check("a hand-added camera's duration comes back",
      sm.get_camera_recording_duration(HAND) == 42,
      sm.get_camera_recording_duration(HAND))
check("and setting it left the window alone",
      sm.get_camera_recording_window(HAND).get('recording_start_hour') == IN[0],
      sm.get_camera_recording_window(HAND))
check("and the name and address survived both",
      sm.get_cameras()[HAND].get('name') == 'Patio'
      and sm.get_cameras()[HAND].get('device_ip') == '10.0.0.9',
      sm.get_cameras().get(HAND))
sm.set_camera_recording_duration(HAND, None)
check("clearing it really clears it",
      sm.get_camera_recording_duration(HAND) is None,
      sm.get_camera_recording_duration(HAND))
check("and the camera is still there afterwards", HAND in sm.get_cameras(), sm.get_cameras())

# A preset is a window with a name, so that "the cameras outside" is one
# setting rather than the same pair of hours copied onto each of them.
print("\nthe named presets")
check("outside is all day by default",
      sm.get_recording_presets()['outside'] == {'start_hour': 0, 'end_hour': 24},
      sm.get_recording_presets())
check("inside is 23 to 6 by default",
      sm.get_recording_presets()['inside'] == {'start_hour': 23, 'end_hour': 6},
      sm.get_recording_presets())

# Point the presets at windows defined relative to the real clock, so these
# checks say something definite whatever time the suite is run at.
sm.set_recording_preset('outside', IN[0], IN[1])
sm.set_recording_preset('inside', OUT[0], OUT[1])
check("the house window survived editing a preset",
      sm.is_recording_enabled() is False)

OUTSIDE_CAM = "d" * 32
sm.save_cameras({OUTSIDE_CAM: {'camera_id': OUTSIDE_CAM, 'name': 'Patio'}})
sm.set_camera_recording_window(OUTSIDE_CAM, preset='outside')
check("a camera on a preset follows the preset, not the house",
      sm.is_recording_enabled(OUTSIDE_CAM) is True)
check("and the preset comes back on read",
      sm.get_camera_recording_window(OUTSIDE_CAM).get('recording_preset') == 'outside',
      sm.get_camera_recording_window(OUTSIDE_CAM))

sm.set_camera_recording_window(OUTSIDE_CAM, preset='inside')
check("moving it to the other preset moves its hours",
      sm.is_recording_enabled(OUTSIDE_CAM) is False)

# Editing the preset has to move every camera that follows it — that is the
# whole point of naming it rather than copying the hours.
sm.set_recording_preset('inside', IN[0], IN[1])
check("editing a preset moves the cameras following it",
      sm.is_recording_enabled(OUTSIDE_CAM) is True)
sm.set_recording_preset('inside', OUT[0], OUT[1])

# Its own hours win, so a camera can be lent a window for a week without
# being taken off its preset and forgotten there.
sm.set_camera_recording_window(OUTSIDE_CAM, start_hour=IN[0], end_hour=IN[1],
                               preset='inside')
check("a camera's own hours beat its preset",
      sm.is_recording_enabled(OUTSIDE_CAM) is True)
check("and it is still on the preset underneath",
      sm.get_camera_recording_window(OUTSIDE_CAM).get('recording_preset') == 'inside')

sm.set_camera_recording_window(OUTSIDE_CAM, start_hour=None, end_hour=None,
                               preset='inside')
check("clearing the hours drops back to the preset, not the house",
      sm.is_recording_enabled(OUTSIDE_CAM) is False)

sm.set_camera_recording_window(OUTSIDE_CAM, preset=None)
check("clearing the preset goes back to the house",
      sm.get_camera_recording_window(OUTSIDE_CAM) == {},
      sm.get_camera_recording_window(OUTSIDE_CAM))

print("\na preset that is not a preset is refused")
try:
    sm.set_camera_recording_window(OUTSIDE_CAM, preset='garden-shed')
    check("refused", False, "it accepted an unknown preset")
except ValueError as e:
    check("refused, and lists the real ones",
          'inside' in str(e) and 'outside' in str(e), e)

# A camera pointed at a preset that has since been deleted must not quietly
# inherit the house window as though nothing were wrong.
sm.set_camera_setting(OUTSIDE_CAM, {'recording_preset': 'ghost'})
check("a camera on a deleted preset falls back to the house",
      sm.is_recording_enabled(OUTSIDE_CAM) is False)
sm.set_camera_recording_window(OUTSIDE_CAM, preset=None)

print("\na settings file that only customised one preset keeps the other")
partial = dict(sm.get_global_settings())
partial['recording_presets'] = {'inside': {'start_hour': 1, 'end_hour': 2}}
sm.save_global_settings(partial)
check("the customised one is kept",
      sm.get_recording_presets()['inside'] == {'start_hour': 1, 'end_hour': 2},
      sm.get_recording_presets())
check("and the untouched one is still there",
      sm.get_recording_presets()['outside'] == {'start_hour': 0, 'end_hour': 24},
      sm.get_recording_presets())

print()
print("all checks passed" if all(ok) else f"{ok.count(False)} FAILED")
raise SystemExit(0 if all(ok) else 1)
