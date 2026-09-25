# Core Backend - AI Agents Documentation & Change Log

This file tracks all changes made by AI agents specifically to the Core Backend (web_server/ directory).

### [2026-09-03] Snapshots outlived the recordings they belonged to

Deleting a video left its snapshots behind. There are three or four per clip and
only one of them shares the clip's name -- both are written from the same motion
event with the same timestamp -- while the rest land on `save_snapshot`'s own
5-second cooldown, under names of their own. Nothing ever collected those.

It showed when the household emptied the month folders: every video went, and
**31,451 JPEGs and 29 GB stayed**, which is as much disk as the video had been.

Two halves, deliberately split:

- `delete_recording` now also removes the snapshot that shares the clip's exact
  name. That one is unambiguously this clip's, so it can go on the spot, next to
  the `.json` sidecar that already travelled with it.
- `purge_orphan_snapshots()` takes the rest, as a sweep in the 60-second
  maintenance loop rather than as more work on the delete path. It asks "is
  there any clip that could own this", which is the question that stays right
  when clips overlap, when one is binned by the reviewer or aged out by
  retention instead of deleted by hand, and when a delete half finished.
  Deleting a neighbour's snapshots is not recoverable, and this cannot do it.

A snapshot is owned when the same camera has a clip that started no more than
`SNAPSHOT_ORPHAN_WINDOW` seconds before it (300, against a default clip of 10).
The window is far wider than any clip on purpose, because of which way it errs:
it decides what is *kept*, so generous leaves a few stale JPEGs and tight
deletes a snapshot whose video is still on the shelf.

Bounded like `purge_old_recordings`: `YYYY-MM` folders only -- never `pending`,
never `to_review`, never a directory somebody adds later -- and a filename it
cannot parse is never touched, because a sweep that deletes what it cannot read
is a sweep that deletes whatever the next rename produces.

Dry-run against the live tree before shipping: 31,450 of 31,458 to delete
(28.48 GB), the 8 kept being exactly the ones with a surviving clip beside them.

### [2026-09-03] The reviewer is told what the detector found, and what is scenery

The vision model got three stills and a generic instruction, and answered the
way anything does when asked to find meaning in noise: "A small object moved on
the ground", "Shadow of tree moved across patio". Neither is an event. Both are
the model describing scenery, because nothing in the prompt told it what the
clip was supposed to be about -- and it was being asked to describe, classify
and judge in one shot, so hedging was the safest thing it could do.

The recorder's detector already knows: it is why the clip exists at all now. So
`prompt_for(filename)` reads the tags off the filename -- the same grammar
`living_tags_in` parses -- and writes them into the prompt:

    The camera's own object detector has already been through these frames and
    followed each of these across several frames, so they really are in the clip:

        person, car

    LOOK AT THOSE ... did they DO something, or were they merely present?

    IGNORE EVERYTHING ELSE ... plants, branches and leaves; the date and time
    burned into the corner or down the edge of the picture; chairs, tables,
    pots, washing; shadows and changing light; rain or drops on the lens;
    insects near the lens; grain at night.

An untagged clip gets the other half, which says the detector found nothing and
that this is evidence but not proof -- it can miss somebody in the dark or at
the far end of a garden -- so the burden runs the other way rather than the
frames being declared empty.

`DETECTOR_TAGS` includes vehicles where `LIVING_TAGS` deliberately does not:
that set decides what may veto a deletion, and a parked car tags a quarter of
the tray. This one is only ever *told* to the model, and knowing a car was found
is exactly what lets it rule on whether the car did anything.

Two things this had to get right and nearly did not:

- **`str.replace`, not `str.format`.** The prompt carries a literal JSON answer
  template, so every brace in it is a format field: `.format()` raises
  `KeyError('"keep"')` on the one part of the prompt that must never change.
- **The single-frame retry passed no filename**, so a clip that did not fit
  would silently have been reviewed under the "found nothing" half -- the
  wrong prompt, on the clips already least legible.

Measured live against qwen3-vl on two real frames from this house: the tagged
one came back `kind=static, keep=false, "car parked still"`, the untagged one
`"No change in scene or objects"`. Both correct, both in the vocabulary.

