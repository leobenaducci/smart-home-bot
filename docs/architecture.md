# How this is put together

A map of the stack for somebody who has to change it. `CLAUDE.md` carries the
conventions and the rules that must not be broken; the rest of `docs/` goes
deep on one topic each. This is the thing neither of those is: what the pieces
are, how a request moves through them, and where the seams are.

Alfred gets the long section, because Alfred is the part with the most moving
pieces and the least obvious shape.

---

## 1. Two layers, and the boundary is the whole design

```
services/          the payload — real applications, each self-contained
├── home-core/         the household portal
├── nanobot/           the assistant engine (a fork; see its AGENTS.md)
├── home-cameras/      registry, wall, motion review
├── home-voice/        voice gateway and TTS
├── ...
│
deploy/            the packaging — installs, ships, verifies
admin/             a web page for the settings a person actually changes
config/  secrets/  what makes it this household's install
i18n/              one flat catalogue per locale
./home-stack       the only script a person runs
```

**Packaging never edits a service to make it deployable.** It supplies
environment, stages assets into a build context, and verifies the result. If a
service needs something in order to run, that goes in `deploy/manifest.yml` —
not into the service. This is what lets `services/` stay a directory of real
applications with their own histories rather than a directory of things that
only work here.

The boundary runs the other way too: a service does not read `home-stack.yml`.
It reads environment variables, and the deployer is what turns one into the
other.

## 2. One machine, three roles

Every role — `hub`, `compute`, `storage` — points at `127.0.0.1` on a default
install, and `Target.is_local` short-circuits ssh and rsync when a role resolves
to this box. Services deploy into `~/.local/share/home-stack`, owned by the
deploying user, so nothing on the normal path needs root.

Give a role a real address and only that role moves. The roles exist so that
"the cameras have the GPU" and "the documents have the disk" are expressible
without rewriting anything.

**The VPS is the one exception, and it is a proxy.** It runs no application code
and stores no household data: it terminates the public connection and forwards
everything down a reverse tunnel to the machine at home, which serves the pages,
holds the user store and verifies passwords. Do not add a unit that puts data
out there.

## 3. The services

Sixteen are declared; eleven are on for a default install. The five that stay
off (`home-search`, `homeassistant`, `n8n`, `nodered`, `registry`) wait until a
household asks for them.

| Service | Role | What it is |
|---|---|---|
| `home-core` | hub | The portal: chat, chores, groceries, menu, files, the entry page, and the house-facing proxy. Three units. |
| `nanobot` | hub | One assistant container **per member**, plus the code broker. |
| `nanobot-house` | hub | The shared room assistant — the one a voice panel talks to. |
| `home-cameras` | compute | Camera registry and the wall. Two units; the web server is CUDA-based either way. |
| `home-voice` | compute | Voice gateway and TTS for the panels. |
| `faster-whisper` | compute | Speech to text. |
| `home-paperless` | storage | Document archive, with Postgres beside it. |
| `mqtt` | hub | Broker and dashboard. `persistence false` — see `mqtt-conventions.md`. |
| `local-proxy` | hub | The LAN-facing proxy. Always runs. |
| `cloud-proxy` | vps | The public proxy. Off by default. |
| `ntfy` | hub | Notifications, local by default. |
| `admin` | hub | The settings page. |
| `crawl4ai` | compute | Reads a web page and hands back Markdown. On by default; the assistants reach it as an MCP server. |
| `audio-cpp` | compute | One runtime for speech to text and text to speech, CPU or CUDA. **Off by default**, and nothing points at it — `TTS_ENGINE` stays `piper`, which is baked into the voice gateway's own image and is also the fallback. |
| `alfred-mcp`, `audio-cpp`, `browser-use`, `home-search`, `homeassistant`, `n8n`, `nodered`, `registry` | — | Off until asked for. |

Published ports live in `21000–21499`, which is empty on a stock machine and
below the ephemeral range. The obvious numbers — 3000, 5000, 8000, 8080 — are
exactly the ones something else has already taken.

