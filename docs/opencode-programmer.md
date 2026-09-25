# The Programmer runs on opencode

For whoever has to change, debug or undo this. It describes why the Programmer
profession does not work like the other five, what runs where, and which parts
are load-bearing. `CLAUDE.md` has the one-paragraph version and the rule about
the Go plan; this is the long one.

## Why it is a replacement and not a model

The obvious thing to want is opencode as *a model* behind the assistant that is
already there: leave Alfred alone, point the Programmer's model at opencode,
get the coding models. That is not expressible. `opencode serve` is an **agent**
server, not a gateway:

- There is no `/v1/chat/completions`. A turn is `POST /session`, then
  `POST /session/{id}/message` — or `prompt_async`, which returns 204 and
  streams.
- Its `tools` field is `{"bash": false, "read": false, …}` —
  `{"type":"object","additionalProperties":{"type":"boolean"}}` in its own
  OpenAPI. It switches *opencode's* tools on and off. It does not accept tool
  schemas.

nanobot's `LLMProvider.chat()` takes OpenAI tool schemas and must return
`tool_calls`. A provider wired to opencode could accept the call and could
return prose, and every tool the Programmer persona is built on — `read_file`,
`grep`, `edit_file`, `exec`, branch/commit/push, `document`, the share upload —
would silently do nothing. Silently is the operative word: nothing errors, the
assistant simply stops being able to act and starts describing what it would
have done.

So the space moves across whole: opencode's agent, opencode's tools, opencode's
models, with the household's own capabilities handed back to it over MCP.

Two things were measured before committing to that, against `opencode` 1.18.25:

| | |
|---|---|
| No agent named | ~2,150 input tokens of "You are OpenCode, You and the user share the same workspace…" on every turn |
| A custom agent in `.opencode/agent/<name>.md` | ~355 tokens — cwd, platform, date, a note about the skill tool |

The custom agent's prompt **replaces** the built-in one rather than stacking on
it, which is what makes `personas/programmer.md` usable as the whole prompt
instead of a second voice arguing with a coding agent's.

Streaming survives too: `GET /event` carries `message.part.delta` and ends the
turn on `session.idle`, so the space keeps the token-by-token rendering HomeCore
already does.

## What runs where

```
person → HomeCore, /chat/programmer
             │  one opencode session per chat_id, created with
             │  ?directory=<the broker's checkout for this member>
             ↓
        opencode serve      127.0.0.1:4096, on the host, a systemd *user* unit
             │  agent "alfred-programmer"
             │  native tools: read glob grep edit bash webfetch websearch
             ↓
        alfred-mcp          the household's own capabilities, over MCP
             ├─ the code broker   projects, checkout, branch, commit, push
             ├─ the file share    upload, and the download: link that goes in chat
             ├─ document          html/pdf handover
             └─ profession        delegating a visual piece to the Designer
```

### `opencode serve` is on the host, not in a container

Same relationship this stack has with Ollama: **pointed at, never deployed.**
Three reasons, in order of how much they cost to get wrong:

1. **The credential is opencode's.** `opencode auth login` writes
   `~/.local/share/opencode/auth.json`. This stack never reads it, never copies
   it into an image and never puts it in an archive. Containerising the server
   would mean mounting it.
2. **The checkouts are on the host.** The code broker clones into
   `{paths.state}/nanobot-code-workspace/<member>/<project>`, and the agent has
   to be able to read and write exactly that.
3. **A user unit sees one person's home.** Config, credential and checkouts are
   all in it. A system unit running as root would find none of them.

### The port is the security boundary

It binds `127.0.0.1` and that is not a tidy default — it is the whole
protection. **This is an agent with a shell.** Anything that can reach the port
can run `bash` as whoever started the unit, with no authentication in front of
it. opencode has an `OPENCODE_SERVER_PASSWORD`, which is HTTP basic auth over
plain HTTP; it is not a substitute for not listening. Containers that need it
reach it through `host.docker.internal`, which is this same loopback.

## The broker is not optional

`nanobot/code_broker/` exists because *"the broker cannot take the agent's word
for who is calling."* It owns which projects a person may see, where they are
checked out, the git credential, and the co-author line on a commit.

