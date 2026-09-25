# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A packageable self-hosted smart-home stack: twelve services on a default
install, an installer, a deployer, an admin page and seven locales. The
manifest declares twenty; eight (`alfred-mcp`, `audio-cpp`, `browser-use`,
`home-search`, `homeassistant`, `n8n`, `nodered`, `registry`) stay off until a
household asks for them, which is why the two numbers are both true and neither
is the whole answer. `brightdata` is a switch in the same list
without a container of its own: it gates an MCP server in the assistants'
config, and off means the entry is *removed* rather than left pointed at
nothing -- see `apply_service_wiring`.

`crawl4ai` is how a page gets *read* -- URL in, Markdown out, on the
household's own hardware and with no credential. It replaced a hosted
alternative (Firecrawl) that was briefly here and is now gone entirely: a
second crawler bought nothing except a metered key, a page-read that left the
network, and two tools in the assistant's list whose descriptions both said
they read a web page. A model picks between those by ordering, not by fitness.

crawl4ai's `CRAWL4AI_API_TOKEN` is not an ordinary credential. Its image reads
it to decide the **socket bind** -- empty, it serves the container's own
loopback and nothing can reach it, which looks exactly like the container being
down. So it is **derived**, not stored: `derive_crawl4ai_secret()` in
`deploy/deploy.py`, from `PROXY_SHARED_SECRET`, the same shape as the proxy,
code-broker and MCP-bridge tokens. There is nobody to obtain one from -- the
server is the household's own -- and nothing to fill in, so an existing
household upgrading into crawl4ai gets a working one on the next deploy without
being told to run anything. A value set by hand in the env file still wins.

It was extracted from a live household's multi-repo setup — the services are real, mature applications with
their own histories; the packaging layer around them (`home-stack`, `deploy/`,
`admin/`, `i18n/`, `config/`, `secrets/`) was built for this repository.

Everything runs on one PC by default. `OPENCODE_API_KEY` is the only
credential that leaves the network, and the optional VPS proxy is the only
thing that runs off that machine.

**Go belongs to the coding harness. Everything else uses Zen.** One account
opens both — `https://opencode.ai/zen/v1` is Zen, per token; `/zen/go/v1` is the
flat $10/month Go subscription — and which one a caller may reach depends on
what the caller *is*.

`opencode serve` may use Go, and does: it is OpenCode's own binary, driven by a
person sitting in the Programmer space, holding its own credential and
identifying itself as the client Go is sold for. That is the shape the flat plan
was built around, and the household pays for it.

Nothing else in this stack may. Go's traffic is "monitored for abusive traffic
that degrades the experience for other users" and expects a client that
"properly identifies itself"; what the rest of this stack sends is containers on
one key running unattended around the clock, roughly 2,800 turns a month with
nobody at a keyboard -- measured over three weeks in `docs/token-spend.md`,
which also says where those tokens go and why a compression layer would not
help here. That is the shape a flat plan flags and **the account can
be blocked for it**. So every caller written *here* — the assistant, the titler,
the two model probes, anything added later — points at `/zen/v1`, pays per
token, and carries the header below. `OPENCODE_GO_API_KEY` is still read as an
alias so an old env file keeps working; which endpoint a caller reaches is not
negotiable.

Which *roles* sit on Zen is the household's choice and not a rule: the admin
page moves them freely, and a role on Zen is fine as long as its caller uses Zen
correctly — which means the session header, every time.

**The one exception, decided by the household on 2026-09-23: Go models in the
pi harness.** With `assistant.harness.allow_go` on ("Allow OpenCode Go models
in pi", beside the sub-agent models on the Models page), the sub-agent and
powerful sub-agent roles may pick an `opencode-go/…` model, and pi — which runs
background tasks — calls Go with it, a fresh `x-opencode-session` per task.
It is off by default and the page says why: this is unattended traffic on the
flat plan, the thing the paragraph above says can get the account blocked. The
exception is that narrow: nanobot's own loop still never calls Go (a task that
cannot go to pi runs on the everyday model instead), and no other caller may.