Its own suite was a coin toss and it had nothing to do with any of this.
`test_clip_review.py` switches the reviewer's wait-for-a-quiet-GPU off before
importing the module, because the filing checks are about which folder a clip
lands in and not about scheduling -- but it did that with
`os.environ.setdefault`, and the deployment sets `CLIP_REVIEW_GPU_MAX_WAIT=900`.
Inside the container, which is the only place the suite can run, the default
never applied: the wait stayed on, the filing checks timed out against a card
the live detectors were using, and a different handful failed on each run. Three
runs here failed three different subsets before the pattern was visible. It is
an assignment now, which is what the comment above it always said it was.

### [2026-09-03] YOLO decides what is recorded, instead of naming the file afterwards

Motion started a recording; YOLO ran, and its answer was used to build the
filename and draw a box. Nothing checked it. Measured over the six days the
retention window holds (2026-08-29..09-03): **4,706 of 8,650 stored clips --
54% -- carried no object tag at all.** No person, no vehicle, no animal.
Shadows, headlight sweep on a wall, rain on the lens, moths at the IR lamp,
night grain. Front and Patio accounted for 4,695 of them.

Snapshots were the same hole and the bigger half by count: 31,445 files and
28.5 GB against the video's 29.9 GB, one every 5 seconds of motion, through the
same missing check, and the reviewer never looks at them. Six days came to
61 GB.

A recording now starts only once `ObjectTracker` has held a target class across
`TRACK_CONFIRM_FRAMES` frames (3, and motion is processed at 5 fps, so 0.6s).
This costs no footage: the ring buffer keeps filling for as long as the camera
is not recording, so `drain_ring_buffer()` still hands over the 5 seconds
before the object was confirmed. Snapshots are behind the same gate.

**Why a tracker and not just "did YOLO see anything".** person, cat and dog run
at a 0.20 confidence threshold, chosen for recall on a dark garden. At that
threshold a single frame is close to a coin toss, and a single frame was all it
took to write a clip. Requiring the *same* object across three frames keeps the
low threshold -- which is what finds someone at the end of the garden -- while
refusing to act on any one frame's opinion.

**Not ultralytics' tracker.** `model.track(persist=True)` keeps state on
`model.predictor`, and `web_server` calls plain `detect_objects()` on the same
model for the "show all objects" overlay and for scene description. A
`predict()` between two `track()` calls resets that predictor, track ids
restart, and a track that restarts never reaches three frames -- which fails
*closed*, silently, because a recording that never starts logs nothing.
`object_tracker.py` associates by IoU instead: no GPU, no shared state, and it
runs in a test on a machine with no card.

**Association is not overlap alone, and that was nearly the bug.** At 5 fps a
walking person moves a good part of their own width between frames: a 40x80 box
stepping 25px overlaps itself by IoU 0.26, under any sane threshold. The first
version of this matched on overlap only and the walker was never followed. A
pair now also matches when the centre moved less than one box length.
`test_overlap_alone_would_not_have_managed_it` pins it.

`detected_objects` still carries everything YOLO saw each frame, so overlays,
logs and MQTT are unchanged; only `confirmed_objects` gates the disk.

### [2026-09-03] The default zone was watching the camera's own clock

A camera with no hand-drawn zones got a "Full Frame" zone inset by 1% -- 13px
of 1296. These cameras burn a timestamp into the picture, and it reprints every
second: motion, in the same place, forever, inside the zone.

Both busy cameras are mounted rotated, so the strip runs *vertically* down an
edge -- and a different edge on each: left on Front, right on Patio, about 58px
(4.5%) in both. A margin on one guessed corner would be right for one camera
and wrong for the other, so `full_frame_zone()` insets every edge, by
`MOTION_CLOCK_MARGIN` (5%). It lives in `motion_detector.py` beside
`MotionZone`, replacing two copies of the literal in `web_server.py`.

With the gate above, the clock can no longer cause a recording on its own -- a
clock is not a person, a vehicle or an animal. The margin still earns its place
by keeping YOLO from being woken once a second, every second, on a card the
detectors and the assistant are both using.
### [2026-08-27] The camera watchdog no longer restarts every camera at boot

A camera whose stream has not delivered a frame yet -- the seconds right after
the container starts, or a camera mid-reconnect -- was judged by
`current_time - 0`, an age of about fifty-six years. The watchdog's
"no fresh frame" check then restarted that camera immediately, and every
cooldown after, on top of whichever camera genuinely stalled. Measured right
after a deploy: three cameras restarted at once, two of them healthy and
processing frames. The churn (stream teardown, RTSP reconnect, recording
finalise) coincided with the recordings page hanging on its video listing for
~90 seconds.

