# Plugins: bringing your own services

This package ships twenty services, twelve of them on a default install. A real household has more, and they are the
ones with its names, its addresses and its credentials in them.

Without somewhere to put those, there are two bad options: fork the package, or
push private services into the core and give up on it being redistributable.
A plugin is the third. It is a directory **outside this tree** holding a
`plugin.yml` whose `services:` block uses the manifest's own schema.

The core never imports, executes or knows about any particular plugin.
Discovery, merge, done.

## Starting one

```bash
./home-stack plugin
```

Asks what the service is — where it runs, which port, whether it keeps data,
needs a credential, wants a tile, should be reachable by the assistant — and
writes a plugin that deploys before you have written a line of it. The
generated service answers `/api/ping` and serves a placeholder page; replace
`server/app.py` and everything around it already works.

It refuses to put the directory inside this tree, for the reason below.

## Where a plugin lives

Outside the package, always. The deployer refuses to load one from inside the
tree, because in there it would be swept up by `deploy/sanitize.py`, shipped by
any clone of this repository, and destroyed by a checkout.

That boundary is the whole arrangement: the household's own services sit
somewhere `sanitize.py` never looks, so the core stays clean and stays
redistributable while a real house runs on it. It is a boundary rather than a
loophole — the same file that goes unnoticed in a plugin beside the tree
produces six hits the moment it is copied inside one.

```yaml
# config/home-stack.yml
paths:
  plugins: /var/lib/home-stack/plugins    # where relative names resolve

plugins:
  - home-switches                          # relative to paths.plugins
  - /opt/something/else                    # or absolute
```

Empty, or absent, changes nothing.

## A whole plugin

This one deploys. It is the reference the mechanism was built against.

```
/var/lib/home-stack/plugins/home-switches/
├── plugin.yml
├── i18n/                  # optional; see below
│   └── en.json
└── server/
    ├── Dockerfile
    ├── app.py
    └── docker-compose.yml
```

```yaml
# plugin.yml
contract: 1                 # required; see "Versioning"
name: home-switches

services:
  home-switches:            # must not collide with a core service
    description: Lights server and the physical wall buttons.
    role: hub               # hub | compute | storage, as in the manifest
    units:
      - name: server
        dir: server         # relative to THIS plugin, not to the package
        build: true
        compose: docker-compose.yml
        env:
          SWITCHES_PORT: "{services.home-switches.port}"
          MQTT_BROKER: "{hosts.hub.from_container}"   # see "The address to use"
        state:
          - { path: "{paths.state}/home-switches", mount: /data,
              env: SWITCHES_STATE_DIR }
        verify:
          - http: "http://127.0.0.1:{services.home-switches.port}/api/ping"
            expect_json: {status: healthy, service: home-switches}
            timeout: 60
    secrets:
      required: [SWITCHES_API_TOKEN]
      optional: [SWITCHES_MQTT_PASSWORD]

tiles:
  - name: Switches
    href: "http://{hosts.hub.address}:{services.home-switches.port}"
    # An emoji. The portal draws this as text, so `mdi-light-switch` would
    # appear on the wall as those words; anything entirely ASCII is treated as
    # an icon-set name and answered with a generic glyph instead.
    icon: 🔘
    description: The wall buttons.
    # The portal leaves house-only tiles out for a request that arrived from
    # outside, rather than drawing a link to a private address that cannot
    # open. Say so; the stack cannot know it about your href.
    lan_only: true

impact:
  services.home-switches: [home-switches]
```

The service also needs a block in `config/home-stack.yml` like any other, which
is what `{services.home-switches.port}` reads and what the enable switch on the
admin page writes:

```yaml
services:
  home-switches:
    enabled: true
    host: hub
    port: 8095
```

## What a plugin gets, and what it does not get away with

Everything downstream of `all_services()` treats a plugin service as a normal
one. That is deliberate, and it cuts both ways.

**It gets:** deployment in dependency order, the full `env:` contract,
`--check-contract`, `--dry-run`, state directories created before the compose
run, its own Compose project, a tile, admin impact, secrets on the admin page,
and `deploy.py list`.

**It does not get away with:** anything the core cannot. In particular —

- **State inside the deploy tree is refused.** `guard_state_paths()` hard-fails
  it. Four separate wipes taught that rule: a re-deploy replaces the pushed
  directory, so live state in there is destroyed and the deploy still reports
  success.
- **A verify check must be able to fail.** `expect_json`, `expect`,
  `expect_status` or an explicit `status_only:` saying why it cannot. A bare
  status code passes against a redirect to a login page and against a 404
  handler that returns 200 — both have happened here.