**Every request to `opencode.ai` carries `x-opencode-session`.** OpenCode began
requiring it on 2026-09-06 -- without it "requests may error" -- and asks for one
stable id per conversation. Four callers in this stack reach that host and each
answers with the best id it actually has: the assistant
(`openai_compat_provider`) sends one per provider instance, the same value it
already sends as `x-session-affinity`; the chat titler sends the
`chat_titles.session_id` of the conversation it is naming, which is a real
per-conversation id; `deploy/models.py` and `admin/models.py` are probes with no
conversation at all and send one id per run. A fifth caller, `opencode serve`,
is OpenCode's own binary and sends its own.

Two rules when adding a caller: send the header only to `opencode.ai` -- a
session id means nothing to Together or Ollama and tagging them reads as a bug
later -- and never hard-code the id, or every household on earth becomes one
caller.

Both rules are why a routing proxy in front of these callers is not a free
swap. `docs/provider-routing.md` is the worked answer for one (9Router), and
the test it names -- does the session header survive the hop -- is the first
thing to run for any other.

## Commands

```bash
./home-stack                              guided menu; no arguments needed
./home-stack install --check              prerequisites only, changes nothing
./home-stack install                      full first-time setup
./home-stack install --generate-secrets   fill in generated keys, derive the rest
./home-stack install --create-user        admin password and the first login

./home-stack list                         services, roles, enabled state
./home-stack check                        assert every compose var is supplied
./home-stack models                       every gateway model and its live state
./home-stack models --roles               only the ones a role points at
./home-stack models --quiet               silent unless a role's model is broken
./home-stack ollama                       the GPUs, the Ollama servers, what an apply changes
./home-stack ollama --apply               make the servers match cloud.ollama.instances
./home-stack ollama --library             the model library: what is on disk, how each tested
./home-stack llamacpp                     which llama.cpp builds exist (vanilla, PrismML)
./home-stack llamacpp build [flavor]      compile one for these cards and this CPU
./home-stack plan                         full plan, changes nothing
./home-stack deploy                       deploy everything enabled
./home-stack deploy home-core             one service
./home-stack deploy --bg                  in the background; `logs` to follow
./home-stack gpu off                      run everything on the CPU

./deploy/deploy.py home-core --only proxy  one unit of one service (raw)

./deploy/import_profiles.py <dir>         read old USER.md profiles into members:
./deploy/import_profiles.py <dir> --apply write them

./i18n/check.py                           translation coverage against en.json
./i18n/check.py --unused                  keys nothing references
./deploy/sanitize.py --check              assert no household data is present
./deploy/sanitize.py --selftest           verify the sanitizer's own rules
./deploy/publish_check.py                 refuse to publish household data (see Publishing)

./home-stack backup                       archive state (recordings excluded)
./home-stack backup --export              refresh the mirror, write no archive
./home-stack backup --auto                mirror now, archive only when due
./home-stack backup --list                what exists, how big, whether it verified
./home-stack backup --verify <id>         restore into a scratch tree and check it
./home-stack backup --verify <id> --only S  check just those services
```

There is no repo-wide test runner. The packaging layer's suites are plain
scripts, take no arguments, and run from the root under the same interpreter
`home-stack` dispatches to — the system `python3` is missing `ruamel` and
`flask` and fails two of them for that reason alone:

```bash
./.venv/bin/python deploy/test_deploy.py    manifest, env contract, state guards
./.venv/bin/python deploy/test_backup.py    archive a tree, damage it, restore it
./.venv/bin/python deploy/test_plugins.py   plugins, and the exemptions they lack
./.venv/bin/python deploy/test_ollama.py    the Ollama servers list, its units, the old shape
./.venv/bin/python deploy/test_publish_check.py  what the publish gate treats as a secret
./deploy/test_install.sh                    the installer, on a throwaway tree
./.venv/bin/python admin/test_templates.py  one class per tag, no bare {{ }} in a script
./.venv/bin/python admin/test_ollama.py     "Runs on", the servers card, the VRAM estimate
```

Services carry their own suites and are run from inside the service directory
(`pytest -q` in `services/home-voice/test/`, `services/nanobot/tests/`,
`services/home-core/local/` -- where `test_templates_parse.py` renders every
page and asks node whether its inline JavaScript parses at all, because a page
whose script died still serves 200 and still looks like the page); the deployer runs the relevant ones against a
built image *before* it replaces a running container.

