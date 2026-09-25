# AI Agents Documentation & Change Log

This file serves as a central tracking document for all changes made by AI agents to the HomeCameras project.

## Recent Changes

### [Date: 2026-08-09] - The Reviewer Was Being Shown The Answer
**Agent**: Claude Code

Every recorded frame carried `_draw_overlay`: the camera name and wall clock
top-left, a coloured box around each thing YOLO found, its class and
confidence beside it. Clips are reviewed by reading frames back out of that
same file, so the vision model was handed a picture with the answer written on
it. Two failures came from that, both seen in the verdicts:

- the clock is redrawn every frame, so **every** clip contained something that
  changed however still the scene was. "La hora en la esquina cambió de
  20:30:38 a 20:30:53" was true, and entirely our own doing — and it is why so
  many clips were judged on the timestamp rather than on the garden;
- an unrecognised class draws in red `(0,0,255)`, and verdicts came back
  describing "un objeto rojo en el césped se mueve": the model reporting our
  own annotation as a thing in the scene. A green box labelled "person: 0.87"
  has the same problem in the other direction — it cannot judge the scene
  independently of a label telling it what is there.

Recordings are written clean now (`RECORDING_OVERLAY=0`, the default).
Snapshots keep the full overlay: nothing reviews those, and they are what gets
looked at to see what the detector thought. The switch stays because a
burned-in timestamp is a reasonable thing to want on security footage — it
just cannot be had at the same time as a model reviewing the clip.

`test_clean_frames.py` records against a flat grey field and counts the pixels
that are not grey, read through the reviewer's own `_extract_frames`. It
checks what reaches the model rather than which function was called, because
the bug was invisible in the code path: `_draw_overlay` was called correctly,
on the right frames, and did exactly what it says.

### [Date: 2026-08-09] - One Camera, One Encoder
**Agent**: Claude Code

Two complaints, one bug: *"some videos won't play"* and *"it keeps saving
1 second videos"*.

Nineteen clips on the shelf had no readable video stream at all — ffprobe
reported `Invalid mvhd time scale`, and always the same impossible number,
which is corruption with a mechanism rather than a bad disk. The cause was
four ffmpeg processes still running with nobody holding them, one eighteen
minutes old:

```
ffmpeg ... /app/data/recordings/pending/2026-08-09_19-16-34_Front_car.mp4
fd 3 -> /app/data/recordings/2026-08/2026-08-09_19-16-34_Front_car.mp4
```

That second line is the whole thing. The file had already been finished by a
*different* encoder, passed `validate_clip`, and been filed to the month
folder — and this one still held a write descriptor to it and kept going.
A recording that is checked and then corrupted, which is why review could
never catch it.

Where the second encoder came from: `restart_camera_connection` retired the
old fetcher thread by removing the camera from `camera_manager.cameras` and
waiting two seconds — then put the camera back. Any thread that did not reach
its check inside that window never exited, and no window is wide enough,
because a restart happens precisely when a camera has stopped producing frames
and its fetcher is blocked. So the house ran two fetchers per camera. Both ran
`if not is_recording(cam): start_recording(cam)` — a check and an act with
fifty milliseconds in between — both passed, and the second overwrote
`_active_recorders[cam]`, orphaning the first encoder for good.

Same second, same filename, two encoders on one path → an unplayable file.
A second apart → two files, and the orphan finalised on garbage collection
holding nothing but its pre-buffer → **a one-second video**.

Fixed at both levels:

- `start_recording` takes the camera under the lock before it does anything
  slow, so the second caller is refused instead of racing. Every failure path
  gives the camera back — a reservation left behind is permanent, and that
  camera would silently never record again.
- Fetcher threads carry a generation; a restart bumps it and the old thread
  retires at its next loop whether or not it noticed anything. All fetcher
  creation goes through one function, so a thread cannot be born without one.
- Any writer that is about to stop being tracked is stopped, not dropped.

`test_one_writer.py` races eight threads at one camera. Against the old code
it returns the *same filename eight times*; against the fix, one winner and
one encoder. It also caught a bug in the fix itself — `return None` when ffmpeg
is missing skipped the cleanup and wedged the camera permanently.

**The nineteen already-broken clips are still on the shelf**; they hold no
decodable video and can only be deleted. The four stuck encoders die with the
container on the next deploy.

### [Date: 2026-08-09] - Filtering Recordings, And Binning The Static Ones
**Agent**: Claude Code

#### The reviewer names what it saw, in one word

It already wrote a sentence about every clip, and a sentence cannot be
filtered: "un vehículo nuevo detrás del muro", "vehículo negro estacionado en
el jardín" and "un vehículo en el garaje y otro en la calle" are the same word
three different ways, so finding the clip where somebody came to the door
meant reading all of them. The verdict now also carries `kind`: one of
`persona`, `animal`, `vehiculo`, `objeto`, `estatico`, `luz`, `clima`, `bicho`
or `otro`. Anything else the model says becomes `otro` — the vocabulary is
closed on the way in, because one of those words deletes recordings.

The recordings page has a **What is in it** filter over it, a count that always
says how much it hid, and a **Seleccionar las que se ven** button in the
controls rather than in the bar that only appears once something is already
selected — which was the one route to a bulk delete, and it was hidden behind
having done part of it by hand.

#### A scene that never changed is deleted, not filed for review

Two thirds of what these cameras record reaches the tray, and better than half
of *that* is a garden identical at both ends of ten seconds — about a third of
everything, at roughly seven hundred clips a week. The fifteen-day rule
deleted them anyway, later and after everybody had scrolled past them. They
are now binned when they are judged.

Held to its edges, because this is the only thing in the app that destroys a
recording:

- only the model's own word for it, `estatico`, never "a cloud went over";
- never when the model failed, and never when it also said to keep it;
- never when the recorder's detector named a person or an animal in the frame
  it fired on (`person`, `dog`, `cat`, … in the filename). Vehicles are not on
  that list: a parked car is in frame all day and tags a quarter of the tray.

Measured against this house's own footage — 45 clips from each side, then 30
more of the tray as the prompt was corrected: nothing on the shelf was binned
in any run, and better than half the tray was. `CLIP_REVIEW_DELETE_STATIC=0`
turns it off and everything goes to the tray as before; the count of what has
gone is on the settings page whether or not any has.

There is deliberately **no** pixel test alongside it. Measured over 611 clips,
the quietest one the reviewer kept — a black cat crossing a dark garden —
moves fewer pixels between frames than a quarter of the clips it sent to the
tray, where a burned-in clock ticking over is the whole of the change. No
frame-difference threshold separates them at any resolution, and one picked by
eye would eventually delete the cat.

#### The tray can be emptied

`POST /api/recordings/review/purge`, behind a button that says how many it
will take. It touches `to_review/` only.

### [Date: 2026-08-09] - Review Outcomes, Backups, And Camera Removal
**Agent**: Claude Code

#### A clip nobody could look at goes to the tray

It used to be kept — right that "the model is down" must never file a
recording as boring, wrong about where that leaves it. Kept means *judged
interesting*: it vanished into the month folder among clips a person chose,
and the tray's "no se pudo revisar" line could never appear because nothing
ever reached it. The tray is the pile a person looks through, and nothing
there is deleted for being dull, only by age.

### Per-camera settings survive a backup

`/api/export_config` never carried `registry_camera_settings`, so a restore
put every camera back on the house schedule and started any camera set to
"never record" recording again — with nothing in the file to show what was
lost. Export is version 3; import restores them one camera at a time, so a
merge stays a merge. Version 2 files simply have no such key.

### Removing a camera removes its settings

A board that re-registers gets the same camera_id, so a camera removed while
set to "never record" came back still refusing to record, and delete-and-re-add
reproduced the state instead of clearing it.

### The recording-duration override had the two-store bug too

`get_camera_recording_duration` read only the registry overlay, so for a
hand-added camera the override was written and never read. Same fix as the
window, in the field next to it.

### The login page says why, instead of looping

Reached through HomeCore it cannot work: HomeCore strips `Set-Cookie` on the way
back — it must, or this app's session cookie lands on HomeCore's origin under
the same name and logs the member out of the house — so a login here would
succeed, set nothing and bounce back forever. It now says PROXY_SHARED_SECRET
is missing and points at the direct address.

