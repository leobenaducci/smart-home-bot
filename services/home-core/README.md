# home-core

The household portal — assistant chat, files, tasks, shopping, the weekly menu,
the family directory, geofences and themes — plus the PHP entry page and the
house-facing proxy. It also holds the user store and answers `/api/auth/verify`,
which is what both proxies check a password against.

```
local/    the Flask portal (HTTPS, the assistant chat, every app page)
cloud/    the PHP entry page
proxy/    the house-facing proxy and its certificate helpers
tools/    maintenance scripts
```

## Running it

You do not run this by hand. It is deployed by the stack's own deployer, which
supplies the environment, stages the shared `i18n/` catalogues into the build
context and verifies the result:

```bash
./home-stack deploy home-core          # all three units, in order
./home-stack deploy home-core --only local
./deploy/deploy.py home-core --dry-run # the plan, changing nothing
```

`deploy/manifest.yml` is where this service's build, environment, state paths
and health checks are declared — not this directory. If it needs something to
run, it goes there.

`local/docker-compose.yml` defaults its mounts to the **live** state tree
(`/var/lib/home-stack/state/home-core/...`, including `users.json`), so
`docker compose up` in `local/` is not a sandbox: it attaches to the
household's real data on host networking.

## Tests

```bash
cd local && pytest -q
```

## More

- [`AGENTS.md`](AGENTS.md) — how the portal is put together, and the reasoning
  behind the parts that look odd.
- [`../../docs/backups.md`](../../docs/backups.md) — the state this service
  owns and which of it cannot be regenerated.
- [`../../docs/admin.md`](../../docs/admin.md) — the page that edits its
  configuration and credentials.