Three checks are **run by hand and by nothing else**, because each needs a
running container and reaches the internet -- which is the wrong thing to make
a deploy conditional on. Nothing catches a regression in what they assert, so
run them after moving a pin:

```bash
python3 deploy/units/crawl4ai/test/smoke.py --token "$(grep -m1 ^CRAWL4AI_API_TOKEN= \
    /var/lib/home-stack/config/smart-home-bot.env | cut -d= -f2-)"
                                          it still refuses to fetch the LAN
python3 services/audio-cpp/test/bench.py            the CPU voice bench
python3 services/audio-cpp/test/profile_tts.py      every TTS model it serves
python3 services/audio-cpp/test/quality_tts.py      and which one speaks Spanish best
```

The first is the one that matters most: crawl4ai's SSRF guard is upstream's,
so it can leave in an upgrade with every check in the manifest still passing.

## Architecture

### Two layers, and the boundary matters

`services/` is the payload — ten applications, each self-contained, each with
its own compose file, Dockerfile and docs. Everything else is packaging.

`./home-stack` is the only script a person runs. It dispatches to
`deploy/install.sh` and `deploy/deploy.py`, which still work directly; it adds
the contract check before every deploy, a log, and a menu when called with no
arguments. The installer prepares a machine, the deployer ships services onto
it, and they stay separate so either can be re-run without the other.

Packaging never edits a service to make it deployable. It supplies environment,
stages assets into a build context, and verifies the result. If a service needs
something to run, that goes in `deploy/manifest.yml`, not into the service.

### Configuration splits three ways

| File | Holds | Tracked |
|---|---|---|
| `config/home-stack.yml` | Where things run, what is on, who lives here, languages | No (`.example` is) |
| `secrets/smart-home-bot.env` | Credentials | No (`.example` is) |
| `deploy/manifest.yml` | How each service is built and how you know it worked | Yes |

The manifest is static; the config is site-specific. A service declares the
secret keys it needs and receives exactly those — that is what keeps
`nanobot-house` from holding per-member credentials even when they exist in the
env file, and the deploy asserts it afterwards rather than assuming it.

### One machine, by default

All three roles — `hub`, `compute`, `storage` — point at `127.0.0.1`, and
`Target.is_local` short-circuits ssh and rsync when a role resolves to this box.
Services deploy into `~/.local/share/home-stack`, owned by the deploying user,
so nothing in the normal path needs root. Give a role a real address and only
that role moves.

**The VPS is the one exception, and it is a proxy.** It runs no application code
and stores no household data: it terminates the public connection and forwards
everything down a reverse tunnel to the home machine, which serves the pages,
holds the user store and verifies passwords (`/api/auth/verify` in the portal,
`AUTH_MODE=upstream` in the proxy). Do not add a unit that puts data out there.

### Names come from `dns:`, and nothing invents one

The deployer's live exports still interpolate `{hosts.*}` -- that block is
deployment plumbing, and `Target.is_local` short-circuits ssh and rsync by
comparing an address, so it stays addresses.

Everything else names a host by reading `dns:`. That is the setting, it is what
the admin page writes, and it is the only place a name is allowed to come from.
A default, a fallback or a script that spells one itself is naming *some*
household's host, which is rarely the one running the code:

- The reserved suffix this stack used to spell was in 310 lines across 130
  files and resolved on
  no household at all -- not the one this was extracted from, not this one.
  `deploy/sanitize.py` was generating it: it rewrote `<alias>.home` to that
  suffix on every run, which is normalisation rather than redaction and bought
  no privacy. The rule is gone and `test_deploy.py` fails on the string
  anywhere in the tree.
- A default that names a host is a placeholder describing a value's shape, not
  a safety net. `./home-stack check` cannot see a missing export behind a
  `${VAR:-default}`, so `test_deploy.py` checks that separately: a default
  naming a host must be overridden by an export.
- Where `dns:` has no entry -- the file share, ntfy -- the default is **empty**.
  An empty value fails visibly; an invented name fails like a service being
  down, and a wrong one fails worse than either.