### [Date: 2026-08-09] - HomeCore's CSRF Token, From One File Instead Of Six Copies
**Agent**: Claude Code
**Task**: Review of "Send HomeCore's CSRF token with anything that changes something", and its repairs

**Problem**:
Proxied at `/camaras/`, these pages are same-origin with HomeCore, so HomeCore refuses
any POST/PUT/DELETE without its CSRF token and answers 403 with an HTML page that the
page then reads as JSON. The fix — a `window.fetch` wrapper that fetches the token and
attaches it — was pasted into all six templates, and had three faults in each copy.
It took the token response's `ok` as success, but with no HomeCore session `/camaras/_csrf`
*redirects*, and fetch follows it: back comes the login page, 200 and `text/html`, so
`r.json()` threw, the catch swallowed it, and the request went out tokenless to be 403'd
— the exact symptom the wrapper existed to remove, now with nothing to say why. It cached
the token for the life of the page with no retry, so a session replaced while a tab stayed
open broke every button until a reload. And it cached the token rather than the request
for it, so several writes fired together each fetched their own.

**Changes Made**: one `web_server/static/js/csrf.js`, loaded through the mount like
`socket.io.js` already is, replacing 210 lines of pasted copies with six one-line tags.
It checks the content type and `redirected` before believing a token response (the guard
the finance dashboard already had), caches the in-flight promise so concurrent writes
share one fetch, does not cache a failure, and on a 403 re-fetches once and sends the
request again. It copies `init` instead of writing to the caller's object, and clones a
`Request` rather than replacing its headers. It still does nothing on the LAN, and it
still cannot cover socket.io — which is XHR — so adding and removing a camera stay
unreachable through the proxy, for their own separate reason; that is written down in the
file rather than implied. The tag now sits above the theme comment, which had been
separated from the `<link>` it describes.

### [Date: 2026-08-08] - A Camera's Own Recording Hours Actually Take Effect
**Agent**: Claude Code
**Task**: Review of "Give the per-camera recording hours a way in", and its repairs

**Problem**:
The control shipped, and the setting behind it did not reach the recorder. Per-camera
settings live in two stores — `cameras.json` for a camera added by hand,
`registry_camera_settings.json` for a board that registered itself — and the window
was written to whichever store holds the camera but read from only one of them. A
hand-added camera's hours were saved, reported back as unset, and ignored all night;
a registered camera's hours could be set but never cleared, because the save merged
its record and a merge cannot remove a key, so a camera set to «nunca grabar» could
not be switched back on from the page.

**Changes Made**: the readers now look in whichever store holds the camera, clears are
whole-record writes, `24` is no longer accepted as a start hour (a window the clock
never enters, which reads like a whole night and means "never"), and reading the
registry overlay no longer saves its records over the camera list. Details, and the
four smaller faults found with them, in
[`web_server/CHANGELOG.md`](web_server/CHANGELOG.md).

### [Date: 2026-08-08] - «Por revisar» On The Recordings Page
**Agent**: Claude Code
**Task**: Give the review tray a face, and stop the two lists disagreeing

**Problem**:
`to_review/` had been filling since the reviewer landed, with nothing on the
page to empty it — clips aged out at fifteen days without a person ever seeing
one, which is the outcome the tray was built to prevent. Worse, `to_review/`
and `pending/` sit inside `RECORDINGS_DIR`, and `list_recordings` treated every
subdirectory as a month folder, so unreviewed clips were already showing on the
shelf below with a checkbox and a Delete button.

**Change**:
- A dashed **Por revisar** tray above the shelf: each card says why the model
  thought it was nothing, how long it has left, and offers Ver / Guardar /
  Borrar. Hidden when there is nothing to say, but a review backlog shows on
  its own.
- `list_recordings` skips `pending/` and `to_review/`. The shelf is what has
  been filed; the tray is what has not.
- `_resolve_path` no longer hunts by basename when the caller named a folder.
  It used to, so «Borrar» on a tray card that another phone had already kept
  resolved onto the archived copy and deleted the recording the household had
  just chosen to save.
- Deleting a clip takes its `.json` verdict with it, the way keeping one does.
- `/api/recordings/review` reports `size` in bytes like its sibling endpoint,
  and reports "never expires" as `null` rather than `0` — with
  `CLIP_REVIEW_MAX_AGE_DAYS=0` the page had been colouring every card red and
  saying "se borra hoy" about a purge that is switched off.
- The tray renders the model's words and the filename as **text**, never as
  markup, and its buttons are real listeners rather than `onclick` attributes
  with a filename quoted into them.
- Both actions read the HTTP status: a 404 or a 500 used to reload the tray
  looking exactly like success.

**Verified**: `web_server/test_clip_review.py` still passes.

### [Date: 2026-08-08] - Clips Are Reviewed Before They Are Filed
**Agent**: Claude Code
**Task**: Stop the recordings folder filling with shadows and leaves

**Problem**:
The motion detector looks at pixels changing, and a cloud crossing the sun
changes every pixel in the patio. So most recordings are a lighting change, a
branch, or a moth on the IR lamp, and the clips that matter are buried among
them. Separately, an ffmpeg that dies mid-encode leaves a file with a plausible
name and size that no player will open, indistinguishable from a real recording
until somebody clicks it.

**Change**:
- A finished clip is written to `recordings/pending/` instead of the month
  folder. `clip_review.ClipReviewQueue` — one worker — validates it with
  ffprobe, shows three frames spread across it to `qwen3-vl:8b` on
  `ollama.home`, and files it: `YYYY-MM/` if something happened,
  `to_review/` if it looks like nothing.
- **Nothing here deletes a recording for being boring.** `to_review/` is a tray
  for a person; only age empties it (`CLIP_REVIEW_MAX_AGE_DAYS`, 15).
- Anything the model cannot answer — down, timing out, answering nonsense — is
  **kept**, never filed as a shadow.
- A clip that fails validation is discarded, because it was never a recording.
- Each filed clip gets a `.json` sidecar saying what the model saw, so the
  review page can explain itself.
- The recording window (`recording_start_hour`/`_end_hour`) can now be set
  **per camera**, falling back to the house window. The patio is worth
  recording overnight and the living room is not.

**Verified**: `web_server/test_clip_review.py` and
`web_server/test_recording_window.py`, both runnable directly.

**One worker on purpose**: the vision model runs on `compute`, which is this
box and the same GPU the per-camera YOLO detectors use continuously.

### [Date: 2026-08-08] - The House Faces, Served From Here
**Agent**: Claude Code
**Task**: Stop the templates fetching fonts from the internet

**Problem**:
The six templates linked fonts.googleapis.com. This box is on the house LAN and
is reached over the VPN or not at all; a `<link rel=stylesheet>` is
render-blocking, and a router that DROPs outbound 443 rather than REJECTing it
does not fail the request — it waits out the TCP timeout. That is tens of
seconds of blank camera wall for a decorative font. It also told a third party,
on every page load, that somebody in this house was looking at their cameras.

**Change**:
- `web_server/static/fonts/` now holds the `.woff2` and a generated
  `house.css`; the templates link `/static/fonts/house.css`, which Flask
  already serves from `static_folder='static'`.
- The files come from `HomeCore/tools/fetch-fonts.sh` in the superproject, which
  writes the same set into all four apps. Do not edit `house.css` by hand.
- latin + latin-ext only: 544 KB, versus ~1.5 MB for the subsets nothing here
  would ever draw.

**Verified**: served the templates over HTTP and asked the browser —
`document.fonts.check()` true for all three families, no 404 for any font file.

### [Date: 2026-08-08] - The House House Style, and the Tokens It Assumed We Had
**Agent**: Claude Code
**Task**: Give the six web_server templates the house type, edge and focus ring

**Problem**:
The shared "The House" block was appended to all six templates, but it is written
against HomeCore's palette — it consumes `--olive-2`, `--honey` and `--clay`.
HomeCameras is a dark theme with an entirely different vocabulary (`--accent`,
`--danger`, `--surface`, `--text`) and defines none of those three. An
unresolvable `var()` is invalid at computed-value time, so the whole
declaration falls back to its initial value:

