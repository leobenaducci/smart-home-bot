# mqtt-dashboard

The house's MQTT broker and a small web dashboard over it. Both live on
**`hub`** — which is the same machine as `mqtt.home` and `assistant.home`,
`192.168.1.10`.

**Two things with two separate lifecycles**, and the distinction matters more
than anything else in this repo:

| | What | How it runs | Deployed by |
|---|---|---|---|
| **Broker** | Mosquitto, the pinned upstream image | **Docker container**, port **1883** | `./home-stack deploy mqtt --only broker` |
| **Dashboard** | Node/Express + `ws` + `mqtt` (`dashboard/`) | **Docker container** `mqtt-dashboard`, port **21050** | `./home-stack deploy mqtt --only dashboard` |

Both are units of the one `mqtt` service in `deploy/manifest.yml`; the broker
unit lives in `deploy/units/mosquitto/`, not here. `mqtt_publish.sh` is a
convenience publisher for testing.

## Deploy the dashboard alone unless you mean to bounce the broker

**Restarting Mosquitto disconnects every MQTT citizen in the house at once** —
the voice panels, the cameras, the ntfy bridges. And because it runs
`persistence false`, retained `state` topics do not survive the restart, so
subscribers come back to an empty world until each publisher next speaks.

`./home-stack deploy mqtt` deploys **both** units and does exactly that. For a
dashboard change, name the unit:

```bash
./home-stack deploy mqtt --only dashboard
```

`--only broker` is the deliberate other half, for when the broker's own image
or config has changed.

`mosquitto-ctrl.sh` in this directory predates the deployer: it builds
Mosquitto from source and installs it as a **host systemd service**, which will
fight the deployed container for port 1883, and its `start` verb also launches
the dashboard the old way (`npm start` under `nohup`) and fights it for 21050.
It is kept for reference; do not run it against a deployed stack.

Conventions for everything on the broker — topic grammar, QoS, retention,
payload shape — are in
[`../docs/mqtt-conventions.md`](../../docs/mqtt-conventions.md),
which governs the whole stack.

## The dashboard

Serves `dashboard/public/index.html` on **21050**, styled in the same "The House"
palette as HomeCore. It subscribes to the broker, streams messages to the page
over a WebSocket, and keeps a searchable buffer: **10,000 messages, pruned at 7
days**. `GET /api/messages` filters by `topic`, `subtopic` and date range;
`/api/subtopics` lists what has been seen.

`network_mode: host` — it needs to reach the broker on the host and serve 21050
directly.

### MQTT → ntfy bridges

The dashboard's other job: forward matching MQTT traffic to a push
notification. A bridge is `{name, topic, ntfyUrl, ntfyTopic, titleTemplate,
priority}`; `topic` supports `+` wildcards matched level by level (`{topic}` in
`titleTemplate` is substituted with the message's topic). They are managed from
the page and stored in `bridges.json`.

**`bridges.json` must live outside the container and outside any workspace.**
`DATA_DIR` is what decides where it lands: unset, `server.js` falls back to
`__dirname` — `/app` inside the image, the ephemeral writable layer — and every
configured bridge is silently destroyed by the next
`docker compose up --force-recreate`. The compose file therefore sets the
container side to `/app/data` and bind-mounts `${MQTT_DASHBOARD_DATA_DIR:-./data}`
over it. The tracked `dashboard/bridges.json` is an empty `[]` seed, not the live
file.

This is the same class of bug that wiped every camera in HomeCameras and the
device names in SmartButton — see the root [`AGENTS.md`](../../README.md).

## Running

Local:

```bash
cd dashboard && docker compose up --build
```

Deploy: a **manual** Jenkins run (`Jenkinsfile`, agent `hub`) — pushing does
not deploy. It backs up the live `bridges.json` to
`/home/homestack/home-lab/configs/mqtt-dashboard-backups/<timestamp>/`
(keeping ten), rebuilds the container with `MQTT_DASHBOARD_DATA_DIR` pointed at
`/home/homestack/home-lab/configs/mqtt-dashboard`, then verifies two things:
that the dashboard answers on 21050, and that `/app/data` really is the mount —
because a container that is up is not the same as a dashboard that kept its
bridges.

## Configuration

| Env var | Default | Notes |
|---|---|---|
| `MQTT_BROKER` | `mqtt.home` | Host only. A full `mqtt://host:port` URL is still accepted, for the older form this service used to require. |
| `MQTT_PORT` | `1883` | |
| `PORT` | `21050` | Dashboard HTTP port |
| `DATA_DIR` | `/app/data` (image) | Container side. **Never leave unset in a deployment** — see above. |
| `MQTT_DASHBOARD_DATA_DIR` | `./data` | Host side of the bind mount. The pipeline overrides it. |

The Dockerfile's `MQTT_BROKER` default was once
`mqtt://host.docker.internal:1883` — a fourth name for the same machine that
only resolves under Docker Desktop, not on the Pi.
