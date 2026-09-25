# AGENTS.md

Guidance for working in this repo. The household's cameras: ESP32 camera boards
and RTSP cameras, a Python server that watches them for motion, records clips,
and answers questions about what a camera can currently see.

This file is the **guide**. [`CHANGELOG.md`](CHANGELOG.md) is a dated **changelog**
— append an entry there when you change something, and read it when you need the
reason behind a decision. Per-component changelogs live in `android/CHANGELOG.md`,
`camera_registry/CHANGELOG.md`, `esp32/CHANGELOG.md` and `web_server/CHANGELOG.md`.

## Two services

| Service | Path | Port | Role |
|---|---|---|---|
| Web server | `web_server/` | 5000 | Streams, motion detection, recording, the dashboard, the API |
| Camera registry | `camera_registry/` | 5001 | ESP32 boards self-register here; `/api/register`, `/api/list`, `/api/<ip>/ping`, `/api/stats` |

Both run on **`compute`**, on a shared external docker network `homecameras-net`.
The web server syncs registry cameras into its own camera list
(`sync_registry_cameras`), so a board that registers appears without anyone
adding it by hand.

`web_server/web_server.py` is ~2600 lines and holds the HTTP routes, the
WebSocket handlers and the per-camera frame loop. The rest are modules it drives:
`camera_manager` (config + per-camera state), `motion_detector` /
`enhanced_motion_detector`, `object_detector` (YOLO), `object_tracker` (the recording gate), `recording` (clips,
snapshots, ring buffer), `h264_encoder`, `mqtt_client`, `settings_manager`,
`fcm_service` (Firebase push to the Android app).

## Nothing is recorded until YOLO confirms it

Motion is not evidence. A recording -- and a snapshot -- starts only once
`object_tracker.ObjectTracker` has held a person, vehicle or animal across
three frames; `enhanced_motion_detector` puts that subset on the event as
`confirmed_objects`, and `web_server` gates on it. `detected_objects` still
carries every per-frame detection, so overlays, logs and MQTT are unchanged.

This is a filter that can lose recordings, so two things about it matter:

- **It costs no footage.** The ring buffer fills for as long as a camera is not
  recording, so the seconds before an object was confirmed are still handed to
  the clip.
- **Its failure mode is silent.** A recording that never starts logs nothing,
  so anything that stops a track reaching three frames looks exactly like a
  quiet garden. That is why association is not IoU alone -- at 5 fps a walking
  person barely overlaps their own previous box -- and why
  `test_object_tracker.py` pins realistic walking, hurrying and driving paces
  rather than only the false positives it is meant to reject.

**Presence, for the security system (2026-09-21).** `security_state.py` reads
the tracker rather than the frame: a kind (person / vehicle / animal) is
*present* on a camera when a confirmed track of it was matched within
`SECURITY_PRESENCE_HOLD` (30 s), *active* when that track has moved lately
and *stationary* otherwise (`TRACK_STATIONARY_SECONDS`, 45 s -- a parked car,
a hoodie on a chair). It publishes `homecameras/security/{cam}/state` on
change and `/event` per arrival or departure, and announces two HA
`occupancy` binary sensors per camera over MQTT discovery (retained,
re-announced every 10 min because the broker keeps nothing across a
restart). `/api/detect/<cam>` carries the same picture as `tracked`, which
is what the assistant answers "is anyone there" from; the single-frame
`detections` stay for what is in view right now. On an infrared frame
(`is_ir_frame`, near-monochrome) priority classes need
`NIGHT_MIN_CONFIDENCE` (0.40) per frame before the tracker sees them -- the
night patio's `person` at 0.31 and pergola-as-two-beds never reach it.
`test_security_state.py` and the stationary cases in `test_object_tracker.py`
run with plain `python3`.

`object_tracker.py` is deliberately plain Python: no ultralytics, no torch, no
card, so the rule that decides what reaches disk can be tested anywhere.
Its tests and `test_motion_zone.py` run with `python3 web_server/<file>`.

A snapshot outlives its clip unless something collects it. Only the first
snapshot of a recording shares the clip's name, so `delete_recording` takes that
one and `recording.purge_orphan_snapshots()` -- in the 60-second maintenance
loop -- takes any whose camera has no clip starting within
`SNAPSHOT_ORPHAN_WINDOW` before them. Without it, deleting the videos leaves the
JPEGs, which is half the disk.