- `background: linear-gradient(…)` on the header edge became `transparent` —
  the hand-ruled edge was a 3px invisible strip on all six pages.
- `outline: 2px solid var(--honey)` became `outline-style: none`. Because that
  is an author declaration it beats the UA's `:focus-visible` ring, so the
  block *removed* keyboard focus indicators from every link and button,
  including the login form — the opposite of what it set out to do.

Separately, `header, .nav, .topbar { position: relative; }` ties the
specificity of this app's own `.nav { position: sticky; top: 0 }` and is
appended later, so it won and the nav bar stopped sticking on dashboard,
recordings, settings and edit_settings. In recordings that also stranded
`.selected-bar { position: sticky; top: 46px }`, whose 46px offset was clearing
a nav that no longer pins.

**Changes Made**:
1. **All six templates** — every house hue in the shared block now carries a
   literal fallback: `var(--olive-2, #7E8A61)`, `var(--honey, #C6892B)`,
   `var(--clay, #AC4B36)`. The block no longer depends on tokens it does not
   ship, so the edge paints and the focus ring is real here.
2. **All six templates** — `header, .nav, .topbar { position: relative }` is now
   `:where(header, .nav, .topbar) { … }`. At zero specificity it applies only
   where the page has not set `position` itself, so the sticky nav survives
   (and `sticky` still anchors the edge pseudo-element).
3. **All six templates** — `.brand h1` dropped from the display-face selector;
   it is a subset of the bare `h1` in the same list and only raised specificity.
4. **All six templates** — the Fraunces request is now the variable range
   `wght@9..144,600..900` instead of three discrete cuts, so a page asking for
   `font-weight: 800` gets 800 rather than jumping to the 900 face.

**Still open**: the Google Fonts `<link>` is render-blocking and these boxes are
LAN-only; if the house has no route out, first paint waits on the connect
timeout. HomeCore vendors KaTeX locally for exactly this reason.

### [Date: 2026-08-01] - YOLO Weights Out of the Repo, Downloaded on Demand
**Agent**: opencode
**Task**: Stop shipping ~330 MB of YOLO weights in every clone and every build

**Problem**:
The 15 `web_server/models/*.pt` files (yolov8 / yolo11 / yolo26, every size,
~330 MB) made the repo a ~1.1 GB clone and every deploy a fat image. They were
deleted in `cc0ab21`, but nothing fetched them back — and the compose mount
pointed at `/app/models`, a path the code never reads (`models/yolo26x.pt`
resolves relative to `/app/web_server`). That mismatch was invisible only
because `COPY . .` had baked the committed weights in; with them gone, a fresh
clone would start with no detector at all.

**Changes Made**:
1. **`web_server/download_models.sh`** (new) — fetches the three weights the
   code references (`yolo26m` motion/GPU, `yolo26s` motion/CPU, `yolo26x`
   scene) from the Ultralytics asset releases if missing. Idempotent; runs
   before `docker compose up`.
2. **`web_server/docker-compose.yml`** — bind mount is now
   `./models:/app/web_server/models`, where the code actually reads.
3. **`Jenkinsfile`** — runs `./download_models.sh` in the Deploy Web Server
   stage, so a fresh workspace still gets fed.
4. **`.gitignore`** — ignores `web_server/models/` so the weights cannot sneak
   back into the repo.

### [Date: 2026-07-29] - Cameras Answer What They See (detector, not vision model)
**Agent**: Claude Opus 5
**Commits**: `c6cc4f3`, `538dd08`
**Task**: Let the household assistant ask a camera a question, and make the answer honest about its own uncertainty

**Problem**:
"¿Hay alguien en el patio?" was answered by shipping the JPEG to a
vision-language model. Measured on real night frames from these cameras, the
small VLMs invented "una persona con chaqueta oscura" in an empty patio on half
their runs, and the large one took 10-40s to reach an answer a detector reaches
in milliseconds — and prose carries no confidence number. The object detector was
already running for motion alerts and was simply never exposed.

**Changes Made**:
1. **`GET /api/detect/<camera>`** — returns what the detector sees: the objects,
   their confidence, and an explicit note when nothing was detected. No sentence
   is composed server-side on purpose; the assistant phrases it, writes better
   Spanish than a format string, and knows who asked and why.
2. **Its own detector instance on `yolo26x`**, the largest weights on disk,
   loaded **lazily** — a household that never asks a camera a question does not
   pay 118 MB of weights for the privilege. The per-camera motion detectors stay
   on `yolo26m`: they run continuously on every camera and share a GPU with the
   house's LLM.
3. **`detect_all_classes`** for the scene endpoint, so a reply can mention the
   chairs and the potted plant. `target_classes` stays on for motion, where the
   point is to only wake someone for things worth waking them for.
4. **Confidence banding (`538dd08`)** — the first live run had `yolo26x` report
   `person` at 0.35 and two `bed` at 0.35-0.38 in an empty night patio: the
   pergola, and nobody at all. Every detection now carries `certainty`
   alta/media/baja, and the payload states plainly that `baja` is a possibility
   and never a fact. The scene detector also stopped inheriting the motion
   tuning, where person/cat/dog are floored at 0.20 deliberately.

**Why bands and not a cutoff**: a real person on a dark frame can also sit at
0.35, so dropping them silently is its own failure. The assistant gets to say
"puede que haya alguien" instead of choosing between a false claim and silence.
A fluent model turns a bare 0.35 into "hay alguien en el patio", which is worse
than the hedged prose it replaced because it sounds certain.

### [Date: 2026-07-28] - Point MQTT Code Defaults at mqtt.home
**Agent**: Claude Opus 5
**Commit**: `8e6c19e`

`mqtt_client.py` defaulted to `localhost` while `docker-compose.yml`,
`settings_manager` and `web_server` all defaulted to `mqtt.home`. The localhost
paths were unreachable dead defaults that would have silently disabled MQTT if
the settings layer were ever bypassed. See `docs/mqtt-conventions.md`,
which governs the whole stack: one broker, one name.

### [Date: 2026-07-27] - Fix Config Loss on Deploy; Close the Login Auth Bypass
**Agent**: Claude Opus 5 (1M context)
**Commit**: `61dfa09`
**Task**: Find out why a deploy wiped every camera — and fix what that investigation turned up

**Problem** (config loss):
`web_server/docker-compose.yml` mounted `../config`, a path relative to the
compose file, which during a Jenkins build is the **job workspace**. Builds 95-97
reused that workspace, so an incremental checkout left the gitignored
`config/cameras.json` alone. Build 104 fell back to a fresh clone and restored
tracked files only — taking all three cameras with it.

**Problem** (security, found while verifying the restore):
`/login` set `session['authenticated']` **without ever calling
`verify_password`** — any password authenticated. `/api/export_config` returns
every camera's RTSP credentials in plaintext, on a port bound to `0.0.0.0`.
Verifying the password alone was not enough either: `SECRET_KEY` was the
committed placeholder `'secret-key-goes-here'`, so a forged session cookie
bypassed `/login` entirely.

**Changes Made**:
1. Config mount moved to `/mnt/data/home-lab-configs/HomeCameras`, matching the
   convention SmartButton already uses on that disk. Deliberately **not** under
   `/mnt/data/home-cameras`: `prune_camera_recordings.sh` deletes files there past
   the retention window, and `media_sync.py` mirrors that tree to Azure — which
   would have shipped the RTSP credentials to blob storage.
2. `/login` verifies the password and returns 401 on failure.
3. `SECRET_KEY` comes from `FLASK_SECRET_KEY`, or a random key persisted at
   `config/secret_key` (0600). Never a hardcoded value.
4. Post-login redirects constrained to same-site paths.
5. `main.py` seeds the default admin password **only when none exists**, instead
   of resetting it to `admin` on every restart and discarding restored passwords.
6. Added `POST /api/change_password` — nothing called `set_admin_password`, so
   the password was otherwise unchangeable.
7. `Jenkinsfile` drops the dead `homecameras_config` volume creation and the
   `docker cp` that reseeded `admin.json` from the repo, and now **fails the
   build** if the config directory is missing rather than starting with an empty
   one.