## 4. Configuration splits three ways

| File | Holds | Tracked |
|---|---|---|
| `config/home-stack.yml` | Where things run, what is on, who lives here, languages | No (`.example` is) |
| `secrets/smart-home-bot.env` | Credentials | No (`.example` is) |
| `deploy/manifest.yml` | How each service is built, and how you know it worked | Yes |

The manifest is static; the config is site-specific. A service declares the
secret keys it needs and receives exactly those — which is what keeps
`nanobot-house` from holding per-member credentials even when they exist in the
env file, and the deploy asserts it afterwards rather than assuming it.

**The deployed copy wins.** `/var/lib/home-stack/config/home-stack.yml` is the
config in use; the checkout's copy is the seed the deployer reads `paths.config`
out of. Editing anything else in the checkout's copy does nothing, and
`./home-stack` says so on every run.

### The env contract

`collect_env()` builds the complete environment a unit's compose file will
interpolate, from three sources: the secrets the service declares, the unit's
`env:` block (interpolated against config, with `$OTHER_KEY` aliasing a secret
under the name compose actually reads), and `state:` entries that name a
variable.

`--check-contract` exists because this was once wrong in a way nothing caught:
the deployer exported only declared secrets plus five names, so the manifest's
`state:` and port declarations were decorative and compose fell back to its own
defaults — several of them *relative* paths inside the directory the deployer
rsyncs with `--delete`. The check compares exports against every `${VAR}` in
the compose files, refuses relative bind mounts, catches external networks
nothing creates, and accepts a key only when the service declares it optional.

The same bug has a second half. A compose file's `${VAR:-default}` is a
statement about the value's *shape*, written by whoever knows what reads it —
so an export shorter than that default replaces a correct fallback with a broken
one and nothing notices. `test_deploy.py` compares each exported URL against its
compose default and fails when the export drops the path.

### Names come from `dns:`

The deployer's live exports interpolate `{hosts.*}` — that block is deployment
plumbing, and `Target.is_local` compares an address, so it stays addresses.
Everything else names a host by reading `dns:`. A default, a fallback or a
script that spells one itself is naming *some* household's host, rarely the one
running the code. Where `dns:` has no entry the default is **empty**, because an
empty value fails visibly and an invented name fails like a service being down.

## 5. Deploying

`./home-stack` is the only script a person runs. It dispatches to
`deploy/install.sh` and `deploy/deploy.py`, which still work directly; it adds
the contract check before every deploy, a log, and a menu when called with no
arguments. The installer prepares a machine, the deployer ships services onto
it, and they stay separate so either can be re-run without the other.

A deploy, per unit: interpolate the env, stage assets into the build context,
run any `pre:` hook, validate the compose file, build, bring the containers up,
then **verify**. Verification asserts payloads, not status codes — `curl -sf`
does not fail on a 3xx, and a check written the easy way once passed against a
different service's redirect on the same port while the real one crash-looped.
`|| echo` in a health check makes the check decorative.

The deployer also runs a service's own tests against the built image *before* it
replaces a running container, so a red run stops the house getting the change
rather than reporting on it afterwards.

### Two invariants worth not breaking

**Live state never lives in the deploy directory.** `guard_state_paths()`
hard-fails a state path that resolves inside the pushed tree. Not hypothetical:
the camera registry, the button/light storage, the dashboard's notification
bridges and the portal's databases were each wiped by a re-deploy in the stack
this came from, and every one of those deploys went green.

**Verification must be able to fail.** A retry is inside that rule, not an
exemption from it: `smoke.py --attempts` re-rolls the voice round trip because
Piper is a VITS model and samples, so the same sentence is a different waveform
every time. It stays honest because every *deterministic* failure still fails
all five attempts. Retry noise you have measured; never a failure.

## 6. State, and getting it back

State lives where `docs/backups.md` says it does — that file is derived from the
manifest's `state:` entries, and `test_backup.py` checks the inventory both
ways, so a declared path nobody documented and a documented path nobody declares
are each a failing test rather than a hole found at restore time.