- **A `state:` variable no compose file reads is an error.** The deployer would
  otherwise create, guard and document one directory while the container writes
  to another.
- **A relative bind mount is an error**, for the same reason as the first point.
- **A name collision is an error, never an override.** Not with a core service,
  not with another plugin. A plugin cannot quietly replace `home-core`.
- **A loopback address in a bridge-networked container is an error.** It is the
  container itself, so the skill reports your service as down while your
  service is up. Use `{hosts.<role>.from_container}`, or declare the variable
  under `self_urls:` if it is the address your service publishes about itself.
  See "The address to use".

## Secrets

A service receives exactly the keys it declares, and the deploy asserts that
afterwards rather than assuming it. Declare them under `secrets:` and they
appear on the admin page's secrets screen, grouped under the plugin's name, so
somebody knows to fill them in. A key nothing lists is a key nobody fills in,
and it fails much later as a service that simply never starts.

Impact is derived from the declaration — changing `SWITCHES_API_TOKEN` offers to
redeploy `home-switches` without your writing it twice.

Ship a `.env.example` fragment beside `plugin.yml` for anything a person has to
paste in by hand.

## Translations

Ship `i18n/<locale>.json` in the plugin. They are **its own coverage set**:
`i18n/check.py` reports them separately, does not require them in this package's
`en.json`, and checks `--unused` against the plugin's own source.

Stage them into the build context with `assets:`, exactly as the core services
do. Do not fetch them from a shared origin — these are different containers,
potentially on different machines, and the camera wall must not need the hub to
be up in order to be legible.

## Pages

A plugin does not contribute a page to `home-core`. It serves its own page from
its own container and contributes a tile.

There is no shared stylesheet and there must not be one, for the reason above.
A plugin page carries its own copy of the style block.

The tile lands on the portal's dashboard under **Extensions**, marked as one.
That marking is the point of the group: when one of these breaks it is nobody's
job in this package to fix it, and the wall should say so before somebody files
it against the stack.

`href` is interpolated against the config, so `{hosts.hub.address}` and
`{services.<name>.port}` are the right way to write it — a literal address in a
plugin is a plugin that only works on the machine it was written on.

There is deliberately no status probe. Painting a live dot means the portal
dialling every tile on every page load, which is the background thread the old
dashboard was replaced to be rid of.

## A plugin, or just a link?

Two different questions, and the answer is usually the second one.

**A link** — `custom_services` in `config/home-stack.yml`, or the admin page's
Services screen — is right for anything you run *somewhere else* and only want
on the wall: a NAS, Jellyfin, Home Assistant on its own box, a printer page. It
is a name, a URL, a line of description and whether it is house-only. This
stack does not deploy it, does not verify it, and does not pretend to.

**A plugin** is right when you want this stack to *own* the service: build it,
ship it to a host, hold its credentials, bind-mount its state somewhere a
re-deploy cannot wipe, and refuse to call the deploy done until it answers.
That is the whole apparatus in this document, and it is worth it exactly when
you would otherwise be writing the compose file, the deploy script and the
health check yourself.

Moving from one to the other later costs nothing: delete the `custom_services`
entry, add the plugin, and the tile moves from one source to the other without
changing what a person sees.

## Versioning

`contract:` is required, and the deployer refuses a plugin declaring a version
newer than it understands rather than half-merging it. The failure this whole
deployer is written against is "green deploy, nothing happened"; a plugin the
deployer silently half-read is that failure with a new cause.

Current contract: **1**.

## Failing loudly

Every way of loading a plugin wrongly is an error at load time, not a skip:

| what | result |
|---|---|
| directory named in `plugins:` does not exist | error |
| no `plugin.yml` in it | error |
| `plugin.yml` is not valid YAML, or not a mapping | error |
| no `contract:` | error |
| `contract:` newer than this deployer | error |
| two plugins with the same `name:` | error |
| two plugins declaring the same service | error |
| a service name a core service already uses | error |
| the plugin directory is inside the package tree | error |

The admin page is the one exception: it degrades to showing no plugins and says
so in its log, because it is where you would go to switch the offending plugin
off. The deployer refuses to run at all.

## Checking your work

```bash
./home-stack list                    your service, marked as a plugin
./home-stack check    every ${VAR} supplied, no bad mounts
./deploy/deploy.py <name> --dry-run        the full plan, changing nothing
./deploy/sanitize.py --check               still clean on the core
./i18n/check.py                            your catalogue, as its own set
python3 deploy/test_plugins.py             the mechanism itself
```