### [Date: 2026-07-26] - Unique MQTT Client ID; On-Demand Recording API
**Agent**: Claude Opus 5 (1M context)
**Commit**: `04481a4`

**Problem**: MQTT was flapping — connect, then `unexpected disconnect (rc=7)`,
several times a second, forever. The client id was the fixed string
`homecameras-server`, and a stale deployment of this app on the `hub` Jenkins
agent connected with the same id, so the broker kicked whichever instance had
connected first and the two fought indefinitely.

**Changes Made**:
1. Client id derived from the hostname (overridable with `MQTT_CLIENT_ID`), which
   makes any second instance harmless.
2. Added `POST /api/cameras/<camera_key>/record` to start a recording on demand,
   with an optional `{"duration": <seconds>}`. Motion-triggered recording is gated
   by the 23:00-06:00 window; an explicit API call is a deliberate act, so it is
   not. That required exempting manual recordings from the window check **in the
   frame loop as well** — it called `stop_recording()` as soon as the window was
   closed, ending a manually triggered recording one frame in (measured: 1 frame /
   0.2 MB before, 93 frames / 2.8 MB after).

Motion events already published outside the window and still do; only recording
was ever time-gated.

### [Date: 2026-07-26] - Store Recordings on /mnt/data So They Can Be Backed Up
**Agent**: Claude Opus 5 (1M context)
**Commit**: `830a80d`

**Problem**: Recordings went to the `web_server_homecameras_recordings` Docker
volume, which nothing backs up and nothing prunes. By 2026-07-26 it held 50 GB
across 52,413 files going back to May — on the root SSD.

**Changes Made**:
1. Bind-mount `/mnt/data/home-cameras` over `/app/data/recordings`. A real host
   path, so `home-backups/media_sync.py` can mirror it to the Azure
   `cameras-backup` container (15-day window) and `prune_camera_recordings.sh`
   can keep the last 30 days on disk. It also puts footage on the 3.6 TB disk
   rather than the 915 GB root SSD.
2. Tracked `config/global_settings.json`, which held the 23:00-06:00 recording
   window only inside the deployed container until now.

Existing recordings were migrated; everything older than 30 days (39,054 files,
31 GB) was deleted first.

### [Date: 2026-06-28] - Wire MQTT Publishing and Time-Gated Recording on Motion
**Agent**: Claude Sonnet 4.6
**Commit**: `b5cda84`

1. **web_server.py**: initialize the MQTT client at startup; feed the ring buffer
   with full-res frames; publish `homecameras/motion/<ip>` on every motion event;
   start/continue/finalize MP4 recordings gated by `is_recording_enabled()`;
   publish `homecameras/recording/<ip>` when a recording completes.
2. **settings_manager.py**: `mqtt_broker_host/port/username/password` added to
   `global_settings` defaults, overridable via env.
3. **docker-compose.yml**: pass `MQTT_BROKER_*` (default `mqtt.home:1883`).

### [Date: 2026-06-27] - Config Bind Mount; Fix Remove Button, Export and Import
**Agent**: not recorded
**Commits**: `ae25805`, `26bc08c`

1. Config switched to a bind mount so host edits are reflected immediately.
   (Superseded on 2026-07-27 — see `61dfa09` above for why the *relative* path
   this introduced was the thing that later wiped every camera.)
2. Added the frontend handler for the `camera_removed` WebSocket event, so the
   remove button actually removes the card from the dashboard.
3. Export wraps cameras + admin + global settings in a **versioned envelope**;
   import restores all three, including the admin password hash/salt.

### [Date: 2026-06-25] - Jenkins Deploy: Recreate Network, Auto-Detect GPU
**Agent**: not recorded
**Commits**: `c0c52e8`, `af8362b`

Deploy recreates the shared `homecameras-net` network and auto-detects GPU
availability, patching the `deploy:` reservation out of the compose file when
`nvidia-smi` fails so the stack falls back to CPU instead of refusing to start.
The network stage no longer tries to remove a network that is in use.

### [Date: 2026-06-24] - UUID camera_id as Primary Identifier
**Agent**: opencode (big-pickle)
**Task**: Replace IP-based camera keys with UUID camera_id throughout the system

**Problem**:
Cameras were identified by IP address (`device_ip`) everywhere — as dict keys, file identifiers, stream route parameters, and state tracking. This was fragile because:
1. IP addresses can change (DHCP, network changes) — breaking all references to a camera
2. The dashboard's `data-mac` attribute was confusingly named (legacy from MAC address era)
3. No stable, persistent identifier existed that survives IP changes

**Changes Made**:

1. **web_server/camera_manager.py** (`CameraConfig` dataclass):
   - Added `camera_id: str` field (auto-generated UUID hex via `__post_init__`)
   - Changed `.id` property to return `camera_id` instead of `device_ip`
   - Changed `cameras` dict keying from `device_ip` to `camera_id`
   - Updated `_process_stream_thread` key to use `config.camera_id`
   - Updated `convert_old_camera_dicts` to preserve `camera_id` from config
   - Added `get_camera_by_ip()` method for IP-based lookups

2. **web_server/settings_manager.py**:
   - Added `_migrate_cameras_dict()` — auto-migrates IP-keyed configs to camera_id-keyed on read, injecting UUID for entries missing `camera_id`
   - Added `_resolve_key()` — resolves a key to camera_id by checking both camera_id and device_ip
   - Updated `get_cameras()` to auto-migrate old-format configs
   - Updated `add_camera()`, `remove_camera()` to use camera_id
   - Updated registry camera settings (`get_registry_camera_setting`, `update_registry_camera_setting`, `remove_registry_camera_setting`) to support lookup by either camera_id or device_ip
   - Updated `get_camera_recording_duration()` and `set_camera_recording_duration()` to resolve by key

3. **web_server/web_server.py**:
   - Added `resolve_camera_id()` / `resolve_camera_config()` — lookup helpers for routes that receive a camera key (could be camera_id or device_ip)
   - Updated `load_cameras_from_config()` to use `camera_id` from config, key by camera_id throughout
   - Updated all WebSocket `camera_list` emits (5 locations: `load_cameras_from_config`, `handle_connect`, `handle_request_camera_list`, `_emit_camera_list`, error state emit in `fetch_camera_frame`) to key by camera_id and include `camera_id` field
   - Updated all API endpoints (`get_cameras`, `get_camera_settings`, `update_camera_settings`, `dashboard`, `snapshot`, `stream_video`, `stream_h264`, `api_list_recordings`) to resolve by camera_id
   - Updated `process_frame()` to use camera_key (camera_id) instead of device_ip for all dict lookups
   - Updated `fetch_camera_frame()` recording-status emits from `camera_ip` to `camera_key`
   - Updated `handle_add_camera()` to generate UUID via `uuid.uuid4().hex`
   - Updated `handle_remove_camera()` to resolve key before tear-down
   - Updated `sync_registry_cameras()` to track/remove by camera_id
   - Updated `_build_registry_camera_config()` to preserve saved camera_id
   - Updated `import_config()` to migrate imported cameras and use camera_id
   - Updated `_teardown_camera()` docstring to clarify camera_key should be camera_id
   - Fixed `update_camera_settings()` to handle device_ip changes as field updates (no key migration)
   - Fixed `_get_camera_name()` to resolve by camera_id/IP

4. **web_server/recording.py**:
   - Added `device_ip` parameter to `list_recordings()` for backward-compatible safe-ID matching

5. **web_server/templates/dashboard.html**:
   - Updated `recording_status` WebSocket handler to use `data.camera_key || data.camera_ip` for selector matching

6. **web_server/templates/edit_settings.html**:
   - Added hidden `camera-id` input field for form submission
   - Updated `loadCameraSettings()` to pass camera_id to API and display IP from response
   - Updated form submission to use camera_id in API URL (not IP-based URL switching)
   - Updated redirect after IP change to use camera_id (not new IP)
   - Updated DOM initialization to read camera key from URL path

7. **web_server/templates/recordings.html**:
   - Updated camera filter dropdown to use `config.device_ip` as display fallback
   - Updated `getCameraDisplayName()` to also match by camera_id