The detector's verdict is also handed to the reviewer: `prompt_for(filename)`
in `clip_review.py` reads the tags off the clip's name and tells the vision
model what was found and what is scenery, rather than leaving it to guess from
three stills. That prompt is built with `str.replace` and not `str.format` --
it carries a literal JSON answer template, and every brace in it would be a
format field.

## Deploying

A **manual** Jenkins run (`Jenkinsfile`, agent `compute`) — pushing does not
deploy. It creates the shared network, deploys the **registry first** and curls
`/api/ping` until it answers, then the web server, then curls `/` — each stage
fails the build if its service never comes up.

**It patches the compose file when there is no GPU.** If `nvidia-smi` fails, a
`sed` strips the `deploy:` reservation block so the stack falls back to CPU
instead of refusing to start. That means `docker-compose.yml` in a build
workspace may differ from the one in git; do not be confused by it.

The build also **fails if `/mnt/data/home-lab-configs/HomeCameras` is missing**,
rather than starting with an empty config directory. That check exists because of
the incident below.

**`PROXY_SHARED_SECRET` must reach the container**, from the Jenkins credential
`PROXY_SHARED_SECRET` (the same one the portal and the assistant use).
It is what lets this app accept HomeCore's word for *which member* is looking
when it proxies these pages at `/camaras/` — the local door is one shared admin
password and cannot tell anybody apart.

Shipped once without it, and the symptom does not point at the cause: the
pages come up, `/settings` asks for the admin password even though you are
logged into HomeCore, and Recordings renders empty — its `fetch` follows the
redirect and gets the login page instead of JSON, so there is nothing to list
and no error to see. The server now logs a warning at startup when it is
unset; if those two symptoms appear together, read that log first.

## Where state lives — and why nowhere else will do

Three locations, each deliberate:

| What | Where | Why there |
|---|---|---|
| Config | `/mnt/data/home-lab-configs/HomeCameras` → `/app/config` | Outside the Jenkins workspace, and outside the recordings tree |
| Recordings | `/mnt/data/home-cameras` → `/app/data/recordings` | A real host path on the 3.6 TB disk, so backups and pruning can reach it |
| Logs | named volume `homecameras_logs` | — |

**The config path is the scar tissue of a real data loss.** The mount used to be
`../config`, relative to the compose file, which during a Jenkins build is the
*job workspace*. Builds 95-97 reused that workspace, so an incremental checkout
left the gitignored `config/cameras.json` alone. Build 104 fell back to a fresh
clone, restored tracked files only, and **every camera vanished**. Never seed the
live config from the repo, and never point this mount at anything inside a
workspace.

It is equally deliberate that config is **not** under `/mnt/data/home-cameras`:
`prune_camera_recordings.sh` deletes files there past the retention window, and
`home-backups/media_sync.py` mirrors that tree to the Azure `cameras-backup`
container — which would have shipped every camera's **RTSP credentials** to blob
storage.

Only `config/admin.json` and `config/global_settings.json` are tracked in git;
`cameras.json` is gitignored and lives only on the host.

## Auth — read this before touching `/login` or the API

Fixed on 2026-07-27 (`61dfa09`), after `/login` was found setting
`session['authenticated']` **without ever calling `verify_password`** — any
password authenticated. The port is bound to `0.0.0.0` and
`/api/export_config` returns every camera's RTSP credentials in plaintext, so
this was the whole front door.

- `/login` verifies the password and returns 401 on failure.
- `SECRET_KEY` comes from `FLASK_SECRET_KEY`, or a random key persisted at
  `config/secret_key` (0600). **Never a hardcoded value** — the committed
  placeholder `'secret-key-goes-here'` meant a forged session cookie bypassed
  `/login` entirely, so fixing the password check alone would not have been
  enough.
- Post-login redirects are constrained to same-site paths.
- The default admin password is seeded **only when none exists**. It used to be
  reset to `admin` on every restart, which also discarded restored passwords.
- `POST /api/change_password` exists because nothing called
  `set_admin_password` and the password was otherwise unchangeable.