A camera that has never delivered a frame now gets the full
`STUCK_REGION_TIMEOUT` grace period before a restart is considered; a camera
that delivered frames and then stalled behaves exactly as before.
### [2026-08-27] The recordings page no longer 500s while a clip is starting

`/api/recordings` crashed with `KeyError: 'filepath'` whenever a camera sat in
the recording-start window. `start_recording()` holds a placeholder --
`{'starting': True}` -- in `_active_recorders` from the moment it takes the
slot until ffmpeg has spawned, and `list_recordings()` built its in-progress
set as `{rec['filepath'] for rec in ...}` over every entry, reservations
included. With cameras recording continuously, the recordings page (which
fetches the snapshot and video lists at once) 500ed on nearly every load --
"Error 500 Api" on a stack healthy everywhere else.

The in-progress set now skips reservations, the same way
`add_frame_to_recording` and `_finalize_recording` already tell a reservation
from a live recorder.

`test_recording_paths.py` now runs a listing while a reservation is in place
and asserts it survives.

### [2026-08-10] Two named schedules, so "the cameras outside" is one setting

A camera could follow the house window or carry its own pair of hours, which
meant every camera that wanted the same hours as another kept its own copy of
them, and changing your mind meant editing each one.

There are named schedules now — **outside**, all day, and **inside**, 23 to 6,
crossing midnight the way any other window here does. A camera points at one
instead of copying its hours, so editing the schedule moves every camera on it.
Both are editable in Settings; the defaults are only where a house that never
touches them starts.

The recorder resolves in this order: **off → the camera's own hours → its
preset → the house.** Own hours beating the preset is the useful way round: a
camera can sit on `inside` with the rest and still be lent its own window for a
week without being taken off the preset and forgotten there — clearing the
hours drops it back onto the preset rather than onto the house.

Details worth knowing:

- `get_recording_presets()` merges per preset rather than taking the stored
  object wholesale. A settings file that had only ever customised `inside`
  would otherwise come back with no `outside` at all, and a camera pointing at
  a preset that does not exist has no window to follow — which resolves to the
  house window and looks, from the outside, exactly like a setting nothing
  acted on. When that does happen it is logged rather than silently absorbed.
- Choosing a preset clears the camera's own hours, and choosing hours clears
  the preset. Leaving either behind would decide the hours later, at the moment
  the other was cleared, which is a long way from where anyone would look.
- **`/api/global/settings` validated nothing.** It merged whatever arrived
  straight into the settings file, so the house window accepted hour 99 — the
  per-camera route has refused that since the window was split per camera and
  this one never did. Both the house hours and the preset windows go through
  `validate_recording_hour` now. Fixed here because an unchecked preset is
  worse than an unchecked house window: every camera following it moves at
  once, to hours that are never shown on the camera's own page.

`test_recording_window.py` covers the resolution order, that editing a preset
moves the cameras on it, that an unknown preset is refused and says which ones
are real, that a deleted preset falls back to the house, and that a partially
customised settings file keeps the preset it did not mention.

### [2026-08-10] The camera grid, sized by what the cameras actually send

Rotated cameras and small ones both left the dashboard full of holes. Three
separate causes, all in `dashboard.html`:

- **Fixed pixel widths in a wrapping flex row.** Each card was
  `resolution / 2.5`, clamped — 432px for a rotated 1080p camera, 768px for an
  upright one. Whatever did not divide evenly into the row was dead space at
  the right-hand end of every row.
- **Portrait and landscape cards were wildly different heights.** A rotated
  camera became a 432×768 tile standing beside a 768×432 one, and
  `align-items: flex-start` left the space beneath the short one empty.
- **The picture box ignored the camera.** It was hardcoded to `16/9`, or `9/16`
  when rotated. cam2 is 640×480, so `object-fit: contain` pillarboxed it inside
  a box it could never fill.

Cards now carry their effective ratio as `--ar` — already swapped for rotation —
and grow in proportion to it, which is what makes a row come out level: a 16∶9
tile needs 3.2× the width of a 9∶16 one to stand the same height, and that is
exactly the ratio of their `--ar` values. The picture box *is* `var(--ar)`, so
`contain` has nothing left to letterbox. Rows fill exactly instead of ending
ragged, and adding a camera reflows the row rather than leaving a gap.

`--tile-max-h` only exists to stop a single card on the last row growing to the
full page width, and it has to be generous or it defeats the layout: at 420px
the house's current three cameras capped out and left **11% of a 1400px row
empty**, which is the bug. 560px fills the row at three cameras and at four, on
a wide screen and a narrow one.