opencode has `bash`, and `bash` has `git`. If the agent commits and pushes
through its own shell, every one of those guarantees is gone and nothing fails
— the commit lands, with the wrong identity, in a repo the person may not have
been entitled to. **The tools map is the whole guard.** Git goes through the
broker's MCP tools; `bash` stays for running tests and reading state.

A future edit that widens the tools map without thinking about this hands the
agent the credential path the broker exists to own.

## Models, and the Go plan

`CLAUDE.md` says this stack must never point at OpenCode Go, and the reason is
specific: five containers on one key, running unattended around the clock,
roughly 1,800 turns a month with nobody at a keyboard. That is the shape a flat
plan flags, and the account can be blocked for it.

The Programmer is the exception, and it is narrow on purpose:

- **A person starts every turn.** The three sites that set `profile` are a
  person's own chat turn, a profession delegating inside that turn, and the
  machine-event path — and the machine-event path never names `programmer`. No
  cron, no heartbeat, no notification triage reaches it.
- **The official client makes the request.** Traffic goes out through
  `opencode serve` itself, which identifies itself, rather than a raw POST at
  the Go endpoint.

The exception cannot spread by somebody editing one line of `assistant.models`,
because opencode is not in that list at all — see "The model is a default, not a
restriction" below. Which model it answers with is `cloud.opencode.model`, and
the only thing that reads it is opencode.

The free Zen models (`nemotron-3-ultra-free`, `nemotron-3.5-lightning-free`,
`mimo-v2.5-free`, `ling-3.0-flash-fin-free`,
`muse-spark-1.2-contributor-free`) are selectable for this role and are **not**
the default. OpenCode's own documentation says collected data may be used to
improve the model, and what the Programmer reads is this house's code and
infrastructure. The default is a Go model for that reason, not for quality.

## Where the configuration lives

Not in `~/.config/opencode/opencode.json`. That file belongs to whoever uses
opencode at this keyboard — it is their editor, and a deploy that rewrites it
changes somebody's tools underneath them. The household's own configuration is
a separate directory the deployer writes and the unit points at:

One directory per member, because one server per member — the token in it opens
that person's bridge and nobody else's:

```
{paths.config}/opencode/<member>/opencode.json          the MCP block and its token
{paths.config}/opencode/<member>/agents/alfred-programmer.md   the persona
```

`OPENCODE_CONFIG` and `OPENCODE_CONFIG_DIR` point there, out of
`~/.config/home-stack/opencode-serve-<member>.env` — written by the *deployer*,
alongside that member's configuration, not by the installer. opencode loads the
global file *first* and this one over it, so the machine's own settings survive
and only the household's additions are ours.

**opencode reads all of this at startup and never again.** Measured: an agent
file added to a running server does not appear in `/agent`, and an edited
prompt still serves the old text. So the deploy restarts `opencode-serve` after
writing — without that, the change looks applied and is not.

The persona is a second file, not a rewrite of
`services/home-core/local/personas/programmer.md`. That one is still live: it
is what the nanobot Programmer answers with whenever opencode is unreachable or
has no agent, which is the fallback the readiness check above depends on. The
two describe the same profession in two different tool vocabularies, and they
will drift if nobody minds them.

## Any model the household already pays for

opencode discovers providers from its **environment**, so the deployer writes
the household's credentials to `~/.config/home-stack/opencode-providers.env`
(0600) and the unit reads it. No `provider` block is written and opencode's
config file carries no secrets.

Measured on this house: two credentials set (`TOGETHER_API_KEY`,
`OLLAMA_API_KEY`) took it from 90 models across two providers to **138 across
four** — Together AI and Ollama Cloud appearing on their own, on top of the
OpenCode Zen and Go models opencode is signed in to itself.

`OPENCODE_PROVIDER_KEYS` in `deploy/deploy.py` is a named list rather than a
sweep of the secrets file. "Anything ending in `_API_KEY`" would also hand the
Programmer the camera's, the document store's and the notification service's.
`OPENCODE_API_KEY` is deliberately not on it: opencode holds its own OpenCode
credential in `auth.json`, and this stack has never read that file.

An empty key is skipped rather than exported. An exported empty one makes
opencode offer a provider that answers 401 on the first turn, which reads as the
model being broken rather than as the credential being absent.

## The model is a default, not a restriction

`cloud.opencode.model` — `"<providerID>/<modelID>"` — is written into the
agent's own front matter, which is opencode's native way of pinning a model to
an agent. HomeCore sends `{agent, parts}` and nothing else. Nobody here calls a
provider: opencode owns the routing, the credential and the fallback.