Treat `/api/export_config` as a credential-bearing endpoint in anything you add.

## `camera_id` is the identity — IP is a mutable field

Every camera has a stable UUID hex `camera_id`, generated in `CameraConfig.__post_init__`.
All dicts (`cameras`, `camera_states`, `stream_threads`, `frame_queues`, motion
detectors, stuck-region state) are keyed by it. `device_ip` is just a field that
can change with DHCP.

- Old IP-keyed `cameras.json` and `registry_camera_settings.json`
  **auto-migrate on first read** (`_migrate_cameras_dict`), injecting a UUID for
  entries that lack one. The migration writes back to the file it came from —
  it used to write to `cameras.json` whichever file it had been handed, so one
  read of a legacy registry overlay saved the overlay over the camera list.
- **A camera's settings are in one of two files**, and which one depends on how
  the camera arrived: `cameras.json` for one added by hand, `registry_camera_settings.json`
  for a board that registered itself. `update_camera_settings` writes to whichever
  holds the camera, so **a reader that knows only one of them is wrong for half the
  house** — the symptom is a setting that is accepted, shown back, and then ignored.
  Read per-camera settings through `settings_manager.get_camera_setting(key)`, which
  looks in both, and `set_camera_setting` is the one place that decides which
  store a camera's settings are written to. Reach for that pair rather than
  picking a store: the getter and setter of a field kept drifting apart one
  field at a time, and a value written to the store its own getter does not
  read is saved, shown back, and never consulted.
- API routes accept **either** a `camera_id` or a `device_ip` — resolve with
  `resolve_camera_id()` / `resolve_camera_config()` (routes) and `_resolve_key()`
  (settings). Never index a dict with an unresolved key from a request.
- Recording filenames use `_safe_camera_id()` (`:` and `.` → `-`), because raw
  MACs and IPs broke URLs and the recordings filter.

## The frame loop

One fetcher thread per camera (`fetch_camera_frame`), which for each frame:
motion detection → object detection → ring buffer or active recording → optional
MQTT publish → WebSocket emit.

Things that are the way they are for a reason:

- **Idle does not mean stop processing.** When nobody is watching the dashboard,
  the loop adds a small sleep but keeps detecting. It used to `continue` past all
  processing, which silently disabled motion-triggered recording whenever no one
  had the page open.
- **A camera with no motion zones gets a default "Full Frame" zone.** Empty zones
  used to disable detection entirely, so two cameras were simply never watching.
- **Stuck detection**: `check_stuck_region` watches the timestamp region for
  changed white-pixel count; unchanged for `STUCK_REGION_TIMEOUT` (30 s) triggers
  `restart_camera_connection`, retryable after `STUCK_REGION_RETRY_COOLDOWN`
  (300 s). The restart must **pop the camera from `camera_manager.cameras`**
  before re-adding it, or the old fetcher thread never sees that it should exit
  and you accumulate orphaned threads forever.

## Two detectors, on purpose

| | Motion alerts | Answering a question |
|---|---|---|
| Weights | `yolo26m` | `yolo26x` (`SCENE_DETECT_MODEL`) |
| Loaded | per camera, always | **lazily**, once, on first use |
| Classes | `target_classes` only | `detect_all_classes=True` |
| Confidence | person/cat/dog floored at 0.20 | a single floor, `SCENE_MIN_CONFIDENCE` |

The motion detectors run continuously on every camera and share a GPU with the
house's LLM, so they stay on the mid-size weights and only look for things worth
waking someone for. A false alarm costs a glance; a miss costs the thing the
camera is for.

**`GET /api/detect/<camera_key>`** inverts that trade. It answers "is anybody
en el patio?" — the largest weights, every class, and a confidence number.
Lazy-loaded because a household that never asks a camera a question should not
pay 118 MB of weights for the privilege.

**Certainty banding is load-bearing.** Every detection carries `certainty`
alta/media/baja and the payload states outright that `baja` is a possibility and
never a fact. On the first live run `yolo26x` reported `person` at 0.35 and two
`bed` at 0.35-0.38 in an empty night patio — the pergola, and nobody at all. A
fluent assistant turns a bare 0.35 into "hay alguien en el patio", which is worse
than the hedged prose this replaced because it sounds certain. Bands rather than
a hard cutoff, because a real person on a dark frame also sits at 0.35 and
dropping them silently is its own failure.

