# Migrating an existing install

This is for whoever plans the move — a person or another agent instance. It
assumes you have a household already running some of these services and want it
on this package, or that you have an install of this package from before a
rename and want it current.

Read `CLAUDE.md` first for the two-layer split. Nothing below overrides it: the
packaging layer supplies environment and verifies results, and never edits a
service to make it deployable.

## The one rule

**Live state never moves silently.** Every wipe this deployer guards against
happened to a deploy that reported success. A migration is mostly the work of
deciding, for each directory and each named volume, whether it is carried,
recreated, or deliberately dropped — and writing that decision down before
running anything.

`docs/backups.md` is the inventory. It is derived from the manifest's `state:`
entries, so it is the list of things a migration has to account for.

## Before you touch anything

```bash
./home-stack list                   what is enabled, and where it runs
./home-stack check   every compose variable is supplied
python3 deploy/test_deploy.py             manifest invariants, no config needed
./home-stack plan          the full plan, changing nothing
./deploy/sanitize.py --check              no household data in the tree
```

`--check-contract` needs a real config and secrets file; `test_deploy.py` does
not, so run that one first if you are working from a fresh clone.

Take an inventory of the existing box before it changes:

```bash
docker ps -a --format '{{.Label "com.docker.compose.project"}}\t{{.Names}}\t{{.Status}}'
docker volume ls
ls -la /var/lib/home-stack/state /var/lib/home-stack/config /var/lib/home-stack/media
```

Keep that output. It is what you compare against afterwards, and the volume
list is the part people forget.

## Renames that have already happened

Each of these carries a compatibility shim, so an older install upgrades by
pulling and deploying. The shims are meant to stay; none of them is a
deprecation with a deadline. Verify each one applied rather than assuming it:
the whole point is that a missed migration looks like a successful deploy.

| What changed | Old | New | How an old install survives |
|---|---|---|---|
| Service and directory | `home-web` | `home-core` | — |
| Config block | `services.home-web` | `services.home-core` | `SERVICE_RENAMES` in `deploy.py` moves the block on load and warns |
| Secrets | `HOMEWEB_SECRET_KEY`, `HOMEWEB_DEBUG_API_KEY`, `HOMEWEB_PROXY_TOKEN_*` | `HOMECORE_*` | `SECRET_ALIASES` reads the old name when the new one is absent; the new name wins if both exist |
| State directories | `{state}/home-web/*` | `{state}/home-core/*` | `renamed_from:` on the state entry moves it once |
| Compose project | the bare unit name (`local`, `proxy`, `web`) | `<service>-<unit>` | the deploy brings the legacy project down, then copies its named volumes across |

### The compose project rename is the dangerous one

Compose names a project after its directory, which here was the *unit* name. Two
services with a unit called `local` shared one project, and `up --remove-orphans`
deletes containers in the project that the current files do not define — so
deploying the portal destroyed the chat proxy and vice versa, both reporting
success.

Project names are `<service>-<unit>` now. The consequence to watch is that
**Compose prefixes named volumes with the project name**, so the rename would
otherwise hand each service a new empty volume and leave the real data on disk
under the old name. `migrate_compose_volumes()` copies them, restricted to the
volumes the unit's own compose files declare.

Four units hold named volumes:

| Unit | Volumes |
|---|---|
| `home-core/proxy` | `proxy-certs`, `acme-data`, `caddy-data`, `caddy-config` |
| `home-core/cloud` | `cloud-sessions` |
| `home-cameras/registry` | `camera_registry_data`, `camera_registry_logs` |
| `home-cameras/web` | `homecameras_logs` |

After deploying any of those, confirm the copy happened and the old volume is
still there as a fallback:

```bash
docker volume ls | grep -E 'camera_registry|proxy-certs'
docker run --rm -v home-cameras-registry_camera_registry_data:/d busybox ls /d
```

The migration copies rather than moves, and only into a volume that does not
exist yet, so it is safe to re-run and the original is left untouched. Delete
the old volumes by hand once you have confirmed the new ones, not before.

## Things that deliberately did not move

Do not "finish" these renames. Each is a stored format, and changing it orphans
data that is already written:

- **`homeweb:` chat ids.** A chat_id is `homeweb:<user>:<day>[:<conv>]`, written
  into cron jobs, nanobot session keys and the portal's history filenames. The
  modules that build and parse it (`nanobot/utils/homeweb_chat_id.py`,
  `nanobot/channels/homeweb_relay.py`) are named after the format, not the
  service.