Each `state:` entry carries a policy: `essential` (copied every run), `skip` (a
tool rebuilds it), `bulk` (large, static, yours, and something else carries it),
or `postgres` (dumped, because a file-level copy of a running cluster is torn
across relation files and generally refuses to start).

A backup has two shapes and only one has history. `backups.export.path` mirrors
the same inventory to the same paths every run so an external uploader ships
only what changed — but a mirror has no past. The dated archives are the
history, kept in three unioned tiers and written on their own clock. What a run
cannot reach goes in `not_archived` and `backup.uncaptured()`, which warn while
archiving and repeat at `--verify`: naming a hole is not filling it, but an
archive that quietly omits something reads exactly like one that doesn't.

---

# Alfred

Alfred is the household assistant. Everything below is how it is actually put
together in this stack — the composition, the wiring and the accounting. For
the engine's own internals, `services/nanobot/AGENTS.md` is the reference; that
directory is a fork of nanobot and has diverged.

The name is a setting: `site.assistant_name`, defaulting to Alfred.
`apply_assistant_name()` rewrites the word in prompt files at deploy time, the
manifest's `assistant_name_in` lists which files get that treatment, and
`CODE_COMMIT_NAME` carries the name into `git log` where prompt rewriting
cannot reach.

What does *not* rename are stored formats, branch prefixes and image tags —
`homeweb:<user>:<day>` chat ids, the `alfred/` branch prefix,
`alfred-nanobot:latest`, `alfred/documents`. Renaming those orphans every
existing conversation, or every remote's branch list, and buys nothing.

## A1. Six instances, two shapes

There is not one Alfred. There are five per-member instances and one house
instance, each its own container with its own state.

```
nanobot-user1 … nanobot-user5     one per member, private
nanobot-house                     shared, scoped to rooms
code-broker                       the coding harness, behind a token
whatsapp-bridge-user1             one member's WhatsApp link
```

**Per-member instances are rendered, never hand-written.** One member in the
config, one container out. `deploy/compose_members.py` generates the compose
from `members:`, and ports are derived rather than assigned:

```
websocket   21201 + index
api         21301 + index
gateway     21401 + index
```

The house instance sits at the top of each range — `21299`, `21399`, `21499` —
so it can never collide with a member however many are added.

**The two shapes differ in what they are allowed to know.** A per-member Alfred
holds that member's credentials and sees that member's documents, tasks and
memory. The house instance is what a voice panel in the kitchen talks to, so it
is scoped to rooms rather than to a person, and it deliberately holds *fewer*
credentials — its `config.json` is a whole file rather than an overlay for
exactly that reason, because listing a provider is the same as demanding its
credential. The deploy asserts this afterwards: `nanobot-house holds no
credential it must not have` is a real check, not a comment.

## A2. How a turn arrives

Alfred has no single front door. Five paths reach it, and which one a turn came
through decides both the session it runs in and how it is paid for.

| Path | Who starts it | Reaches Alfred via |
|---|---|---|
| **Portal chat** | a person typing | `home-core` → the member's websocket |
| **WhatsApp** | a person messaging | `whatsapp-bridge` → that member's instance |
| **Voice panel** | a person speaking | `faster-whisper` → `home-voice` gateway → `nanobot-house` |
| **Events** | the house itself | `home-core` calls `_alfred_notify()` |
| **Cron / heartbeat** | the assistant's own clock | inside the instance |

The event path is the volume one and the least visible. When a chore is due,
somebody's location changes, or a notification needs phrasing, `home-core`
does not template a string — it asks that member's Alfred to say it, and falls
back to plain ntfy only if the assistant is unreachable.

### The session key is the taxonomy

`_alfred_notify(username, prompt, scope=...)` decides **which nanobot session
the turn runs in**, and that is the difference between a conversation and a
firehose. Without a scope the turn joins whatever conversation the person is
currently in — right for a reply, wrong for a chore reminder that would
otherwise land in the middle of a discussion about something else.