It sets where the Programmer *starts*, not what it may use. Every provider above
is available to it, and whoever is working can switch models in opencode without
touching this stack's configuration.

It is deliberately **not** a role in `assistant.models`. That list is the roster
of models *nanobot* calls, and every prefix in it is one this deployer can build
a provider for. An `opencode:` entry there was a category error, and it showed —
it needed a `MODEL_PROVIDERS` prefix, an exception in `apply_model_choices` so
the profession's fallback would not be handed a provider nanobot cannot build,
a check refusing the prefix on every other role, a derived value, and an
environment variable through the manifest and the compose file. One setting in
the block that already configures opencode costs none of that.

**Empty is a choice, not a default.** With no model named, opencode picks its
own — on an account with OpenCode Zen that is `big-pickle`, which is *free*
(`in=0 out=0`) and therefore covered by the free tier's own note that collected
data may be used to improve the model. What the Programmer reads is this house's
code and infrastructure, so the shipped setting names a Go model instead. That
is a data-use decision, not a cost one.

The line is dropped rather than left empty when nothing is named: `model:` with
nothing after it is a parse error, and an agent that fails to parse is one
opencode does not list — which the readiness check reads as "not ready" and
quietly answers on the assistant instead.

## Operating it

```bash
./home-stack install --opencode          install the template unit and the health check
./home-stack deploy alfred-mcp           configure and start one server per member
systemctl --user status opencode-serve@user1
journalctl --user -u opencode-serve@user1 -f
curl -s 127.0.0.1:4096/global/health     {"healthy":true,"version":"..."}
```

The split is deliberate. The installer prepares the machine — the template
unit, the readiness script — and starts nothing: which members have a server,
which port each answers on and where its configuration lives are all things the
*deployer* writes, and an instance enabled before any of that exists would come
up on the template's default port with no household configuration. Two members
would race for one port and neither would have an agent.

`--opencode` is skipped rather than fatal when opencode is not installed or is
not signed in: it backs one profession, and a household that does not want it
should still be able to install everything else.

Two things that bite:

- **Lingering.** A user unit dies with the last login shell. A household hub is
  normally logged out, so without `sudo loginctl enable-linger <user>` the
  Programmer works over ssh and not from a phone. The installer says so when it
  is not set.
- **`HOME_STACK_NO_HOST_UNITS=1`** makes the installer leave systemd alone.
  `deploy/test_install.sh` sets it, because that suite runs the whole install
  flow and would otherwise enable a real service on whatever machine ran it.

## One server per person, and why it cannot be one

opencode holds **one** MCP block carrying **one** member's token. That is what
the bridge authenticates, and deliberately not something a tool call can choose
— see `alfred_mcp/identity.py`.

The profession spaces are open to everybody: nothing gates `/chat/programmer`
on being an admin. So a shared server would authenticate every member's
household tools as whoever it was configured for — `list_projects` answering
with somebody else's projects, `save_text` writing into somebody else's folder
on the share. Nothing would fail. It would simply be the wrong person's code.

So there is one of each per member who has it switched on:

| | |
|---|---|
| `members[].programmer` | the checkbox on that person's admin page |
| `alfred-mcp-<member>` | their bridge, rendered by `deploy/compose_alfred_mcp.py` |
| `opencode-serve@<member>` | their server, a systemd template instance |
| `{paths.config}/opencode/<member>/` | their agent and their MCP token |

Ports count up from `services.alfred-mcp.port_base` and
`cloud.opencode.port_base`, in the order those members appear — **position
among the members who have it on**, so switching it on for somebody does not
renumber another person's bridge and leave their opencode dialling a port that
moved.

HomeCore is told the whole map as `OPENCODE_SERVERS`
(`user1=http://127.0.0.1:4096,user2=http://127.0.0.1:4097`) and routes each
person to their own. A member without one keeps the nanobot Alfred, which
resolves identity per turn — and is never served by somebody else's server.

Everything about an instance derives from one member id: the container name,
the port, the three ids that name the person, and the two derived tokens. That
is what makes the class of bug in `deploy/compose_members.py`'s header
impossible here rather than merely absent — go and read those four, they are
all the same shape, a per-member value that was right for four members out of
five.