`--check-contract` and `--dry-run` are expected to pass both with your plugin
present and with `plugins:` empty. The second is the regression test that the
core is unchanged for everyone who has no plugins.

## Assistant skills

Your service can own its own assistant instructions. It answers
`GET <api>/skill` with an envelope, and the agent keeps a copy on disk:

```json
{
  "name": "switches",
  "version": "1",
  "mode": "replace",
  "description": "Turn the house lights on and off.",
  "instructions": "# Switches\n\nPOST /api/switch with {\"id\": …}.\n"
}
```

`replace` means this *is* the skill. `append` means the floor copy stands and
this adds to it — the house's own room names, say. An unreachable service keeps
whatever was last fetched; only an explicit **404** removes the skill. The skill
is filed under the name the agent asked for, never the `name` the response
claims, so one service cannot rename itself into another's skill.

Telling the agent where to ask is the one place a plugin reaches into a service
it does not own, so it is narrow and named:

```yaml
contributes:
  nanobot:                       # and/or nanobot-house
    env:
      SWITCHES_API_URL: "http://{hosts.hub.from_container}:{services.home-switches.port}"
    allowed_env_keys: [SWITCHES_API_URL]
    skills:
      - name: switches
        env: SWITCHES_API_URL
        floor: floor/switches    # a directory holding SKILL.md
```

- **`env:`** reaches the container through a file the deployer writes into the
  build context and the compose file reads with an optional `env_file:`.
  Exporting a variable is not enough on its own — a name no compose service
  lists arrives nowhere.
- **`allowed_env_keys:`** extends `tools.exec.allowedEnvKeys`, which is what
  lets the skill's own code read that address. Additive only: that list is a
  security boundary. Without it the skill runs, finds nothing, and reports your
  service as down.
- **`floor:`** is the copy used when your service is unreachable. It is staged
  into the image's *builtin* skills, which the loader consults last — workspace
  beats remote beats builtin, so a floor copy in the workspace would
  permanently defeat the live one your service is serving.

A contribution cannot displace anything: two plugins contributing the same
variable is an error, a variable the service already sets is an error, and a
skill name this package ships is an error rather than a claim that loses at
run time. All 27 shipped names are held, not just the ones an environment
variable happens to point at -- `nanobot-house` deliberately leaves
`TASKS_API_URL` and `PAPERLESS_URL` unset, and a check that read "did the
shipped row resolve?" handed those names away exactly there.

**`name:` is a name, and the deploy enforces it.** It becomes a directory under
the agent's skill cache, so anything that is not a single path component is
refused: `../skills/file-share` would land in the workspace, which outranks
both the cache and the builtin copy, and what is written there is not inert --
the runner executes the `python` fence out of `SKILL_PYTHON.md` with the exec
tool's credentials.

### The address to use

`{hosts.<role>.from_container}` above rather than `{hosts.<role>.address}`,
and never a literal.

There are two right answers and the config carries both. `address` is what
something *on the host* uses — ssh, rsync, a verify `curl`, and any service on
host networking. `from_container` is what a bridge-networked container uses to
reach the same machine: on the default single-PC install every role is
`127.0.0.1`, which inside a container is the container itself, so it resolves
to `host.docker.internal` instead. Give a role a real address and the two are
the same string, which is why one spelling is correct on both install shapes
and a hardcoded `host.docker.internal` is correct on neither.

`--check-contract` enforces this, per compose service:

- a bridge-networked service handed a loopback address is an error, whether it
  is written as a URL or as a bare host (`MQTT_BROKER`, `FILE_SHARE_HOST`);
- so is one handed `host.docker.internal` without
  `extra_hosts: host.docker.internal:host-gateway` **on that service** — on a
  neighbour, or in a comment naming it, does not count;
- so is a *host-networked* service handed `host.docker.internal`, which
  resolves nowhere there;
- and the same applies to what a plugin `contributes:` to a core service, not
  only to its own units.

The one exemption is a variable that is the service's *own* address rather than
somewhere it dials — an ntfy base URL that ends up in a notification link, a
Paperless public URL. Declare it, with the reason, the way a verify check
declares `status_only:`:

```yaml
env:
  SWITCHES_PUBLIC_URL: "http://{hosts.hub.address}:{services.home-switches.port}"
self_urls:
  SWITCHES_PUBLIC_URL: >
    the address it publishes about itself, opened from a browser on the host --
    not somewhere this container dials.
```

A `self_urls:` entry naming a variable the unit does not set is an error, so a
rename cannot leave an exemption standing over nothing.