- **`alfred/documents`** and the **`alfred-nanobot`** image. The assistant is
  renameable (`site.assistant_name`), and the rename is deliberately
  case-sensitive and word-bounded — `\bAlfred\b` — precisely so the lowercase
  path and image name are left alone.
- **Member ids.** Monotonic, never reused. Filling a freed slot hands a new
  person the departed member's derived task token, assistant state and backups.

## Order of operations

Dependencies are declared with `depends_on` in the manifest and resolved
topologically, so `./home-stack deploy` already orders correctly. When
migrating piecemeal, this order keeps each step verifiable:

1. `mqtt`, `ntfy` — no dependants' state, quick to confirm.
2. `admin` — never disabled, and the way back if config goes wrong.
3. `home-core` — the user store. Confirm the `renamed_from:` moves before
   anything tries to authenticate.
4. `nanobot`, `nanobot-house` — assistant state and memory.
5. `home-paperless` — documents and the Postgres behind them.
6. `faster-whisper`, then `home-voice` — voice depends on both whisper and the
   house assistant.
7. `home-cameras` — the named volumes above.
8. `local-proxy`, and `cloud-proxy` only if `cloud.vps.enabled`.

Deploy one service at a time and read the verify output. `--only <unit>` narrows
it further when a service has several units.

## Hardware assumptions worth checking first

Two services assume an NVIDIA GPU, and neither degrades — both fail in a way
that reads as something else:

- `services.faster-whisper.device` (`cuda`/`cpu`) and `.compute_type`
  (`float16`/`int8`). On CPU, pair with a smaller `.model`. Without this the
  container raises "CUDA driver version is insufficient" at import and
  restart-loops, which looks like a slow model load.
- `services.home-cameras.gpu`. The GPU reservation is an overlay
  (`docker-compose.gpu.yml`), applied only when this is on, because
  `driver: nvidia` is a hard requirement — `up` fails outright with "could not
  select device driver" on a machine without one.

The upgrade defaults for both are the *old* behaviour (GPU on), so an existing
install is unaffected; `config/home-stack.example.yml` ships them off, because
most machines have no GPU.

Also check disk before starting. The camera wall image is ~13 GB and the whisper
image ~5 GB, both CUDA-based, and they are pulled whether or not there is a GPU
to use them.

## Verifying the result

A green deploy is not the check. Ask each service the question a person would:

```bash
curl -s http://127.0.0.1:21002/healthz           # admin
curl -sk https://127.0.0.1:21001/ping            # home-core portal
curl -s http://127.0.0.1:21004/publico/login     # entry page (routing, not just Apache)
curl -s http://127.0.0.1:21021/api/ping          # camera registry
curl -s http://127.0.0.1:21020/api/ping          # camera wall
curl -s http://127.0.0.1:21011/health            # voice gateway
curl -s http://127.0.0.1:21399/health            # shared assistant
```

Then the things a status code cannot tell you:

- **The user store carried.** `{state}/home-core/users.json` is not `[]` unless
  the household genuinely has no accounts. This is the single most important
  file in the install.
- **Assistant memory carried.** `{state}/nanobot/user<N>/workspace/USER.md` is
  the consolidated one, not a freshly seeded template.
- **The voice round trip.** `services/home-voice/test/smoke.py` speaks through
  piper, feeds the audio back through whisper and asks the assistant. It reads
  the device registry itself, so it needs no arguments:
  `VOICE_CONFIG_DIR=... VOICE_GATEWAY_TOKEN=... python3 test/smoke.py`
- **The shared assistant holds no personal credentials.** Asserted by the
  `container_env_absent` check on `nanobot-house`, but worth confirming after a
  migration that moved env around.

## When something is wrong

The deployer is designed so that failure stops before the running service is
replaced: image tests (`test:`) run against the built image *before* it takes
over, and state directories are created before the compose run so a bind mount
is never what creates them.

If a verify fails after `containers up`, the new containers are already live.
`docker logs <container>` first — most of the failures found while writing this
were a check pointing at an endpoint the service does not serve, not a broken
service.

Recovery is `git checkout` the previous commit and re-deploy that service; state
is outside the deploy directory by construction, so rolling the code back does
not roll data back with it. That is also why the volume migration copies instead
of moving.

## Adding services as part of a migration

A household moving onto this package usually has something that is not one of
the ten — and those are exactly the services with its names, addresses and
credentials in them. They go in a plugin, outside this tree. See
`docs/plugins.md`.