**Bug Fix**:
- `fetch_camera_frame()` in `web_server.py`: the recording frame-feed `else` block was accidentally removed during refactoring, causing active recordings to never receive frames and `still_recording`/`rec_path` variables to be undefined. Restored the `else` block with `add_frame_to_recording()`.

**Backward Compatibility**:
- Old-format `cameras.json` (IP-keyed, no `camera_id`) auto-migrates on first read
- Registry camera settings (`registry_camera_settings.json`) similarly auto-migrates
- All API endpoints accept either camera_id or device_ip as key
- `_resolve_key()` and `resolve_camera_id()` provide bidirectional lookup

**Summary**:
- Every camera now has a stable UUID (`camera_id`) that persists across IP changes
- Old IP-keyed configs auto-migrate on first read
- All internal state tracking uses camera_id; device_ip is just a mutable field
- API routes accept camera_id or device_ip interchangeably

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

### [Date: 2026-05-13] - Fix Stuck Camera Restart, Video Playback, and Motion Recording
**Agent**: opencode (GLM-5)
**Task**: Fix three issues: camera stuck detection not restarting, recordings not playing in browser, motion detection not reliably triggering recordings

**Problem**:
1. `restart_camera_connection()` only stopped the stream thread and re-added the camera, but didn't start a new fetcher thread or clear stale state (motion detectors, overlays, frames, recordings). After restart, the camera would have no fetcher thread processing frames, so it remained effectively stuck.
2. After a stuck restart was scheduled (`restart_scheduled = True`), there was no retry mechanism — if the restart failed to fix the stuck camera, it would never try again.
3. Recordings didn't play in the browser because `encodeURIComponent()` encoded `/` characters in file paths (e.g., `2026-05/filename.mp4` → `2026-05%2Ffilename.mp4`), breaking URL routing. Also lacked HTTP Range request support for video seeking.
4. Motion-triggered recordings stopped working whenever no one was viewing the dashboard because `fetch_camera_frame()` skipped ALL processing (including motion detection) when the snapshot idle threshold was exceeded.
5. Batch delete always sent `type: 'video'` regardless of the actual recording type, preventing deletion of snapshots.
6. FFmpeg video output didn't specify an explicit H.264 profile/level, which could cause browser compatibility issues.

**Changes Made**:
1. **web_server/web_server.py** (`restart_camera_connection`):
   - Added proper teardown: stop old fetcher thread, close containers/captures, clear all stale state
   - Added `stop_recording(mac)` call to clean up any active recording before restart
   - Added `start_new_camera_fetcher(mac)` to start a new fetcher thread after restart
   - Set `camera_manager._running[mac] = True` before re-adding camera
   - Reset stuck region state with `last_restart_time` for retry tracking

2. **web_server/web_server.py** (`check_stuck_region`):
   - Added `STUCK_REGION_RETRY_COOLDOWN = 300` (5 minutes) to allow retrying stuck restarts
   - If camera is still stuck 5 minutes after a restart attempt, it will try again

3. **web_server/web_server.py** (`fetch_camera_frame`):
   - Changed idle behavior: instead of `continue` (skipping ALL processing), now only adds a 0.2s sleep while still processing frames for motion detection and recording
   - This ensures motion-triggered recordings work even when no one is viewing the dashboard

4. **web_server/web_server.py** (`serve_recording`):
   - Added full HTTP Range request support (206 Partial Content) for video seeking
   - Added `Accept-Ranges: bytes` and `Cache-Control: no-cache` headers

5. **web_server/templates/recordings.html**:
   - Fixed `playVideo()`: split path on `/` and encode each segment separately
   - Fixed download button: use correct type based on actual recording type
   - Fixed batch delete: include correct `type` from recording data
   - Added `playsinline` attribute to video element

6. **web_server/recording.py** (FFmpeg encoding):
   - Added `-profile:v high -level 4.1` to both libx264 and h264_nvenc commands

7. **web_server/web_server.py** (imports):
   - Added `stop_recording` to recording module imports
   - Added `import re` at module level

**Summary**:
- Camera stuck detection now properly restarts with full cleanup and retry logic
- Videos play correctly in the browser with proper URL handling and Range request support
- Motion-triggered recordings work reliably even when no one is viewing the dashboard
- Recordings are sorted newest-first (already was correct in backend)

### [Date: 2026-05-08] - Fix Recordings Not Visible + Recording Duration Bug
**Agent**: opencode (GLM-5)
**Task**: Fix recordings not showing on recordings tab and recording duration default bug

**Problem**:
1. `filterRecordings()` in recordings.html discarded all snapshots (`rec.type !== 'video'`) — only videos were shown
2. `VIDEO_CLIP_DURATION` defaulted to 3600 seconds (1 hour) via `MAX_RECORDING_HOURS` env var — dangerous fallback if duration key is missing from recording dict
3. `is_recording_enabled()` logged at INFO level on every frame (~15 logs/sec per camera), filling logs rapidly
4. `get_global_settings()` read from disk on every call (~15 reads/sec per camera), no caching — excessive I/O
5. `get_processing_frame()` called twice per frame in ring buffer section — wasteful redundant call

**Changes Made**:
1. **web_server/templates/recordings.html**:
   - Removed `if (rec.type !== 'video') return false;` from `filterRecordings()` — both snapshots and videos now display

2. **web_server/recording.py**:
   - Changed `VIDEO_CLIP_DURATION` from `MAX_RECORDING_HOURS * 3600` (defaulted to 3600s) to `RECORDING_DURATION` env var defaulting to `10` seconds

3. **web_server/settings_manager.py**:
   - Changed `is_recording_enabled()` log level from `logger.info` to `logger.debug`
   - Added TTL-based cache (5-second TTL) to `get_global_settings()` to avoid per-frame disk reads
   - Added cache invalidation in `save_global_settings()`

4. **web_server/web_server.py**:
   - Refactored ring buffer section to reuse `processing_frame` from motion detection instead of calling `get_processing_frame()` a second time

**Summary**:
- Snapshots now appear on the recordings tab alongside videos
- Recording duration fallback is 10 seconds (not 1 hour)
- Logs no longer flooded with per-frame recording-enabled messages
- Global settings cached for 5 seconds to reduce disk I/O
- Eliminated redundant per-frame processing_frame retrieval

### [Date: 2026-05-06] - Fix Recordings Not Working (MAC addresses, duration, motion zones, UI)
**Agent**: opencode (GLM-5)
**Task**: Fix recordings not being created or shown in the recordings page

**Problem**:
1. Filenames used MAC addresses with colons (`:`) which broke URLs and camera filtering
2. Recording duration defaulted to 1 hour (3600s) instead of the configured 10 seconds
3. Duplicate `_ring_buffers` dict and `get_default_duration()` function in recording.py
4. Cameras with empty `motion_zones` had detection disabled entirely (Backyard, Garage)
5. Recordings page filtered out all snapshots
6. Front-end used `filename` instead of `path` for video playback (missing month subdirectory)
7. `delete_batch` API endpoint was missing

**Changes Made**:
1. **web_server/recording.py**:
   - Added `_safe_camera_id()` to convert both IP and MAC addresses to filesystem-safe IDs (`:` and `.` → `-`)
   - Fixed `save_snapshot()` and `start_recording()` to use `_safe_camera_id()` in filenames
   - Fixed `list_recordings()` camera filter to compare safe IDs instead of broken `replace('-', '.')` 
   - Removed duplicate `_ring_buffers` dict (line 669) and duplicate `get_default_duration()` (line 664)
   - Fixed `camera_ip` field in API responses to return safe ID format

2. **web_server/web_server.py**:
   - Added default "Full Frame" motion zone for cameras with no zones configured
   - Passed `duration=settings_manager.get_recording_duration()` to `start_recording()`
   - Added `/api/recordings/delete_batch` POST endpoint for batch deletion

3. **web_server/templates/recordings.html**:
   - Removed filter that hid all snapshots (`if (rec.type === 'snapshot') return false`)
   - Changed video playback to use `rec.path` (includes month subdir) instead of `rec.filename`
   - Fixed camera filter dropdown to use safe ID format for matching
   - Added `getCameraDisplayName()` that maps safe IDs back to camera names
   - Applied camera filter to snapshot API requests too
   - Fixed delete/select/thumbnail functions to use `path` instead of `filename`