The scopes are the vocabulary for "what kind of turn was that":

| Scope | What it is |
|---|---|
| `ev-notif` | Notification triage — the highest-volume, smallest turn |
| `ev-task` | Chores |
| `ev-geo` | Location |
| `ev-ask` | Household questions |
| `whatsapp` | Relayed messages |
| `dlg` | One profession delegating to another |
| `sub` | Sub-agents |
| *(empty)* | Ordinary Alfred |

This is worth understanding because it is already in the data. nanobot encodes
the scope in the session key, so the breakdown the household wants — what did
the assistant actually spend its month doing — needs naming rather than
instrumenting. `CHAT_SPACES` in `home-core/local/app.py` is where that naming
lives.

## A3. Which model answers

A role names a job, not a model. Twelve roles, each a claim about the *work*,
which changes far more slowly than any roster does:

| Role | The job | Ranked on |
|---|---|---|
| `everyday` | Ordinary chat. Somebody is waiting. | cached input |
| `powerful` | Work the assistant starts for itself | input |
| `programmer` | Code review and diagnosis, over whole files | input |
| `teacher` | Guides, tests, mark schemes — long output, exact format | output |
| `designer` | A whole HTML page in one answer | output |
| `notifications` | The highest-volume turn in the house | input |
| `doctor`, `legal` | Professions: read carefully, answer plainly | input |
| `subagent`, `subagent_powerful` | Background work, nobody waiting | input |
| `vision` | Camera frames and photos — local by default | input |
| `titles` | The portal's chat titler (home-core, not the assistant) — local | input |
| `documents` | Paperless's own AI, reading every scan — local, needs vision | input |
| `fallback` | Everyday Alfred, on a bad day | cached input |

A value names a model **and, by its prefix, where it runs**: a bare name is
OpenCode Zen, and `ollama:`, `ollama-cloud:`, `openrouter:`, `together:`,
`openai-compatible:` and `freetoken:` each select a provider. They can all be
live at once. Notification triage on the GPU in the cupboard, everyday chat on a
hosted model, one profession on whatever is genuinely best at it.

`assistant.models` is the one place a model changes; the deployer renders it
into each instance's config on every deploy.

Two things the ranking deliberately does not claim. It does not know which model
is *best* — there is no quality column in any catalogue, and 25 of 26 hosted
models report `reasoning: true`, which tells you nothing about which one writes
a correct code review. What it answers is "the cheapest model that can do this
job at all", by hard filters on context window, output ceiling and image input.
Everything else is what the household has *measured*, dated, kept beside the
role.

### The rescue ladder

`fallback` is the model every hosted role swings onto when the one it asked for
keeps erroring — a model that is *down*, not one that is busy. It must differ
from every role in use, because a fallback already in use is a second attempt at
what just failed, and the deploy refuses that.

Behind the configured fallback sits `OUTAGE_FALLBACKS`, three bare names
appended only when the fallback runs on the gateway that routes them. They are
picked for family diversity — one outage should not take two of them — and none
of them is a shipped role default, because a name a role already uses is
dropped from the chain and the ladder would silently shrink.

The failure this exists for is specific and has happened: one model returned a
hard 500 on every call while others answered on the same key, and because one
name is used by several roles at once, that single outage was every assistant in
the house at the same moment.

## A4. Prompts, professions and skills

**One config, one entrypoint.** `services/nanobot/config/` holds the base —
`config.json` plus the prompt files nanobot itself names: `SOUL.md`,
`AGENTS.md`, `TOOLS.md`, `HEARTBEAT.md`, `MORNING.md`. `NANOBOT_INSTANCE`
selects an instance and `config/instances/<name>/` overrides only the files that
instance actually differs in; everything else falls back to the base. Per-member
overlays only ever add, and `null` in one deletes a key.

`USER.md` is copied, never symlinked — memory consolidation writes to it and the
mount is read-only.