The trade this makes: internal traffic now depends on the household's resolver
answering. That is a real dependency and it is the reason this section used to
say the opposite. It is deliberate -- these names resolve here, measured from
inside a container, and an address in a shipped default is somebody else's
address.

### The env contract

`collect_env()` builds the complete environment a unit's compose file will
interpolate, from three sources: the secrets the service declares, the unit's
`env:` block (interpolated against config, with `$OTHER_KEY` aliasing a secret
under the name compose actually reads), and `state:` entries that name a
variable.

This is the part that was wrong for a while and is worth understanding before
editing: the deployer used to export only declared secrets plus five names, so
the manifest's `state:` and port declarations were decorative — compose fell
back to its own defaults, several of which were *relative* paths inside the
directory the deployer rsyncs with `--delete`.

`--check-contract` exists to keep that from returning. It compares exports
against every `${VAR}` in the compose files, refuses relative bind mounts,
catches external networks nothing creates, and accepts a key only when the
service declares it optional. Run it before touching the manifest and after.

The same bug has a second half. A compose file's `${VAR:-default}` is a
statement about the value's *shape*, written by whoever knows what reads it, so
an export shorter than that default replaces a correct fallback with a broken
one and nothing anywhere notices. `NANOBOT_URL` was the bare host where the
gateway's own default carried `/v1/chat/completions`: every voice turn POSTed
to `/`, got a 404, and told the family "I could not reach Alfred" while the
assistant answered the same question in two seconds. `test_deploy.py` now
compares each exported URL against its compose default and fails when the
export drops the path.

### Two invariants worth not breaking

**Live state never lives in the deploy directory.** `deploy.py`'s
`guard_state_paths()` hard-fails a state path that resolves inside the pushed
tree. This is not hypothetical: the camera registry, the button/light storage,
the dashboard's notification bridges and the portal's databases were each wiped
by a re-deploy in the stack this came from, and every one of those deploys went
green.

**Verification must be able to fail.** Checks assert payloads, not status codes.
`curl -sf` does not fail on a 3xx, and a check written the easy way once passed
against a different service's redirect on the same port while the real one
crash-looped. `|| echo` in a health check makes the check decorative.

A retry is inside that rule, not an exemption from it. `smoke.py --attempts`
re-rolls the voice round trip because Piper is a VITS model and samples — the
same sentence is a different waveform every time, and whisper mishears some of
them. It stays honest because every *deterministic* failure still fails all
five attempts: a gateway posting to the wrong URL, whisper down, an unreachable
assistant, a truncated clip. Retry noise you have measured; never a failure.

### i18n

One flat JSON catalogue per locale in `i18n/`, read by three bindings —
`i18n.py` (Flask), `i18n.php` (the entry page), `i18n.js` (browser dashboards).
`en.json` is the reference and the fallback; a missing key renders the English
string, never the key name.

Each app serves its own copy, staged into its build context by the deployer's
`assets:` directive. They are deliberately not fetched from a shared origin:
these are different containers on different machines, and a cross-origin fetch
would make the camera wall depend on the hub being up to be legible. The same
reasoning is why there is no shared stylesheet — see the "The House" block
comment in `admin/templates/base.html`.

The portal's own household pages read the same catalogue. The chores page and
the navigation chrome — the chat's apps menu and the module header row — render
every label through `t()`: `common.*` for the module names, `nav.*` for the
chat menu, `tasks.*` for the chores page. `en.json` is the source, `es.json`
is complete for these, and the other locales fall back to English by design —
`i18n/check.py` reporting them missing is the expected state, not a regression.

### No household data

`deploy/sanitize.py` is the record of every identifier that was replaced when
this package was extracted, and re-checks the tree on demand. Its rules are
anchored so they cannot match their own output; `--selftest` asserts that,
along with the strings that must survive untouched (`Path.home()`,
`user.home`, `{services.home-core.port}` — all of which an earlier, unanchored
version rewrote).

Files whose entire content was personal — household profiles, bank statements,
the user store, password hashes — were deleted and replaced with templates.
Do not try to reintroduce seed data for members: the family directory ships
empty on purpose, and `_FAMILY_SEED` in `services/home-core/local/app.py` is
`{}` with a comment explaining why.