Both card builders were changed — the Jinja loop and the JS refresh — so a card
that arrives with the page and one built by a later refresh are laid out
identically. Both fall back to 16∶9 when a camera has not reported its geometry,
which also keeps a zero dimension from setting every tile to `NaN`.

### Clip review: a category, and one clip that is binned

The verdict carries `kind` — one word off a closed list — so the recordings
page can be filtered by what is in a clip rather than by reading a sentence.
`_ask_model` maps anything unrecognised to `otro` and every failure path
returns an empty string, so no branch can produce the one word that deletes.

`is_just_a_static_scene()` is that decision, in one place: the model answered,
said not to keep it, named `estatico`, and the filename carries none of
`LIVING_TAGS` — what the recorder's own detector saw at the trigger, which is a
different model looking at full-resolution live frames. It is vetoed by that
second opinion because in this house's tray 28 of 693 clips carry one of those
tags, and those are precisely the clips where the two judges disagree.

The prompt was rewritten around what *changed* rather than what is in frame.
Asked the old way, qwen3-vl kept "un vehículo negro estacionado en el jardín";
asked the new way it calls the same clip `estatico`. Three rules earned their
place by measurement, each after watching it get something wrong:

- the clock in the corner is `estatico`, not `luz` — it was the largest single
  bucket in the tray and the model was reading it as a lighting change;
- a person or an animal in frame is never `estatico` however still they are; a
  dog lying on the grass was binned once during calibration and is not now;
- a car that "se movió ligeramente" and is in the same place at the end did
  not move — that is sensor noise, and the model was rationalising it into an
  event for 16 of 45 tray clips.

Measured on 45 clips from each side, then 30 more of the tray: nothing on the
shelf was binned in any run; the tray's static share went 44% → 56% as those
rules landed. What the model is unsure about still goes to the tray, which is
where it went before.

Tests: `test_clip_review.py` covers each way the rule could widen, and
`test_recordings_page.py` drives the page in headless Chromium through the
DevTools protocol — the filter, the counts, the chips, the select-what-is-shown
button and the tray's purge request.

### Clip review: frames now fit the model's context

Every clip from a 1080p camera was failing with `request (4365 tokens) exceeds
the available context size (4096 tokens)`. Three full-resolution frames do not
fit, so each one was counted as unreviewable and therefore kept: the filtering
could not work, and until the verdicts reached the recordings page there was
nothing to see it in.

Frames are capped at 768px on the longest side (`CLIP_REVIEW_FRAME_PX`), which
is what fits rather than a judgement about quality — the question is "person or
branch", not "read the number plate". Measured on a real 1080p clip: 180 KB of
JPEG down to 27 KB, 6.5s, correct reading.

Errors on the card are sentences now ("the images do not fit the model")
with the raw body on hover, and a request that still overflows retries once
with a single frame.

### Clip review: context headroom, and a second opinion

Three capped frames come to ~2350 tokens against ollama's default 4096 — it
fits, but only just, and "only just" is what produced the 400s. The request
now asks for 8192 (`CLIP_REVIEW_NUM_CTX`), which is enough for shapes that
used to fail outright: 1080p frames measure 4365 tokens and pass.

A verdict is written once and kept, so every clip judged while this was broken
still reads "ollama 400" however well the reviewer works now. Recordings whose
review failed get a **Revisar de nuevo** button. It rewrites the verdict and
deliberately does not move the file — it corrects a sentence rather than
moving a recording out from under someone — and says when the model now thinks
the clip was nothing, so deleting it stays a decision, not a side effect.

### The outcome of a review is written down, not inferred

Every reader worked out "unreviewable" from the truthiness of an error string,
and one case was badly wrong: review switched off produces a *state*
("review disabled"), not a failure, so with `CLIP_REVIEW_ENABLED=0` every
recording went to the tray and the fifteen-day purge would have deleted the
lot. The verdict now carries `outcome` — keep, discard or unknown — decided
once. The log line follows it too: a clip sitting in the tray was being logged
as "guardado".

### Removing a camera clears its recording rules, not its identity

Dropping the whole record took the name, motion zones, rotation and camera_id
with it, so a still-online board reappeared minutes later under a fresh uuid
with everything forgotten. "Remove" was quietly "reset".

### One resolver for per-camera settings

`set_camera_setting` decides which of the two stores a camera's settings go to,
and every setter goes through it. The window resolved the store by hand while
the duration wrote registry-only — the same bug in the field next door.

### The proxied login page says why, and shows it