**Professions** are personas with their own model and their own prompt, living
in `home-core/local/personas/`: programmer, teacher, designer, doctor, legal.
`home-core` sends the *role name*, never a model, so changing what a profession
runs on is one config line and a redeploy rather than a client change. A turn
inside a profession does not fall through to `powerful` — each profession names
one model and uses it.

**Skills** are the assistant's verbs — around thirty of them, from `tasks`,
`grocery`, `menu` and `family` through `camera-feed`, `geo` and `notifications`
to `github`, `n8n` and `skill-creator`. A skill whose environment is unmet still
ships its name and full description into every prompt marked unavailable, which
is why switching a feature off means switching it off in the manifest's
`when_service:` block rather than blanking a URL.

A plugin can serve its own skill: the backups plugin contributes both an API URL
and a `backups` skill to the assistants, with a floor copy staged at deploy time
so the skill exists even when its service does not answer.

## A5. Sub-agents, memory and the broker

**Sub-agents** are what Alfred hands work to rather than doing in the turn: a
background task, a memory consolidation, the morning summary. They get the
*stronger* model, not the faster one — the opposite of the main path, and
deliberately. A background task is the long, hard end of what gets asked, and
nobody is waiting on a keystroke, so latency is worth nothing and being right is
worth everything.

**Memory** consolidates into `USER.md` per member. This is the state that cannot
be re-derived and the reason `{state}/nanobot` is in every archive.

**The code broker** is a separate container behind its own token
(`CODE_BROKER_SECRET`). It exists so the assistant can be handed a coding task
without every per-member instance carrying the capability.

## A6. What it costs, and how that is known

Every completed run reports one usage record — model, prompt tokens, cached
tokens, completion tokens, tools, and the session key that says what kind of
turn it was. `home-core` stores them in `usage.db` and is the only place they
are interpreted.

This exists because choosing a model was guesswork dressed as measurement.
Picking a replacement meant sending synthetic prompts to seven candidates and
reasoning about the result — a proxy for the house's traffic, not the traffic.
The one number that decided it came from a single log line somebody happened to
paste into a config comment. That should be a page, not an anecdote.

The page is `/stats`: cost by scope, by model, by person, by day, and by tool,
against `assistant.spend_budget` where the household has set one. Rates are a
dated snapshot rather than a live fetch, because the page must not depend on
reaching the internet and a cost that silently re-values last month's history is
worse than one that is openly a bit stale.

Two lessons from the data are worth carrying: an unpriced model shows as
unpriced rather than as zero, and the cached-input rate is not a footnote — a
model with a cheaper headline input price can cost half again as much per turn
because it publishes no cache discount, which is exactly why notification triage
moved off one.

## A7. What Alfred is not allowed to do

- **It holds no more credentials than its job needs.** The house instance is the
  sharp case; the deploy asserts it rather than trusting the config.
- **It does not invent a person.** Three ids name a person — member id, login id
  and folder — and they are not interchangeable. Anything a request reaches keys
  on the **login id**, because that is what the session carries. A table keyed on
  the wrong one does not fail; it answers *nothing*, which reads as "you have no
  rules" and looks exactly like a feature nobody switched on.
- **It does not get seeded with household data.** The family directory ships
  empty on purpose and `_FAMILY_SEED` is `{}` with a comment explaining why.
- **It is not the only thing that can answer.** Every event path falls back to
  something plainer — plain ntfy for a notification, a fixed sentence for a
  voice panel — because an assistant that is down should degrade rather than go
  silent.

---

## Where to go next

| For | Read |
|---|---|
| The rules that must not be broken | `CLAUDE.md` |
| nanobot's own internals, and how this fork diverged | `services/nanobot/AGENTS.md` |
| What is backed up, and what cannot be | `docs/backups.md` |
| Adding your own service | `docs/plugins.md` |
| Anything that leaves the network | `docs/optional-cloud.md` |
| Topic grammar and retention | `docs/mqtt-conventions.md` |
| The public proxy | `docs/proxy-home-setup.md` and its siblings |