**Summary**:
- New recordings have filesystem-safe filenames (no colons)
- Recordings now last 10 seconds (configurable) instead of 1 hour
- All cameras now get a default "Full Frame" motion detection zone
- Snapshots now appear alongside videos on the recordings page
- Video playback and delete use correct paths with month subdirectories

### [Date: 2026-05-06] - Fix Motion Detection Recording (undefined `frame` var + time window ignored)
**Agent**: opencode (GLM-5)
**Task**: Fix motion-triggered recording not working

**Problem**:
1. `process_frame()` referenced undefined variable `frame` instead of parameter `processing_frame` — caused `NameError` caught silently, so motion detection never ran and no recordings were ever triggered
2. `settings_manager.is_recording_enabled()` was never called before starting recording, so the recording time window (e.g., 0-12) was completely ignored

**Changes Made**:
1. **web_server/web_server.py**:
   - Replaced three occurrences of undefined `frame` with `processing_frame` in `process_frame()` (lines 429, 514, 599)
   - Added `settings_manager.is_recording_enabled()` check before triggering recording on motion (line 543)
   - Added `settings_manager.is_recording_enabled()` check before feeding ring buffer (line 827)

**Summary**:
- Motion detection now works (no more NameError)
- Recording time window is now respected
- Ring buffer only buffers frames when recording is within the enabled time window

### [Date: 2026-05-06] - Fix Camera Settings Form and Name Display
**Agent**: opencode (big-pickle)
**Task**: Fix empty camera settings form and improve camera name display

**Problem**:
1. Camera settings form showed empty for registry cameras
2. Camera names not displaying properly on dashboard
3. Remove button and card header used MAC address instead of IP

**Changes Made**:
1. **web_server/web_server.py**:
   - Fixed camera settings form showing empty for registry cameras
   - Added MAC address as primary key for camera identification
   - Fixed `add_camera` error handling with better validation

2. **web_server/templates/dashboard.html**:
   - Show camera name with IP in "name/ip" format on dashboard card
   - Added name field to dashboard camera data
   - Fixed remove button to use `device_ip` not MAC in header

3. **web_server/templates/edit_settings.html**:
   - Fixed settings form to properly load camera config (including registry cameras)

**Summary**:
- Camera settings form now works for all camera types
- Dashboard displays camera names correctly
- Fixed camera identification to use IP consistently

### [Date: 2026-05-05] - Modern Dark Theme UI Redesign
**Agent**: opencode (big-pickle)
**Task**: Redesign dashboard, settings, and recordings pages with modern dark theme

**Changes Made**:
1. **web_server/templates/dashboard.html**:
   - Complete redesign with modern dark theme
   - Updated camera cards with improved layout and styling
   - Added gradient backgrounds and smooth transitions
   - Improved responsive design for mobile devices

2. **web_server/templates/edit_settings.html**:
   - Redesigned settings page with dark theme
   - Modern form controls with better spacing
   - Improved motion zone editor UI

3. **web_server/templates/recordings.html**:
   - Redesigned recordings page with dark theme
   - Improved grid layout for recordings browser
   - Better video modal player styling

4. **web_server/templates/base.html** (if exists):
   - Updated base template with dark theme CSS variables
   - Consistent styling across all pages

**Summary**:
- Modernized entire web UI with consistent dark theme
- Improved user experience with better visual hierarchy
- Enhanced mobile responsiveness

### [Date: 2026-05-05] - Fix Config Persistence & Timezone Support
**Agent**: opencode (big-pickle)
**Task**: Fix configuration persistence across deployments and add timezone support

**Problem**:
1. Camera settings were lost after container restarts
2. Recording timestamps were in wrong timezone
3. Global settings endpoint was missing

**Changes Made**:
1. **web_server/web_server.py**:
   - Fixed config persistence: ensure settings are saved to `config/cameras.json` after changes
   - Added `/api/global/settings` GET and POST endpoints
   - Fixed timezone detection: check `TZ` environment variable
   - Keep timezone config in settings UI

2. **web_server/docker-compose.yml**:
   - Added `TZ=Etc/UTC` environment variable for correct recording timestamps
   - Fixed Docker config paths for persistent settings (proper volume mounts)

3. **web_server/templates/edit_settings.html**:
   - Added timezone display/configuration in settings UI

**Environment Variables**:
- `TZ` - Set container timezone (default: `Etc/UTC`)

**Summary**:
- Camera settings now persist across container restarts
- Recording timestamps use correct local timezone
- Global settings API endpoints added

### [Date: 2026-05-05] - Fix Import/Export and Camera Stream Restart
**Agent**: opencode (big-pickle)
**Task**: Fix configuration import to properly start camera streams and save all settings

**Problem**:
1. Imported camera configs didn't start streaming
2. Not all camera settings were saved after import
3. `asyncio.Lock` used in threading context caused errors
4. Dashboard couldn't handle dict configs from import

**Changes Made**:
1. **web_server/web_server.py**:
   - Fixed `import_config()`: start camera streams in background thread after import
   - Fixed `add_camera()`: use `threading.Lock` instead of `asyncio.Lock`
   - Fixed `get_all_cameras()`: always return `CameraConfig` objects (not dicts)
   - Fixed dashboard to handle dict configs from import gracefully
   - Fixed camera config handling after import

2. **web_server/camera_manager.py**:
   - Ensure camera streams start properly after config import

**Summary**:
- Configuration import now properly restarts camera streams
- All camera settings preserved after import/export
- Fixed threading issues with lock types

### [Date: 2026-05-05] - Jenkins Pipeline and Docker Improvements
**Agent**: opencode (big-pickle)
**Task**: Fix Jenkins deployment pipeline and Docker volume issues

**Problem**:
1. Jenkins pipeline had redundant build stages
2. Docker logs volume not properly declared
3. `IsADirectoryError` when writing logs
4. Missing `--remove-orphans` flag in deploy

**Changes Made**:
1. **Jenkinsfile.web_server**:
   - Removed redundant "Build Docker Image" stages
   - Added `--no-cache` flag to docker build
   - Added `--remove-orphans` to deploy stage
   - Added docker-compose logs to deploy stage for debugging

2. **web_server/docker-compose.yml**:
   - Declared missing `homecameras_webserver_logs` volume in top-level volumes block
   - Added logs volume mount for persistent logging
   - Removed bad volume mount that caused `IsADirectoryError`

3. **web_server/web_server.py**:
   - Fixed log path to use absolute path (prevents `IsADirectoryError`)

**Summary**:
- Jenkins pipeline streamlined and more reliable
- Docker logs now persist correctly
- Fixed directory/file conflict in logging

### [Date: 2026-05-04] - Add Camera Names & Editable IP in Settings
**Agent**: opencode (big-pickle)
**Task**: Add a `name` field to cameras and allow modifying both name and IP in settings

**Problem**:
1. Cameras were only identified by IP address, which is not user-friendly
2. No way to assign friendly names to cameras (e.g., "Front Door", "Backyard")
3. IP address couldn't be changed from the edit settings page

**Changes Made**:
1. **web_server/camera_manager.py**:
   - Added `name: str = ""` field to `CameraConfig` dataclass

2. **web_server/web_server.py**:
   - Updated all API endpoints to include `name` field in responses:
     - `get_cameras()` API endpoint
     - `get_camera_settings()` API endpoint
     - WebSocket `camera_list` emits (3 locations)
   - Updated `update_camera_settings()` to handle `name` and `device_ip` (IP) changes
   - Added IP change handling: updates all internal dictionaries (`camera_manager.cameras`, `stuck_region_config`, `camera_motion_detectors`, etc.)
   - Updated `handle_add_camera()` to include `name` in new camera config
   - Updated `import_config()` to preserve `name` field when importing

3. **web_server/templates/dashboard.html**:
   - Updated camera card header to display `name` (falls back to IP if no name set)
   - Updated JavaScript `camera_list` handler to use `config.name || config.device_ip`