The guard now fires only when `PROXY_SHARED_SECRET` is actually missing (any
client could otherwise 503 the login for itself with one header), and
login.html renders the explanation instead of a silent empty password box.

## Recent Changes

### [Date: 2026-08-08] - The Per-Camera Recording Window, Where It Can Be Read
**Agent**: Claude Code
**Task**: Review of "Give the per-camera recording hours a way in", and its repairs

**Problem**:
There are two stores for per-camera settings — `cameras.json` for a camera added
by hand, `registry_camera_settings.json` for a board that registered itself —
and the window was written to whichever one holds the camera but read from only
the second. For a hand-added camera the hours were accepted, echoed back as
absent, and never consulted by the recorder: the live `config/cameras.json` on
`compute` already carried a `recording_start_hour`/`recording_end_hour` pair
that nothing had ever honoured. For a registry camera the clear path could not
clear: the record was persisted with `update_registry_camera_setting`'s default
merge, and a merge cannot remove a key, so «seguir la casa» and undoing «nunca
grabar» reported success and changed nothing — a camera switched off could not
be switched back on from the page.

Three more, each silent: the GET's registry branch never returned the window at
all, so the page reported "follows the house" for a camera that did not, and the
next save acted on what it had read; the new `400`s were the first early returns
placed *after* `camera_config` had been mutated, and for a local camera that
object is `get_cameras()`'s own cache, so a rejected request left its changes
where the next successful save would write them to disk; and `24` was accepted
as a start hour, which `is_recording_enabled` turns into a window the clock
never enters — `24 → 0` reads like a whole night and means "never".

**Changes Made**:
1. **`settings_manager.get_camera_setting(key)`** (new) — this camera's record
   from whichever store holds it. `is_recording_enabled` and
   `get_camera_recording_window` now go through it.
2. **`set_camera_recording_window`** writes to the store the camera is in, as a
   whole-record write in both cases, since the point of a clear is the key that
   is no longer there.
3. **`validate_recording_hour`** (new) is the one place that decides what an
   hour is — the route calls it instead of keeping its own copy. The start hour
   stops at 23; the end hour still reaches 24. The message names the box on
   screen rather than the JSON field.
4. **`update_camera_settings`** takes a copy of a local camera's record instead
   of editing the cache in place, and persists a registry camera with
   `replace=True`. `recording_enabled` must now be a real boolean — `bool("false")`
   is `True`, which is the opposite of what such a caller asked for.
5. **`get_camera_settings`** returns the window from both branches.
6. **`_migrate_cameras_dict`** writes back to the file it migrated. It was
   hardcoded to `cameras_file`, so one read of a legacy registry overlay saved
   the registry's records over the camera list and took every hand-added camera
   with it.
7. **`edit_settings.html`** — the hour boxes are disabled while hidden (a
   display:none control is still validated, and a form the browser cannot
   scroll to simply stops submitting); the form says nothing about the window
   until it has read one, so a save made before the fetch lands no longer erases
   it; «nunca grabar» keeps the hours instead of deleting them; the hours are
   loaded into the boxes whatever the mode; and choosing "only between these
   hours" with a box empty is refused rather than quietly saved as "follow the
   house".
8. **`test_recording_window.py`** covers the hand-added camera, the start hour
   of 24, and that reading the registry overlay leaves the camera list alone.

### [Date: 2026-08-01] - YOLO Weights Downloaded on Demand, Not Committed
**Agent**: opencode
**Task**: Keep the detectors fed without shipping 330 MB of binaries

**Problem**: The 15 `models/*.pt` files were deleted from the repo (`cc0ab21`),
but the code still loads `models/yolo26x.pt` (scene detection), `models/yolo26m.pt`
(motion, GPU) and `models/yolo26s.pt` (motion, CPU fallback) — and the compose
mount pointed at `/app/models`, a path the code never reads.

**Changes Made**:
1. **`download_models.sh`** (new) — fetches the three referenced weights into
   `models/` from the Ultralytics asset releases when missing.
2. **`docker-compose.yml`** — the mount is now `./models:/app/web_server/models`,
   matching the relative `models/...` paths the code uses.
3. **`Jenkinsfile`** — runs `./download_models.sh` before `docker compose up`.
4. **`.gitignore`** (repo root) — ignores `web_server/models/`.

### [Date: 2026-05-23] - Fix Camera List Not Loading and Restart Bugs
**Agent**: opencode (big-pickle)
**Task**: Fix cameras appearing stuck (dashboard blank), multiple NameError crashes, and orphaned fetcher threads on restart