**Examples, fixtures and bench cases use invented people and devices**, never
the household's: members Tomi, Mora, Juana, Pili, Nico and Paula, the login id
`999000111`, bulbs "Oficina Tomi" and "Luz Paula" with MACs from `A0B1…` and
`B0C1…`. The real ones are in live data only. A bench case that has to reach a
real device names the invented one, and the house maps it back in
`/shared-state/bench-house-names.json`, outside the repository. A real name,
address, phone number, coordinate or login id pasted into a test "just to
reproduce it" is how the last leak happened; add it to
`deploy/sanitize-rules.local.py` (gitignored) whenever one turns up, so
`--check` sees it next time. A clean `--check` on a machine whose local rules
are incomplete proves little.

## Working on the live machine

This repository is usually edited on the machine it deploys to, with the
household using it. These cost real outages and are not obvious from the code:

- **The live config is `/var/lib/home-stack/config/home-stack.yml`**, not
  `config/home-stack.yml` in the checkout, which is only a seed the deployer
  reads `paths.config` from. The admin page writes the live one.
- **A deploy ships the working tree**, uncommitted edits included -- another
  session's too. Look at `git status` before deploying.
- **Deploying an assistant kills the turns it is running.** Before
  `./home-stack deploy nanobot` or `nanobot-house`, check nothing is mid-turn
  (`docker logs --since 2m <container>`) and that no benchmark is running
  (`running` in `<paths.state>/admin/bench/job.json`; the benchmark runs
  inside the first member's assistant container).
- **One deploy at a time.** Two builds race on `alfred-nanobot:latest`, and a
  deploy started from the admin page ships *admin's* copy of the code (see
  `docs/admin.md`). Check for one with
  `ps -eo args | grep "[p]ython.*/app/admin/deploy/deploy.py"` -- a plain
  `pgrep -f` matches its own shell -- and deploy `admin` after shipping
  services, so the page's copy is current.
- **Confirm a deploy by what the container runs**: grep a string you just
  added inside it (`docker exec <c> grep -c '<new text>' /app/...`). "Deployed
  and answering" only says a container started.
- **Before deploying `admin`**, compare the config file's inode inside the
  container with the host's (`stat -c %i`); a single-file mount pins the inode,
  and a save can land in a deleted file.
- **Test suites default to live paths** (`/var/lib/home-stack`) unless a test
  sets its own; one has deleted real accounts. Point `HOME_STACK_*` at a
  scratch directory when a suite you write touches config or state, and render
  admin pages against a copy of the live state -- a scratch config hid a 500.

## Publishing

The repository is public, and the machine it deploys to is somebody's home.
Two clones of it, with different jobs:

| Clone | Job | Pushes |
|---|---|---|
| **working checkout** (e.g. `~/smart-home-bot`) | Where changes are made, tested and deployed from | **Never.** Its push URL is disabled and a `pre-push` hook refuses. |
| **publishing clone** (the same name + `-main`) | Where fixes become pull requests | Branches only, through the gate below |

**Household data never enters git.** The household's config, credentials,
members and models live in the live config directory; its own services and
custom skills are plugins in `paths.plugins`; per-member skills and memory are
state; bench device names map through `/shared-state/bench-house-names.json`;
the identifiers the sanitizer hunts for are in the gitignored
`deploy/sanitize-rules.local.py`. So a commit in the working checkout is
generic by construction. When a change only makes sense for this house, it
belongs in one of those places -- not in the tree. The rare exception that must
be in the tree is its own commit with a subject starting `house:`, and it is
never published.

**To publish a fix**, in the publishing clone:

```bash
git fetch --multiple origin work        # `work` is a remote pointing at the working checkout
git switch -c fix/<name> origin/main
git cherry-pick <commits>               # from work/main; never a `house:` commit
./deploy/publish_check.py               # the gate; the pre-push hook runs it too
git push -u origin fix/<name>           # then open the PR (gh pr create, or the link printed)
```

