# Camera Registry - AI Agents Documentation & Change Log

### [2026-08-10] "Database unavailable" was never about the database

The ESP boards were told `Database unavailable` on every call. The database was
fine — 24 KB, readable, on a disk with 617 GB free. The process had simply run
out of file descriptors, so `sqlite3.connect` could not open anything at all,
`get_db_connection()` returned None, and all ten of its callers reported the
only thing they know how to report.

The descriptors were eventlet hubs, 1018 of them:

- `SocketIO(app, ...)` never said which `async_mode` it wanted, and
  Flask-SocketIO picks **eventlet** whenever eventlet is merely importable. It
  was in `requirements.txt` though nothing imports it.
- The app is served by `app.run()` — werkzeug's threaded server, a new OS
  thread per request. Eventlet builds its hub lazily *per thread*, so every
  request created a hub with a fresh epoll fd and dropped it unclosed when the
  thread exited. `web_server`'s SocketIO client polls continuously and
  reconnects forever (`reconnection_attempts=0`), so the count only ever went
  up: ~1000 in the eleven hours the container had been up. Then the 1024 soft
  limit, and every `open()` in the process started failing.
- Measured, old image vs new, 150 `transport=polling` requests: **15 leaked
  eventpoll fds → 0**, total fds flat at 4.

`async_mode='threading'` now matches how the app is actually run, and eventlet
is out of `requirements.txt` so the auto-detection cannot come back. Nothing
about WebSocket support changes — werkzeug's server never served one, and the
transport was already polling.

**The data directory is resolved properly now**, which is a fault found while
reading the above rather than a cause of it. The default was
`<file>/../data/camera_registry` — correct in a checkout, where this file sits
in `HomeCameras/camera_registry/`, but wrong in the image, where the Dockerfile
copies the *contents* of `camera_registry/` into `/app` and flattens that level
away. `..` escaped to `/`, so the default was `/data/camera_registry`, which
the non-root `registry` user cannot create. docker-compose.yml always sets
`REGISTRY_DATA_DIR`, so production never met it, but `docker run` without that
variable died at import on a bare PermissionError traceback. The default is now
`<app dir>/data/camera_registry`: `/app/...` in the image, exactly where compose
mounts the volume, and the project folder in a checkout.

Two rules go with it:

- an explicit `REGISTRY_DATA_DIR`/`DATABASE_PATH` is honoured or fatal, never
  quietly swapped for somewhere else. A fallback would start the registry
  against an *empty* database and every camera in the house would read as
  never-registered — quieter than a crash, and much worse;
- the directory is **write-probed** at startup, because
  `os.makedirs(exist_ok=True)` succeeds on a read-only directory that already
  exists. Without the probe the first symptom is `sqlite3.connect` failing,
  `get_db_connection()` returning None, and "Database unavailable" again.

Both failures now exit non-zero naming the path, the source of the choice and
the uid. Logging is configured *before* any of this, since it used to be set up
afterwards and the one interesting line could not be logged.

**`/api/ping` answered `{"status": "healthy"}`, HTTP 200, throughout.** It put
the real state in a `database` field and returned 200 regardless, and that
field said `error: 'NoneType' object has no attribute 'cursor'` — the endpoint
called `.cursor()` on the None it had just been handed, so its own health probe
crashed on exactly the condition it existed to detect. Both the Docker
HEALTHCHECK and the Jenkinsfile's `Verify Camera Registry` stage test that URL,
so `docker ps` said `(healthy)` and the last deploy went green while the
registry refused every camera for eleven hours. It is 503 / `degraded` now,
which is the same lesson as `/api/<ip>/ping` last time: a check that cannot
report ill is not a check.

### [2026-08-09] The sample and the server disagreed about registration

A new ESP camera never appeared. The registry was healthy the whole time and
had **never once** received a registration — because the file anyone builds a
camera from could not produce one that would be accepted.

- `/api/register` reads `ip_address` from the **top level** and answers 400
  without it. `esp32_sample.c++` put it inside `metadata`, so every camera
  built from the sample was refused before it could appear.
- The sample did not compile either: `String(WiFi.localIP())` cannot be pasted
  between two adjacent string literals. It builds the body with `String`
  concatenation now, and sends `Content-Type: application/json`.
- The sample pinged `/api/<camera_id>/ping`; the registry keys everything on
  the address, so every heartbeat missed.
- **`/ping` answered `{"status":"ok"}` whatever you asked about.**
  `update_heartbeat` already returns whether it matched a row and the route
  threw it away, so a camera pinging the wrong path was told it was fine for
  ever while nothing was recorded, then aged quietly to "offline" in a server
  that had answered 200 to every request it ever made. It is a 404 now, and
  says what to do about it.
- `GET /` was a bare 404, so checking whether the registry was alive looked
  exactly like it being dead. It now reports itself and its endpoints.

`test_registration_contract.py` reads the payload keys out of the **real**
`esp32_sample.c++` rather than a copy, so the two cannot drift apart again.


This file tracks all changes made by AI agents specifically to the Camera Registry project.

## Recent Changes

### [Date: 2026-04-08] - Camera Registry Project Setup
**Agent**: Cline (Anthropic)
**Task**: Set up Camera Registry for ESP32 camera management and registration

**Changes Made**:
1. Created `camera_registry/app.py`:
   - REST API for camera registration and list endpoints
   - Camera data structure (id, ip, name, status, last_seen)
   - HTTP server running on port 5001
   - Simple JSON API responses

2. Created `camera_registry/requirements.txt`:
   - FastAPI and uvicorn dependencies
   - Uvicorn access log to camera_registry/logs/

3. Created `camera_registry/docker-compose.yml`:
   - Containerized camera registry service
   - Persistent logging to camera_registry/logs/

4. Created `camera_registry/test_registry.py`:
   - Basic test script for registering cameras
   - Testing camera list retrieval

5. Created `camera_registry/data/` and `camera_registry/logs/` directories

**API Endpoints**:
- `GET /api/cameras` - List all registered cameras
- `POST /api/cameras` - Register a new camera

**Summary**:
- Camera Registry provides a lightweight backend for managing ESP32 camera connections
- Used by Android app and web client to discover cameras on the network
- Provides JSON API for camera registration and status

## Camera Registry Project Structure

```
camera_registry/
├── app.py                    # Main FastAPI application
├── requirements.txt          # Python dependencies
├── docker-compose.yml        # Container orchestration
├── Dockerfile                # Container image definition
├── esp32_sample.c++          # ESP32 camera firmware example
├── test_registry.py          # Test script
├── data/                     # Camera data storage
├── logs/                     # Application logs
└── AGENTS.md                 # This file
```

## Contact & Support

For questions about Camera Registry contributions or to report issues, please refer to the project maintainers.