**Problem**:
1. **Dashboard showed blank/loading** — `handle_connect()` WebSocket handler had a `NameError: name 'ip' is not defined` because the dict comprehension used `ip` as the key but `for mac, config` as loop variables, left over from the MAC-to-IP refactoring (commit `0eb3bd0`). Each client connection crashed the handler, preventing the camera list from ever reaching the frontend, making cameras appear "stuck" or not loading.
2. **Settings save crash** — `update_camera_settings()` at line 1750 referenced undefined variable `mac` when updating `show_all_objects`, causing `NameError`.
3. **H.264 encoder crash** — `get_h264_encoder()` at line 1295 logged `{mac}` which was undefined (should be `{camera_key}`).
4. **Orphaned fetcher threads** — `restart_camera_connection()` popped the fetcher thread from the dict but the old thread kept running forever because it checked `camera_key not in camera_manager.cameras` which was always False (camera was re-added immediately). Each restart spawned an orphaned thread that never died, accumulating over time and causing race conditions on stuck detection state.

**Changes Made**:
1. **web_server/web_server.py** (`handle_connect`, line 1908):
   - Changed `for mac, config in cameras.items()` to `for ip, config in cameras.items()`
   - Changed `camera_manager.get_camera_state(mac)` to `camera_manager.get_camera_state(ip)`

2. **web_server/web_server.py** (`update_camera_settings`, line 1750):
   - Changed `v.camera_id == mac` to `v.camera_id == camera_key`

3. **web_server/web_server.py** (`get_h264_encoder`, line 1295):
   - Changed `{mac}` to `{camera_key}` in log message

4. **web_server/web_server.py** (`restart_camera_connection`):
   - Added `camera_manager.cameras.pop(camera_key, None)` after stopping the stream thread, so the old fetcher thread sees the camera is gone and exits its loop
   - Also cleaned up `camera_states`, `stream_threads`, `frame_queues` from the camera manager during teardown
   - Re-adds the camera after the 2-second sleep inside the lock block

5. **web_server/web_server.py** (`start_camera_fetchers`):
   - Renamed misleading `mac` loop variable to `cam_id` for clarity

**Summary**:
- Dashboard now loads cameras correctly — no more `NameError` on client connect
- Settings save for `show_all_objects` no longer crashes
- H.264 encoder log message no longer crashes
- Restart no longer spawns orphaned fetcher threads; old fetcher thread exits cleanly when camera is temporarily removed

### [Date: 2026-05-16] - Fix Recording Time Window, Camera Names in Filenames/Overlays
**Agent**: opencode (big-pickle)
**Task**: Fix recording time window enforcement, timezone handling, and add camera names to filenames and overlays

**Problem**:
1. Snapshots were saved regardless of the recording time window — only video recording start respected `is_recording_enabled()`
2. Active recordings continued past the time window end — `add_frame_to_recording()` never checked the window
3. `is_recording_enabled()` used naive `datetime.now()` instead of the configured `TZ` timezone
4. Filenames used sanitized MAC/IP addresses (e.g., `AA-BB-CC-64-00-8F`) instead of friendly camera names (e.g., `Front-Door`)
5. Video overlays showed MAC/IP addresses instead of camera names

**Changes Made**:
1. **web_server/web_server.py** (snapshot saving in `process_frame`):
   - Wrapped snapshot saving in `settings_manager.is_recording_enabled()` check — snapshots now respect the time window

2. **web_server/web_server.py** (active recording frame feed in `fetch_camera_frame`):
   - Added time window check before each frame feed — if the window has closed, calls `stop_recording()` to finalize the recording immediately

3. **web_server/settings_manager.py** (`is_recording_enabled`):
   - Changed `datetime.now()` to `datetime.now(get_timezone())` using the same timezone-aware function from `recording.py`, ensuring the time window check matches the timestamps in filenames

4. **web_server/recording.py** (camera name support):
   - `_safe_camera_id()`: Added optional `camera_name` parameter — sanitizes the name for filesystem use, falls back to IP/MAC
   - `_draw_overlay()`: Added optional `camera_name` parameter — displays name instead of IP/MAC on video frames and snapshots
   - `save_snapshot()`: Added optional `camera_name` parameter — passed through to `_safe_camera_id()` and `_draw_overlay()`
   - `start_recording()`: Added optional `camera_name` parameter — passed to all overlays, stored in `_active_recorders` dict
   - `add_frame_to_recording()`: Reads `camera_name` from recorder dict and passes to `_draw_overlay()`
   - Added `import re` for name sanitization