After the PR is merged, the working checkout catches up with
`git pull --rebase origin main`; the published commits drop out as already
applied, and `house:` commits stay on top.

**The gate** (`deploy/publish_check.py`) reads this machine's real values and
fails if any of them is in what would be pushed: every credential in the live
and seed env files, every login id, email and phone in the portal's user store,
the sanitizer's identifiers (with the local rules -- without them it warns that
it cannot see the household's names), key-shaped strings, and any commit whose
author or committer email is not `git config publish.email` (a GitHub no-reply
address; a personal one was once published this way). It prints the kind of
value and the file, never the value. The publishing clone needs the working
checkout's `.venv`, `config/home-stack.yml` seed and
`deploy/sanitize-rules.local.py` linked in, all gitignored.

Rules that follow: **a push goes after the check with `&&`, never `;`** -- a
chain that carried on after a failed commit once pushed the wrong one. **A
force-push to `main` is only ever to undo a leak**, and only after asking.

## Plans

A multi-step request is planned by one model and carried out step by step by
another. `docs/plans.md` is the reference; `services/nanobot/AGENTS.md` has the
rules for editing it (a step gets only the tools its text names, the shell is
hidden, read-only is the default and the runtime enforces it).

## Conventions

- **English is the source language**, everywhere: user-facing copy, the
  assistant's skills and personas, the prompts HomeCore sends it, code, comments
  and commit messages. New user-facing strings go through `i18n/`.
  Three kinds of Spanish are deliberate and must not be "translated away":
  the tables that match **what a person typed** (grocery keywords, the
  chore-emoji rules, day and meal words, name nicknames) carry both languages
  and gain English entries rather than losing Spanish ones; the alias maps that
  read **values stored before a rename** (`THEME_SHAPE_ALIASES`,
  `GROCERY_CAT_ALIASES`, `MENU_MEAL_ALIASES`, `CHAT_SPACE_ALIASES`,
  `KIND_ALIASES`); and test fixtures that exercise accents on purpose.
- **The household modules are panels, not links.** The chat apps menu opens
  Tareas, Archivos, Compras and Menú as embedded panels; `CHAT_APP_LINKS`
  lists only what leaves the chat (Cameras). The standalone pages are the
  wall's tiles and each other's neighbours on the module header. A module that
  shows up as both a panel and a menu link has two entries with different
  behaviour and different names — `test_house_only.py` pins both halves of the
  contract.
- **The module pages draw one header.** tasks, grocery and menu include
  `templates/_app_header.html` — the portal, Alfred and the four modules in
  one scrollable row, current page marked. files keeps its rail and carries
  the same two doors in its foot. One copy, so the navigation cannot drift
  between them; the same reason the config pages share `_config_base.html`.
- **Container clocks are UTC**; anything shown to a person converts using
  `site.timezone`. Don't normalize the two into one.
- **MQTT** is governed by `docs/mqtt-conventions.md`, which spans the whole
  stack. Topic grammar is `<prefix>/<domain>/<device-id>/<leaf>`; the broker
  runs `persistence false`, so treat `status` as the liveness signal rather
  than trusting a retained value.
- **Three ids name a person, and they are not interchangeable.** This is the
  most expensive confusion in this codebase's history — it has cost the
  household their themes, their notification rules, their persona modes, their
  assistant profiles and half the family directory, in one migration, without
  a single error being raised.

  | | example | what it is | used for |
  |---|---|---|---|
  | **member id** | `user1` | `members[].id` in the config. Monotonic, never reused. | state directories, assistant instances, ports, the suffix on per-member secrets |
  | **login id** | `999000111` | `users.json`'s `username`. What a person types to sign in, and what `session['user']` holds. | **everything reached from a request** |
  | **folder** | `tomi` | `share_folder(member)` — the slugified display name. | paths on the file share, and what the household calls somebody |

  The rule: **anything a request reaches keys on the login id**, because that
  is what the session carries. The folder is for paths on the share. The
  member id is for what the deployer builds. Convert once, at the edge —
  `portal_logins()` and `share_folder()` in the deployer, `FILES_FOLDERS` and
  `_member_of()` in the portal — and never in the middle.

  A table keyed on the wrong one does not fail. It answers *nothing*, which
  reads as "you have no rules" or "you have no theme", and looks exactly like
  a feature nobody switched on. `LOGIN_KEYED_TABLES` in
  `services/home-core/local/app.py` names every table that keys on a login and
  `migrate_person_keys()` moves any that drift; the family directory keys on
  the folder and `migrate_family_keys()` does the same for it. Both are
  idempotent and run at boot. If you add a table with a person in it, put it in
  one of those lists.