**The endpoint composes no sentence.** It hands over findings and lets the
assistant phrase them — it writes better Spanish than a format string and knows
who asked and why. It says explicitly when nothing was detected, because "nothing
recognised in a dark patio" and "the camera is broken" are different claims and
an empty list reads as both.

## Recording

- Clips are ~15 s: a **5 s pre-motion ring buffer** (JPEG-compressed, per camera,
  drained and prepended when recording starts) plus 10 s after. Pre-buffer frames
  carry a timestamp-only overlay — never stale detection boxes from before the
  event.
- Only the **current frame's** detections may be drawn. Collecting from the
  accumulated `motion_events` dict is what made overlays pile up in clips.
- **Motion recording is time-gated** by `is_recording_enabled()` —
  `recording_start_hour` / `recording_end_hour` in `global_settings.json`,
  currently 23:00-06:00. Motion *events* publish outside the window and always
  did; only recording is gated.
- **A camera can keep its own window**, set on its edit page and stored beside
  the camera. Both hours or neither — a lone hour is ignored rather than paired
  with the house's other end — and its own `recording_enabled: false` switches
  that camera off without touching the house. So when a camera is not recording,
  `global_settings.json` is only half the answer: check the camera's own record
  too, with `settings_manager.get_camera_recording_window(camera_id)`. Which
  file that record is in depends on how the camera arrived (see below); read it
  through `get_camera_setting`, which knows both, and never through one store
  directly.
- **Manual recordings are exempt** (`POST /api/cameras/<camera_key>/record`, with
  an optional `{"duration": <seconds>}`). The exemption has to be honoured in the
  frame loop as well, not just at the entry point — the loop called
  `stop_recording()` the moment it saw a closed window, ending a manual recording
  one frame in.
- `size_bytes == 0` / duration defaults: the fallback is 10 seconds. It was once
  `MAX_RECORDING_HOURS * 3600`, i.e. an hour.
- Serving supports HTTP **Range** requests, and paths must be encoded
  **segment by segment** — `encodeURIComponent` on a whole path escapes the `/`
  in `2026-05/clip.mp4` and breaks playback.

## MQTT

Governed by [`../docs/mqtt-conventions.md`](../../docs/mqtt-conventions.md),
which covers the whole stack. Broker `mqtt.home:1883` via `MQTT_BROKER_HOST` /
`MQTT_BROKER_PORT`.

| Topic | When |
|---|---|
| `homecameras/motion/<camera>` | every motion event |
| `homecameras/motion/<camera>/objects` | when objects were detected |
| `homecameras/recording/<camera>` | a clip starts or ends |

Two traps. **Subscribe with `+`, not `#`** — `motion/#` also matches
`motion/<cam>/objects`, which is how a subscriber ended up firing twice per
detection. And the **client id must be unique**: a fixed `homecameras-server`
collided with a stale deployment on the `hub` agent, and the broker kicked
whichever had connected first, forever (`rc=7` flapping several times a second).
It is derived from the hostname now, overridable with `MQTT_CLIENT_ID`.

Keep code defaults pointed at `mqtt.home`, not `localhost` — an unreachable dead
default silently disables MQTT if the settings layer is ever bypassed.

## Frontend, app and firmware

- `web_server/templates/` — `dashboard.html`, `recordings.html`,
  `edit_settings.html`, `add_camera.html`, `settings.html`, `login.html`. Dark
  theme, no build step. The dashboard is driven by WebSocket `camera_list` emits,
  which must include `camera_id`.
- `android/` — the Android client (see its own `CHANGELOG.md` and `DEPLOYMENT.md`).
  Push goes through `fcm_service.py`.
- `esp32/camera-server/` — board firmware; boards self-register with the
  registry.

## Tests

`tests/` holds standalone scripts (`test_camera_feeds.py`,
`test_scene_detection.py`, `test_motion_debug.py`, …) run directly rather than a
suite wired into CI, plus `camera_registry/test_registry.py`. Nothing runs them
on deploy.