5. **web_server/web_server.py** (`_get_camera_name` helper):
   - Added helper function that looks up the camera name from `cameras.json` or registry settings by MAC key
   - Camera name resolved once per motion event and passed to both `start_recording()` and `save_snapshot()`

**Summary**:
- Recording time window now correctly prevents snapshot saving outside configured hours
- In-progress recordings are stopped immediately when the time window expires
- Time window check consistently uses the configured timezone
- Filenames now use camera names: `2026-05-16_14-30-00_Front-Door_person.mp4` instead of `2026-05-16_14-30-00_AA-BB-CC-64-00-8F_person.mp4`
- Video overlays show camera name instead of MAC address

### [Date: 2026-05-13] - Add Per-Zone Light Change Filtering
**Agent**: opencode (GLM-5)
**Task**: Add per-camera motion zone setting to filter out lighting changes and only trigger on actual movement

**Problem**:
Motion detection was triggered by lighting changes ( sunrise, sunset, cloud shadows, automatic lights) because these cause uniform pixel-level shifts across large areas of the frame. Real movement has distinct edge contours while lighting shifts produce diffuse, high-circularity blobs.

**Changes Made**:
1. **web_server/motion_detector.py** (`MotionZone` dataclass):
   - Added `filter_light_changes: bool = False` field
   - Added `light_change_threshold: float = 0.35` field (0=aggressive filter, 1=disabled)

2. **web_server/motion_detector.py** (`_check_zone_motion`):
   - Added `current_frame` and `background_frame` optional parameters
   - When `zone.filter_light_changes` is enabled, calls `_is_light_change()` to validate motion events

3. **web_server/motion_detector.py** (new `_is_light_change` method):
   - Uses OpenCV contour analysis on the difference frame within the zone
   - Calculates circularity (4πA/P²) and compactness (P/A) of detected motion contours
   - Lighting changes produce smooth, round, high-circularity blobs; real objects have irregular edges with low circularity
   - Returns True (filter out) when circularity > 0.7 AND compactness is very low

4. **web_server/motion_detector.py** (`_detect_motion_cpu` and `_detect_motion_gpu`):
   - Both methods now extract the background frame and current frame and pass them to `_check_zone_motion` when any zone has `filter_light_changes` enabled

5. **web_server/web_server.py** (`process_frame` and `update_camera_settings`):
   - Pass `filter_light_changes` and `light_change_threshold` from zone config to `MotionZone` constructor in both initialization and update paths

6. **web_server/templates/edit_settings.html**:
   - Added "Filter light changes" checkbox per zone in the zone list panel
   - Added "Sensitivity" slider (0.1–0.9) that appears when light filtering is enabled
   - Zone list items show "(light filter)" badge when enabled
   - Updated `zonesToPercentages` and `percentagesToZones` to serialize/deserialize the new fields

**Summary**:
- Per-zone light change filter to distinguish real motion from lighting shifts
- Uses edge contour circularity analysis — high circularity + low compactness = lighting change
- Each motion zone can independently enable/disable the filter with adjustable sensitivity

### [Date: 2026-04-08] - Fix Blue Screen Issue on Android
**Agent**: Cline (Anthropic)
**Task**: Fix blue screen issue on Android app when viewing H.264 streams

**Problem**:
The Android app was showing a blue screen instead of the camera feed, with multiple underlying issues:
1. TextureView background drawable error causing app crashes
2. Kotlin compilation errors with Byte/Int type mismatches
3. Missing camera resolution data preventing proper decoder configuration
4. Layout issues masking the actual video feed

**Changes Made**:
1. Updated `web_server.py`:
   - Removed JPEG fallback encoding that was causing stream format mismatches
   - Ensured proper raw H.264 elementary stream output
   - Improved error handling and logging

**Key Technical Findings Documented**:
- **H.264 Stream Format**: Raw H.264 elementary streams must be used instead of container formats for direct MediaCodec decoding

**Summary**:
- Fixed app crashes caused by TextureView background drawable error
- Ensured proper H.264 hardware decoding with MediaCodec
- Android app now displays actual video feed instead of blue screen

### [Date: 2026-04-10] - Add Hardware H.264 Encoding Support
**Agent**: Cline (Anthropic)
**Task**: Add hardware-accelerated H.264 encoding using NVIDIA NVENC