- **Household members are managed on the admin page and nowhere else.** Member
  ids are monotonic and never reused: filling a freed slot handed a new person
  the departed member's derived task token, assistant state and backups.
  Deactivating keeps the person and drops their assistant instance; removing is
  the destructive one and still never reissues the id.
- **Saving is not deploying.** The admin page records which services a change
  affects (`IMPACT` / `SECRET_IMPACT` in `admin/app.py`) and offers to deploy
  exactly those. A setting that never reaches a container is worse than one
  never changed.
- **State lives where `docs/backups.md` says it does.** That file is derived
  from the manifest's `state:` entries; if you add one, add it there too —
  `test_backup.py` checks the inventory both ways, so a declared path nobody
  documented and a documented path nobody declares are each a failing test
  instead of a hole found at restore time. What a run *cannot* reach goes in
  `backup.uncaptured()`, which warns while archiving, records the gap in the
  archive's meta and repeats it at `--verify`; a member whose assistant runs
  under `runtime: kubernetes` keeps workspace and memory in a PVC and is the
  only entry today. Naming a hole is not filling it, but an archive that
  quietly omits someone's assistant memory reads exactly like one that doesn't.
- **A backup has two shapes, and only one of them has history.**
  `backups.export.path` mirrors the same inventory to the same paths every run,
  so an external uploader ships only what changed — but a mirror has no past:
  delete something and the next refresh deletes it there too. The dated
  archives are the history, kept in three unioned tiers (`keep`,
  `keep_weekly`, `keep_monthly`) and written on their own clock
  (`full_every_days`, with `--auto`). The mirror is **plaintext and 0700** on
  purpose: per-file encryption is what keeps an upload incremental, and that
  belongs to whatever uploads it. `docs/uploader-contract.md` is that boundary;
  `export_root()` refuses any directory whose `rsync --delete` would reach a
  live path.
- **`homeweb:` is a stored format, not a name.** The portal is `home-core`, but
  a chat_id is still `homeweb:<user>:<day>[:<conv>]` and the modules that build
  and parse it (`nanobot/utils/homeweb_chat_id.py`,
  `nanobot/channels/homeweb_relay.py`) are named after the format, not the
  service. That string is written into cron jobs, nanobot session keys and the
  portal's history filenames; renaming it orphans every existing conversation
  and buys nothing, which is the same reason `alfred/documents` and
  `alfred-nanobot` kept their names when the assistant became renameable.
  A renamed *service* does move its state directory — the manifest's
  `renamed_from:` does that once, on the next deploy — and old names for
  secrets and config keys are aliased on read rather than required to change.
- **`gpu: off` does not make a service small.** `services.home-cameras.gpu`
  toggles the `docker-compose.gpu.yml` overlay and nothing else; the web
  server's Dockerfile is `FROM nvidia/cuda:...` either way, so the build pulls
  the CUDA runtime on a machine with no card. `docs/migration.md` puts that
  image at ~13 GB and whisper's at ~5 GB, and extracting needs the space again
  — a host that fits the other eleven services can still fail this one on `no
  space left on device`. `services/audio-cpp` is the second of these: its CPU
  image is 187 MB and the CUDA one built from the same Dockerfile is **2.89
  GB**, so "the same service with the card on" is a fifteen-fold difference in
  what has to be pulled. (`z-image` was the largest of all at 10.5 GB of image
  and 31 GB of weights; it was removed on 2026-09-08 and
  `docs/local-generation.md` keeps the measurements.) On a tight disk, deploy one service at a time:
  concurrent builds contend for it, and `nanobot-house` and the per-member
  units build the same `alfred-nanobot:latest` tag, which races.