## The switch checks itself

The Programmer moves to opencode only when *all* of this is true: `cloud.opencode`
is on, and opencode is reachable, and it actually carries the
`alfred-programmer` agent. Otherwise the space stays on the nanobot Alfred, and
nothing about it looks different.

That last condition is not caution for its own sake. `prompt_async` accepts
`agent: "alfred-programmer"` with a **204 even when no such agent exists**, and
answers as opencode's own `build` agent instead — its coding persona, its
voice, its ~2,150-token prompt. Nothing errors. The Programmer just quietly
becomes somebody else.

It is also what lets the routing ship before the persona does: phase 3 was
deployed while `/agent` still listed only opencode's built-ins, and the space
carried on as it always had.

Which side answered is recorded on the turn (`turn['backend']`) rather than
re-decided when somebody presses Stop. The readiness check is cached for a
minute, so it can flip mid-turn — and a cancel that asked again could abort an
opencode session for a turn nanobot is running.

## Two surfaces, and why this one

`cloud.opencode.api` picks the surface HomeCore talks to. It is `v1`.

**Read the name carefully: `v2` here is the `/api/*` routes on the OpenCode
*1* binary, and it is not OpenCode 2.** The two are different programs.
OpenCode 1 is `opencode-ai` on npm — `latest` is 1.18.27, which is what
`~/.opencode/bin/opencode` and the systemd units run. OpenCode 2 is
`@opencode-ai/cli`, whose binary is literally named `opencode2`, which
installs alongside rather than over the top, and which is in beta and moved
from Bun to Node. It is **not installed here**, and nothing in this stack has
ever pointed at it.

That distinction is the whole context for what follows. The 1.18.27 binary
serves `/api/*` — it answers, which is exactly why the routes were taken at
face value — but on that binary they are a preview, and everything below is a
measurement of *that*, not of OpenCode 2. Whether OpenCode 2 has the same
shape, the same behaviour or the same bugs is simply unknown; no claim here
should be read as one about it.

With that said, the two surfaces on 1.18.27 behave differently in ways that
only show up against a running server.

| | v1 (`/…`) | the `v2` key (`/api/…`, still the 1.18.27 binary) |
|---|---|---|
| session | `POST /session` | `POST /api/session` (`agent` and `location` on the *session*) |
| prompt | `POST /session/:id/prompt_async` | `POST /api/session/:id/prompt` |
| events | `GET /event`, global | `GET /api/session/:id/event`, per session |
| answer | `message.part.delta`, token by token | `session.next.text.ended`, whole |
| stop | `/abort` | `/interrupt` |
| **MCP tools** | **mounted** | **absent** |

The last row is why. On 1.18.27 an `/api` session resolves opencode's own
thirteen tools and none of the MCP ones, while `GET /mcp` cheerfully reports
`{"alfred": {"status": "connected"}}` — the server *has* the connection, the
session just never gets the tools. The same agent, the same config file and the
same model on v1 calls `alfred_list_projects` on the first turn. So v2 costs
the Programmer the broker, the share and the delegation — and leaves `bash`,
which means git without the broker: precisely the bypass the whole design is
built to prevent.

Two things were learned getting there, both of which look like an outage rather
than a mistake:

- **v2's per-session stream sends nothing until its session has an event** —
  response headers included. Open it before posting the prompt, the way v1
  requires, and the request blocks on the very event it is waiting to cause.
  The turn dies at the read timeout, 310s later, against a server that is
  perfectly healthy. So on v2 the prompt goes first, and the stream is opened
  with `after=admittedSeq-1`, which replays it. `admittedSeq` comes back from
  the prompt, and every event carries a durable sequence.
- **v1's `/event` is global but not unscoped.** Without `?directory=`, it
  accepts the connection, holds it open and delivers nothing: 0 events against
  41 with. From the portal this is indistinguishable from a model thinking for
  five minutes.

### The published docs are behind the binary

`https://opencode.ai/docs/server/` documents `POST /session`,
`POST /session/:id/prompt_async` (`{messageID?, model?, agent?, noReply?,
system?, tools?, parts}`), `POST /session/:id/abort` and `GET /event`, and this
client matches all of it. What the page does **not** mention is the `directory`
and `workspace` query parameters — and 1.18.27's own OpenAPI (`GET /doc`)
carries them on `/session`, `/session/:id/prompt_async`, `/session/:id/abort`
and `/event` alike. Believe the running binary's spec over the page.