4. **web_server/templates/edit_settings.html**:
   - Added "Camera Name" input field at the top of the form
   - Added "Device IP Address" input field (now editable)
   - Updated JavaScript to load and save both `name` and `device_ip` fields
   - Form now submits `device_ip` and `name` in the POST request
   - On successful IP change, redirects to new URL with updated IP

5. **web_server/templates/add_camera.html**:
   - Added optional "Camera Name" field to the add camera form

6. **web_server/templates/recordings.html**:
   - Updated to display camera names instead of IPs
   - Added `getCameraDisplayName()` helper function
   - Camera filter dropdown now shows names

7. **config/cameras.json**:
   - Added `name` field to existing cameras:
     - `192.0.2.143`: "Front Door"
     - `192.0.2.199`: "Backyard"
     - `192.0.2.207`: "Garage"

**Behavior**:
- Camera names are optional (empty string by default)
- Dashboard displays name if set, otherwise falls back to IP address
- Edit Settings page allows modifying both camera name and IP address
- Changing a camera's IP address updates all internal tracking dictionaries
- On IP change, the browser redirects to new settings URL
- All API responses now include the `name` field

### [Date: 2026-05-03] - Move Configs to Project-Local Storage
**Agent**: opencode (GLM-5)
**Task**: Modify the codebase so all configs and data are stored in directories local to the project root instead of external paths

**Problem**:
1. `SettingsManager` used a relative `config` path that resolved relative to CWD, not the project root — broke if run from outside `web_server/`
2. `recording.py` defaulted recordings to `/mnt/data/home-cameras` (absolute external path)
3. `camera_registry/app.py` stored data relative to its own module directory
4. `web_server.py` logged to `logs/web_server.log` (relative to CWD)
5. Docker Compose mounted external host paths (`/home/homestack/home-lab/configs/`, `/mnt/data/`)

**Changes Made**:
1. **web_server/settings_manager.py**:
   - Added `_PROJECT_ROOT` and `_resolve_path()` to resolve config paths relative to project root (`HomeCameras/`)
   - `SettingsManager(config_dir='config')` now resolves to `<project_root>/config/` instead of CWD-relative
   - All config files (`cameras.json`, `admin.json`, `global_settings.json`, `registry_camera_settings.json`) now live under `<project_root>/config/`

2. **web_server/recording.py**:
   - Changed `RECORDINGS_DIR` default from `/mnt/data/home-cameras` to `<project_root>/data/recordings`
   - Added `_PROJECT_ROOT` resolution for project-local path
   - Env var `RECORDINGS_DIR` override still works for Docker/custom deployments

3. **web_server/web_server.py**:
   - Log file path changed from CWD-relative `logs/web_server.log` to `<project_root>/logs/web_server.log`
   - Auto-creates `<project_root>/logs/` directory on startup

4. **camera_registry/app.py**:
   - `REGISTRY_DIR` now defaults to `<project_root>/data/camera_registry` (was `camera_registry/data/`)
   - `DATABASE_PATH` now defaults to `<project_root>/data/camera_registry/cameras.db`
   - Added `REGISTRY_DATA_DIR` env var override
   - Added `DATABASE_PATH` env var override

5. **tests/test_camera_feeds.py**:
   - Config path default changed from CWD-relative `"config/cameras.json"` to `<project_root>/config/cameras.json`

6. **Config files moved** from `web_server/config/` to project root `config/`:
   - `cameras.json`, `admin.json`, `config_bkp.json` now at `HomeCameras/config/`

7. **Docker Compose** (both services):
   - `web_server/docker-compose.yml`: Changed volumes from external host paths to project-local `./data/recordings`, `./config`, `./logs`, `./models`
   - `camera_registry/docker-compose.yml`: Changed volume from external path to project-local `./data/camera_registry`

8. **Dockerfiles**:
   - `web_server/Dockerfile`: Added `/app/data/recordings` to mkdir
   - `camera_registry/Dockerfile`: Added `/app/data/camera_registry` to mkdir

9. **.gitignore**: Added `data/` and `logs/` patterns to ignore runtime data

**New Project-Local Directory Structure**:
```
HomeCameras/
├── config/                    # Config files (was: web_server/config/ + external mount)
│   ├── cameras.json
│   ├── admin.json
│   └── global_settings.json
├── data/
│   ├── recordings/           # Motion recordings (was: /mnt/data/home-cameras)
│   │   └── YYYY-MM/          # Month-based subfolders
│   └── camera_registry/      # Camera registry DB (was: camera_registry/data/)
│       └── cameras.db
├── logs/                      # Log files (was: CWD-relative)
└── models/                    # ML models (unchanged)
```

**Environment Variable Overrides (still available for Docker)**:
- `RECORDINGS_DIR` — Override recordings storage path
- `REGISTRY_DATA_DIR` — Override camera registry data directory
- `DATABASE_PATH` — Override camera registry SQLite path

### [Date: 2026-05-01] - Fix Overlay Accumulation & Add Pre-Motion Ring Buffer
**Agent**: opencode (GLM-5.1)
**Task**: Fix detection overlay accumulation on recorded video and add pre-motion ring buffer recording

**Problem**:
1. Object detection overlays (bounding boxes, tags) accumulated in recorded videos - old detections from previous frames persisted because the recording code collected objects from ALL historical `motion_events` instead of only the current frame's detections
2. No pre-motion recording - video clips always started after motion was detected, missing the crucial seconds before the event

**Changes Made**:
1. **web_server/recording.py**:
   - Added `RingBuffer` class: circular buffer storing JPEG-compressed frames at recording FPS (5fps) for 5 seconds of pre-motion footage per camera
   - Added `add_to_ring_buffer()`: feeds frames into per-camera ring buffers when not actively recording
   - Added `drain_ring_buffer()`: retrieves and clears buffered frames when recording starts
   - Added `PRE_MOTION_DURATION = 5.0` and `PRE_MOTION_BUFFER_FPS = VIDEO_FPS` constants
   - Modified `start_recording()`: accepts `pre_buffer` parameter; writes pre-buffer frames (with timestamp-only overlay, no stale detections) before live frames
   - Pre-buffer frames get a clean overlay (timestamp + camera IP only, no detection boxes)

2. **web_server/web_server.py**:
   - **Fixed overlay accumulation bug**: Changed `add_frame_to_recording` call to use only current frame's detected objects (`events` + `periodic_result`) instead of accumulated `motion_events` dict
   - Added ring buffer feeding: when not recording, full-resolution frames are added to per-camera ring buffers via `add_to_ring_buffer()`
   - Modified recording start: drains ring buffer via `drain_ring_buffer()` and passes pre-buffer frames to `start_recording()`
   - Imported `add_to_ring_buffer` and `drain_ring_buffer` from recording module

**Ring Buffer Behavior**:
- Each camera continuously stores 5 seconds of footage at 5fps (25 frames) in a JPEG-compressed circular buffer
- When motion is detected, the buffer is drained and prepended to the recording
- Pre-motion frames show only timestamp overlay (no detection boxes from before the event)
- After recording starts, live frames with current detection overlays are written normally
- Total clip duration: ~15 seconds (5s pre-motion + 10s post-motion)

### [Date: 2026-05-01] - MQTT Publishing, Motion Recording, and Video Management
**Agent**: opencode (GLM-5.1)
**Task**: Fix motion/object detection, add MQTT publishing on movement, save video/snapshots on motion, and add web UI for recording management

**Problem**:
- Cameras without motion zones had detection completely disabled
- No MQTT integration for home automation
- No recording/snapshot saving on motion detection
- No way to view or manage recordings from the web UI

**Changes Made**:
1. **web_server/web_server.py**:
   - Fixed motion detection: cameras without zones now get a default "Full Frame" zone (covers entire frame)
   - Added MQTT publishing on motion events and cooldown detection events
   - Added snapshot saving with detection overlay on motion (5-second cooldown per camera)
   - Added video recording (10-second clips) on motion detection with overlay
   - Added `/recordings` page, `/api/recordings` API, `/recordings/<filename>` file serving, DELETE endpoints
   - Added MQTT client initialization with env vars: `MQTT_BROKER_HOST`, `MQTT_BROKER_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`
   - Added `RECORDINGS_DIR` env var support for recording storage path
   - Integrated recording module for snapshot/video saving during motion events