**Changes Made**:
1. Created `h264_encoder.py`:
   - `H264EncoderConfig` dataclass for encoder configuration
   - `H264Encoder` class with hardware-accelerated encoding using NVENC
   - `H264StreamGenerator` class for generating H.264 elementary streams
   - Automatic fallback to CPU encoding (libx264) when NVENC is not available
   - Thread-safe encoding with proper resource management

2. Updated `camera_manager.py`:
   - Added H.264 encoder imports with graceful fallback
   - Added `H264_ENCODING_AVAILABLE` flag for conditional encoding support

3. Updated `web_server.py`:
   - Added H.264 encoder imports with graceful fallback
   - Added `h264_encoders` dictionary for per-camera encoder caching
   - Added `get_h264_encoder()` function for encoder reuse
   - Added `generate_h264()` generator function for H.264 streaming
   - Added `/h264/<camera_ip>` endpoint for H.264 video streaming
   - H.264 stream uses elementary stream format suitable for direct playback

4. Updated `Dockerfile`:
   - Added FFmpeg packages with NVENC support
   - Installed libavcodec-extra, libavformat-extra, and related libraries

**Key Features**:
- Hardware-accelerated H.264 encoding using NVIDIA GPU (NVENC)
- Automatic fallback to CPU encoding when NVENC unavailable
- Per-camera encoder caching for efficiency
- Thread-safe streaming with proper resource cleanup
- Compatible with HTML5 `<video>` tag and media players

**Usage**:
- MJPEG stream (existing): `http://server:5000/stream/<camera_ip>`
- H.264 stream (new): `http://server:5000/h264/<camera_ip>`

**Requirements**:
- NVIDIA GPU with NVENC support (GTX 9xx series or newer)
- FFmpeg with NVENC support
- PyAV (av) Python package

**Summary**:
- Added hardware-accelerated H.264 encoding using NVIDIA NVENC
- Provides more efficient streaming with lower CPU usage
- Falls back to CPU encoding for systems without NVENC
- New `/h264/<camera_ip>` endpoint provides H.264 elementary stream

### [Date: 2026-04-10] - Fix "Source Error" with Raw H.264 Stream
**Agent**: Cline (Anthropic)
**Task**: Fix "source error" in backend for H.264 streaming

**Problem**:
The Android app was showing a "source error" immediately when opening the camera stream. The issue was caused by the fragmented MP4 format produced by FFmpeg not being properly compatible with MediaCodec.

**Root Cause**:
The H.264 encoder was using FFmpeg to produce fragmented MP4 (ISOBMFF) format with `-movflags +frag_keyframe+empty_moov+default_base_moof`. This format was not properly parseable when served as a continuous HTTP stream.

**Changes Made**:
1. Updated `h264_encoder.py`:
   - Changed FFmpeg output format from `mp4` to `h264` (raw elementary stream)
   - Removed fragmented MP4 flags (`-movflags`)
   - Added `-preset quality` and `-rc vbr` options for NVENC configuration
   - Now outputs raw H.264 NAL units without any container format

2. Updated `web_server.py`:
   - Changed response mimetype from `video/mp4` to `video/h264`
   - Added `Cache-Control: no-cache` and `Connection: keep-alive` headers
   - Updated comments to reflect raw H.264 elementary stream output

**Technical Details**:
- Raw H.264 elementary stream contains only NAL units (no container)
- FFmpeg command changed: `-f h264` instead of `-f mp4 -movflags +frag_keyframe...`
- Lower latency compared to fragmented MP4 approach

**Summary**:
- Fixed "source error" by using raw H.264 elementary stream format
- Changed FFmpeg to output raw H.264 NAL units without MP4 container
- Updated mimetype to `video/h264` for proper stream identification

## Core Backend Project Structure

```
web_server/
├── camera_manager.py         # Camera detection and management
├── enhanced_motion_detector.py # Advanced motion detection
├── fcm_service.py           # Firebase Cloud Messaging integration
├── h264_encoder.py          # Hardware-accelerated H.264 encoding
├── main.py                  # Entry point
├── motion_detector.py       # Basic motion detection
├── mqtt_client.py           # MQTT publishing for home automation
├── object_detector.py       # Object detection (YOLO)
├── recording.py             # Video/snapshot recording with overlays
├── settings_manager.py      # Application settings management
├── web_server.py            # Main HTTP server with streaming endpoints
├── static/                  # Static assets
├── templates/               # HTML templates
└── AGENTS.md                # This file
```

## Contact & Support

For questions about Core Backend contributions or to report issues, please refer to the project maintainers.