That difference is not academic for `/event`, which is why the scoping above is
there. It is **not** needed for `abort`: tested both ways against a live turn,
with and without `?directory=`, and each returns `true`, stops the events
immediately and takes the session to `session.idle`. So `_opencode_abort` sends
none, and that is a checked fact rather than an inference from the spec —
having the parameter listed says nothing about whether omitting it is silently
wrong, which is exactly the trap `/event` set.

One thing the page has that this stack does not use: `OPENCODE_SERVER_PASSWORD`
(with `OPENCODE_SERVER_USERNAME`, default `opencode`) puts HTTP auth in front
of the server. Today the only thing between a caller and an agent with a shell
is the loopback bind — see "The port is the security boundary" above. Worth
considering as a second lock; it would mean the secret joining the env file and
the header joining every call HomeCore makes.

There is a third reason, found while trying to settle what ends an `/api` turn:
**those prompts are sometimes admitted and never run.** `POST /api/session/:id/prompt`
answers 200 with an `admittedSeq`, and then nothing happens — no `loop`, no
`stream`, no event, no error, on a server whose v1 sessions answer in under two
seconds on the same agent and the same model. It is intermittent: the same call
worked repeatedly an hour earlier. Intermittent is worse than broken, because
the fallback never fires — readiness passes, the turn is routed to opencode,
and the person waits out the read timeout.

Which is why `session.next.step.ended` as the terminator is still unverified.
It looks per-*step* rather than per-turn, and a turn that called one of
opencode's native tools first would end the read after the tool step and return
an empty answer. The turn needed to prove it is exactly the turn that would not
run. Confirm it against a build where v2 turns start reliably before flipping
the key; guessing a terminator is how a turn ends early and looks like a model
with nothing to say.

The client for that surface is kept, tested and one config key away
(`cloud.opencode.api: v2`), because none of the above is wrong about the
routes themselves. What it is waiting on is no longer "1.18.27 mounting MCP
tools on `/api`" but **OpenCode 2**, where those routes are the real product
rather than a preview — an install (`@opencode-ai/cli`, beta), a unit pointed
at `opencode2`, and every measurement in this section taken again from
scratch. Its agent format, its config file and its MCP block may all differ;
upstream says v1 integrations may need rewriting. `test_opencode_programmer.py`
pins both surfaces, including the ordering, since a stream opened in the wrong
order is a deadlock rather than a failure.

Flipping the key starts every conversation a new session, because the stored
one records which surface made it. That is not tidiness: v2 carries the agent
on the *session* — its prompt body has no `agent` field — so a session made on
v1 has none, and answering out of it would be opencode's own `build` persona in
its own voice, reported nowhere. The stored working directory is checked the
same way and for the same kind of reason: v1's stream is scoped to it, so a
session living in a directory nobody is listening on is 310s of silence.

Agents are also listed under different names: v1's `/agent` returns a bare list
whose entries carry `name`, v2's `/api/agent` returns `{location, data}` whose
entries carry `id`. Readiness reads either, because a field name guessed wrong
reads exactly like the agent not being installed — which is what it did.

## What `document` does instead

The plan had `document` — Alfred's HTML/PDF handover — among the MCP tools. It
is not there, and the reason is worth writing down rather than rediscovering:
`create_doc.py` is **1,535 lines** and shells out to headless chromium (or
weasyprint) to render. Copying that into this image would mean a second copy of
a large file somebody else maintains, plus chromium in a container whose whole
argument is that it is small and re-implements nothing.

`save_text` and `share_project_file` cover the handover the persona actually
describes — "a log, a diff, a report, a `.csv`". For a piece that has to *look*
like something, `delegate_to_designer` already goes to the Alfred whose job that
is, and who has `document`.

## Status

| Phase | |
|---|---|
| 1 | the host unit, `cloud.opencode`, the installer step — **done** |
| 2 | `alfred-mcp` — **done** |
| 3 | HomeCore routes `/chat/programmer` to opencode — **done** |
| 4 | the persona becomes an opencode agent — **done** |
| 5 | model selection — **done**, and smaller than planned: it is `cloud.opencode.model`, not a role in `assistant.models` |
| 6 | one server and one bridge per member — **done** |