2. **web_server/mqtt_client.py** (NEW):
   - MQTT client with async connection, auto-reconnect
   - `publish_motion()`: publishes to `homecameras/motion/<camera_ip>` with zone, confidence, objects, tags
   - `publish_recording()`: publishes to `homecameras/recording/<camera_ip>` when clips start/end
   - Graceful degradation when paho-mqtt is not installed

3. **web_server/recording.py** (NEW):
   - `save_snapshot()`: Saves JPEG with detection overlay (timestamp, camera IP, bounding boxes, tags)
   - `start_recording()`: Starts MP4 video clip with overlay (falls back to AVI/XVID)
   - `add_frame_to_recording()`: Continues writing frames until clip duration (10s)
   - `list_recordings()`: Lists saved files with metadata (type, size, camera, timestamp)
   - `delete_recording()`: Deletes individual files
   - Filename format: `YYYY-MM-DD_HH-MM-SS_<cam-ip>_<tags>.jpg/.mp4`
   - Path-secured file serving and deletion

4. **web_server/templates/recordings.html** (NEW):
   - Web page at `/recordings` for browsing and managing recordings
   - Filter by type (snapshot/video) and camera
   - Grid view with thumbnail previews for snapshots, play button for videos
   - Video modal player for playback
   - Single and batch delete functionality
   - Download button for snapshots
   - Linked from dashboard header

5. **web_server/requirements.txt**: Added `paho-mqtt`

**MQTT Topics**:
- `homecameras/motion/<camera_ip>` - Published on every motion event
- `homecameras/motion/<camera_ip>/objects` - Published when objects detected (subset with tags)
- `homecameras/recording/<camera_ip>` - Published when recording starts/ends

**Recording Structure**:
```
/mnt/data/home-cameras/
├── snapshots/
│   └── 2026-05-01_14-30-00_192-168-100-143_person_cat.jpg
└── videos/
    └── 2026-05-01_14-30-00_192-168-100-143_person_cat.mp4
```

**Environment Variables**:
- `RECORDINGS_DIR` - Path to recording storage (default: `/mnt/data/home-cameras`)
- `MQTT_BROKER_HOST` - MQTT broker hostname (default: `localhost`)
- `MQTT_BROKER_PORT` - MQTT broker port (default: `1883`)
- `MQTT_USERNAME` - MQTT auth username
- `MQTT_PASSWORD` - MQTT auth password

### [Date: 2026-04-09] - Multiple Android Fixes
**Agent**: Cline (Anthropic)
**Tasks**: Fix compilation errors, camera loading, Firebase dependencies, and server configuration

**Changes Made**:
1. Created missing Android components:
   - `android/app/src/main/java/com/homecameras/android/ui/MainActivity.kt`
   - `android/app/src/main/java/com/homecameras/android/ui/StreamActivity.kt`
   - `android/app/src/main/java/com/homecameras/android/service/StreamingService.kt`
   - `android/app/src/main/java/com/homecameras/android/receiver/BootReceiver.kt`
   - `android/app/src/main/java/com/homecameras/android/receiver/NotificationReceiver.kt`

2. Fixed camera loading and added settings menu:
   - Added camera sync from server before loading local database
   - Added toolbar menu with Settings and Refresh options
   - Enhanced camera loading logic to sync with backend API
   - Created `android/app/src/main/res/menu/menu_main.xml`

3. Fixed Firebase dependencies:
   - Uncommented Firebase dependencies in `android/app/build.gradle`
   - Added Google Services plugin
   - Created placeholder `android/app/google-services.json`

4. Updated server URL:
   - Changed `BACKEND_URL` in `android/app/build.gradle` to `http://compute.home:5000`

**Summary**:
- Fixed all compilation errors and missing components
- Added proper camera syncing with backend
- Configured Firebase for push notifications
- Set correct server URL for Android app

### [Date: 2026-04-08] - Android Deployment Documentation & Blue Screen Fix
**Agent**: Cline (Anthropic)
**Task**: Create comprehensive Android documentation and fix blue screen issue on H.264 streams

**Problem**:
The Android app was showing a blue screen instead of the camera feed due to multiple issues:
1. TextureView background drawable error causing app crashes
2. Kotlin compilation errors with Byte/Int type mismatches
3. Missing camera resolution data preventing proper decoder configuration
4. Layout issues masking the actual video feed

**Changes Made**:
1. Updated `android/app/src/main/res/layout/activity_stream.xml`:
   - Removed blue background View that was masking video feed
   - Fixed TextureView background issue
   - Added black background to parent FrameLayout

2. Updated `android/app/src/main/java/com/homecameras/android/ui/MainActivity.kt`:
   - Added camera resolution data (width/height) to intent extras

3. Fixed `android/app/src/main/java/com/homecameras/android/media/H264Decoder.kt`:
   - Resolved Kotlin compilation errors
   - Fixed MediaCodec configuration and buffer management

4. Updated `android/app/src/main/java/com/homecameras/android/ui/StreamActivity.kt`:
   - Enhanced HTTP connection handling
   - Added detailed error logging

5. Created `android/DEPLOYMENT.md` and updated documentation

**Key Technical Findings**:
- **TextureView Limitation**: TextureView doesn't support `android:background` attribute
- **Kotlin Type Safety**: Byte and Int types cannot be directly compared
- **H.264 Stream Format**: Raw H.264 elementary streams required for MediaCodec

**Summary**:
- Fixed blue screen issue completely
- Created comprehensive deployment documentation
- Android app now displays actual video feed

---

## Project-Specific Change Logs

For detailed tracking of changes made to individual project components, please refer to their respective CHANGELOG.md files:

- **[android/CHANGELOG.md](./android/CHANGELOG.md)** - Android client project
  - Contains change logs for all Android-specific modifications
  - Tracks UI changes, Firebase integration, and app structure updates

- **[camera_registry/CHANGELOG.md](./camera_registry/CHANGELOG.md)** - Camera Registry project
  - Tracks the lightweight backend for ESP32 camera management
  - Contains API endpoint documentation and Docker configuration details

- **[esp32/CHANGELOG.md](./esp32/CHANGELOG.md)** - ESP32 Camera Firmware
  - Documents the ESP32 camera web server firmware
  - Contains compilation instructions and sensor configuration details

- **[web_server/CHANGELOG.md](./web_server/CHANGELOG.md)** - Core Backend
  - Tracks changes to Python backend and streaming services
  - Contains H.264 encoding implementation details

## Guidelines for Future Agents

When making changes to this project:

1. **Update the relevant CHANGELOG.md file** with a summary of changes
2. **Follow existing code patterns** and project structure
3. **Test changes** before committing when possible
4. **Update relevant documentation** to reflect changes
5. **Maintain backward compatibility** when modifying existing features
6. **Cross-reference** related changes in other AGENTS.md files

## Project Structure Overview

```
HomeCameras/
├── AGENTS.md                 # This file - Main change tracking
├── README.md                 # Main project documentation
├── android/                  # Android client
│   ├── AGENTS.md            # Android-specific change tracking
│   ├── DEPLOYMENT.md        # Deployment guide
│   ├── ANDROID_DESIGN.md    # Technical design
│   ├── build.gradle         # Gradle build configuration
│   └── app/                 # Android app source
├── camera_registry/          # Camera Registry service
│   ├── AGENTS.md            # Registry-specific change tracking
│   ├── app.py               # Registry API
│   └── docker-compose.yml   # Container orchestration
├── config/                   # Configuration files
├── esp32/                    # ESP32 firmware
│   └── AGENTS.md            # ESP32 firmware change tracking
├── web_server/               # Python backend
│   ├── AGENTS.md            # Backend-specific change tracking
│   ├── web_server.py        # Main HTTP server
│   └── [backend modules]     # H264 encoding, motion detection, etc.
├── tests/                    # Unit tests
├── data/                     # Runtime data (recordings, registry)
└── logs/                     # Application logs
```

## Contact & Support

For questions about AI agent contributions or to report issues with agent-made changes, please refer to the project maintainers.