- **The household's Ollama servers are a list: `cloud.ollama.instances`**
  (`deploy/ollama_instances.py`). Each has its own cards, port, window,
  parallel slots and models kept loaded -- those are server-wide in Ollama, so
  "vision with a small window beside notifications with a large one" is two
  servers. A role picks one by its model's prefix: `ollama:` is `main`,
  `ollama-vision:` is `vision`, `ollama-<id>:` any other; the Models page lists
  a local model once and shows "Runs on" beside it. A config from before the
  list (`cloud.ollama.local`/`.vision`/`.bench`) is read as the list it means.
  The servers are systemd units on the host, and the admin container cannot
  reach systemd: `./home-stack ollama` plans, `--apply` writes one drop-in per
  managed unit (`zz-home-stack.conf`, device list reset first) and restarts
  what changed, and `sudo ./home-stack ollama --install-trigger` once lets the
  page's Apply button do the same through a root `.path` unit that runs a
  root-owned copy of `ollama_host.py` against the *saved* list -- only
  `ollama*` units, every value checked by shape. The rule to keep still holds:
  a caller that sends its own `num_ctx` makes Ollama reload the model on every
  request, so a consumer's window comes from its instance's `context`
  (`ollama_context()`), never a constant. `docs/local-ollama.md` has the
  measurements.
  A setup (`cloud.ollama.setups`) may run on llama.cpp instead (`engine:
  llamacpp` or `prism`, PrismML's fork, the only runtime for Bonsai's ternary
  files): same `ollama-<id>:` address, but a `llamacpp-<id>` unit the host
  helper renders whole, running `llama-server` from a root-owned copy of a
  build `./home-stack llamacpp build` made. Builds are pinned in
  `deploy/llamacpp.py`, compiled in a CUDA container for these cards and this
  CPU, with NCCL off and CUDA's runtime shipped beside them -- the host has no
  libnccl and its own CUDA libraries are 12.0. Its `context` is per slot; the
  unit's `-c` is context x slots. A model is an Ollama name (its GGUF read
  from Ollama's store), `hf:owner/repo/file.gguf` (downloaded once into
  `/var/lib/home-stack/models`), or a .gguf there. The Ollama card's Preview
  runs the save's placement on the unsaved form and draws the cards.
- **`home-search` is off by default, and its config is seeded once.**
  `services.home-search.enabled` starts SearXNG and Vane *and* flips the
  assistants: `searxng` and `vane` come off `disabledSkills` and `web_search` is
  pointed at the house instance, through the manifest's `when_service:` block on
  both nanobot units. That indirection is not decoration — an env-unmet skill
  still ships its name and full description into every prompt, marked
  unavailable, and the compose files' `:-` defaults mean blanking a base URL
  does not empty it.
  `settings.yml` is copied from `settings.example.yml` on the first deploy and
  is state after that: editing the example changes nothing on a machine that
  already deployed, and rotating `SEARXNG_SECRET_KEY` does not rotate a running
  instance's. `validate-settings.py` runs as the `pre:` hook, before anything is
  replaced, because the stack this came from validated *after* installing and
  destroyed a working config proving it.
- **`services/nanobot/` contains upstream code and docs.** Its `README.md`,
  `docs/`, `SECURITY.md` and `CONTRIBUTING.md` describe stock nanobot, not this
  deployment, and carry the upstream maintainer's contact details. Read
  `services/nanobot/AGENTS.md` for what this stack actually does with it.
- **One assistant config, one entrypoint.** `services/nanobot/config/` holds the
  base — `config.json` plus the prompt files nanobot itself names (`SOUL.md`,
  `AGENTS.md`, `TOOLS.md`, `HEARTBEAT.md`, `MORNING.md`). `NANOBOT_INSTANCE`
  selects an instance, and `config/instances/<name>/` overrides only the files
  that instance actually differs in; everything else falls back to the base.
  The house instance's `config.json` is a *whole file* rather than an overlay
  because it holds fewer credentials than the base, and listing a provider is
  the same as demanding its credential. Per-member overlays stay overlays —
  they only ever add — and `null` in one deletes a key.
  `USER.md` is copied, never symlinked: memory consolidation writes to it and
  the mount is read-only.
