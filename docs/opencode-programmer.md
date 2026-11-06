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
can run `bash` as whoever started the unit. It does have a password since
2026-10-28 -- `OPENCODE_SERVER_PASSWORD`, derived by the deployer, because
OpenCode 2 refuses everything without one (see "Two opencodes") -- but that is
HTTP basic auth over plain HTTP, and it is not a substitute for not listening.
Containers that need it reach it through `host.docker.internal`, which is this
same loopback.

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

## When Go is out: a local model on the Studio's card

Go's allowance runs out -- the 5-hour window, the week, the month -- and when it
does Go does not fail a turn: opencode retries, and says why only as a
`session.status` of type `retry` carrying Go's own words ("weekly usage limit
reached. It will reset in 4 days 9 hours."). The portal reads that now.

With `cloud.opencode.fallback.model` set, the refused turn runs again on that
model, and every turn after it goes there until the reset Go named; the chat
says so each time ("OpenCode Go's limit is reached (back in 4 days 9 hours).
Continuing on this house's own model"). Without one, the turn ends at once with
when Go is back, instead of sitting "working" for days -- which is what fix
request #10 did on 2026-09-30.

The model runs on the host, on demand, on the Studio's card:

- opencode knows it as the `home` provider at `127.0.0.1:11460`
  (`build_opencode_config`), an address on this machine. The model asks for a
  key (`derive_programmer_local_key`, from `PROXY_SHARED_SECRET`); opencode's
  config names it as `{env:PROGRAMMER_LOCAL_API_KEY}` and the value sits in
  `opencode-providers.env` with the other credentials, so the config still
  holds none.
- `programmer-local.socket` holds that port. The first request starts
  `programmer-local.service`, which pauses the Studio (reason `programmer`, so
  its page says the card is lent, not that a parent stopped it), **waits for the
  running job to finish** -- a video shot can take ~25 minutes, and waiting is
  what the household chose over refusing -- then serves the model with the
  house's llama.cpp build, or with Strata (`engine: strata`, below), and waits
  until it answers.
- `programmer-local-proxy.service` forwards to it and exits after fifteen idle
  minutes; the model's unit is `StopWhenUnneeded`, so it stops too, and its
  stop resumes the Studio.
- `deploy/host/programmer-local.sh` does the pausing and serving from
  `~/.config/home-stack/programmer-local.env`, which the deployer writes
  (mode 600: it carries the Studio's derived secret). Removing the setting
  disables the socket on the next deploy.

**Give it the model's whole window when it fits.** A conversation that began
on Go carries Go's history: #10's opencode session was 90k-183k tokens when Go
ran out, and at a 64k window every request was refused ("exceeds the available
context size") -- the turn moved and then failed anyway. `ornith:9b` is 262k
natively, and its cache is small (mostly linear-attention layers): at 262144 it
takes 10.7 GB of the 12 GB card. Reading a 180k history the first time runs at
~1,600 tokens/s, a couple of minutes, and then it is cached.

**Each engine has its own session, and a handover.** A bigger window let
#10's history fit; it did not make it usable. Handed Kimi's ~180k tokens of tool
output, `ornith:9b` spent ten steps reading one file and committed nothing. So a
local turn works in an opencode session of its own (`local:<chat_id>` in the
session store), and whichever engine takes a turn is first told what the other
did since it last took part: the conversation as the person sees it, text only,
capped at ~24k characters (the opening request and as much of the end as
fits), and for a fix request the command that shows its worktree
(`_opencode_handover`). When Go is back it returns to its own session and is
handed what the local model did. One conversation, one worktree; only the
model's working memory is two. `seen` in the session store records when each
engine last took part, per conversation and not per day.

The model must fit the card whole. `ornith:9b` (Ollama library; a 9B trained
for agentic coding, 5.6 GB) was chosen on 2026-09-30 and measured on one 3060:
6.6 GB with a 64k window, 3.5 s to load, ~42 tokens/s, and 3 of 3 on a small
read-edit-test task through opencode-shaped tools with a 12k-token system
prompt, the prompt served from cache after the first step. Then, through opencode itself (`opencode run`, its own ~14k-token prompt and
tools), on a small package with a pricing bug and a missing `--json` flag, pass
= suite green and tests untouched, three runs each: `ornith:9b` 3/3 (48-89 s,
11-19 tool calls); `qwen2.5-coder:14b` 0/3 -- it wrote its tool calls as JSON in
the text instead of making them, so it never touched a file, and at 32k it
filled the card (11.75 GB). A code-completion leaderboard does not rank models
for this job; tool calls do. `ornith-1.5:9b` (2026-09-30, the same size and window, plus a
vision projector the fallback does not load) 3/3 on the same task (64-114 s,
11-12 tool calls). On the real fix request #10, 1.0 read the same files
repeatedly and committed nothing, so this house runs 1.5. It is far weaker
than Kimi K2.7 Code; the pipeline's gates -- tests, the benchmark, the leak
check, approvals -- are what stop a bad fix from shipping, whichever model
wrote it.

The model is picked on the admin page, in the Programmer card of **Models**
(a local model, its window, and whether it goes first), and saving marks
`alfred-mcp` and `home-core` for deploy. Candidates are compared on the
**coding benchmark** (`docs/coding-benchmark.md`), whose ★ on a llama.cpp run
sets the same thing.

### Two engines: llama.cpp and Strata

`cloud.opencode.fallback.engine` is `llamacpp` (the default) or `strata`.
Strata is a container (`STRATA_IMAGE` in `deploy.py`, pinned) with its own
llama.cpp, the only engine for Qwen3.8-Flash-Next's files. `model` then names
the first part of a split `.gguf` under the models directory; the folder
holding it is mounted read-only and Strata reads the model from the files.
Its engine, settings and speculative-decoding draft layer live in the
`programmer-local-strata` volume, so the **first start downloads ~5 GB and
prepares the model once** (several minutes); later starts load in ~80 s. The
script's readiness wait allows ten minutes for that.

Measured on one 3060 with 64 GB of RAM: Qwen3.8-Flash-Next-Coder IQ1_M (58 GB
of files) takes 11.3 GB of the card and ~26 GB of RAM at a 32k-128k window, at
24-32 tokens/s. It passed all five coding problems in pi where the best model that fits
the card whole passed three -- the only model here allowed to spill onto the
CPU, because it is the one that earned it.

### Reserving the card for coding, and the LAN

The coding benchmark page's Programmer card has **Reserve the card for coding**
(1-12 hours) and *Give the card back*. The page writes the time to
`<paths.state>/admin/programmer-local/hold.json`; the page cannot reach
systemd, so a user `.path` unit on the host watches that file
(`programmer-local-hold.path` -> `programmer-local.sh sync`) and starts or
stops `programmer-local-hold.service`. That unit *requires* the model, so it
loads it -- after the Studio's running job, as for a turn -- and keeps it up
until the time, whatever the traffic. A release writes `until: 0` rather than
deleting the file, so the `.path` sees it; the model then goes as soon as the
idle proxy is not holding it.

`programmer-local-lan.socket` serves the model on port 11462, and
`cloud.opencode.fallback.lan` says when (true and false, its first shape, read
as `reserved` and `off`):

- **`off`** -- loopback only, while reserved.
- **`reserved`** -- every address, while reserved: the socket and its proxy go
  with the hold, and the proxy has no idle exit.
- **`demand`** -- every address, always. The first request does what a
  Programmer turn does on 11460: reserves the Studio's card (after its running
  job), loads the model (~35-90 s for Strata) and holds the request until it
  answers; fifteen idle minutes give the card back.
  A client needs a request timeout longer than that first load. A reservation
  still pins the model loaded meanwhile.

`demand` is a choice the household makes on purpose: anything holding the key
can then take the Studio's card whenever it likes. Without the key nothing
happens: in `demand` the socket hands each connection to
`programmer-local-lan-gate.py`, which reads the request head and answers 401
before anything is started, so a port scan or a stray container connection
leaves the card alone. A request with the key starts
`programmer-local-lan-use.service`, which requires the model (the same wait as
a Programmer turn), and is then forwarded to it whole. Fifteen minutes with no
connection open end the gate, whose `ExecStopPost` stops that unit. The page
shows the key on request, along with the steps to point pi at it.

A deploy restarts the LAN socket (stopping its proxy first, which cuts a LAN
request in flight) only when it rewrote the socket's units; one that leaves
them as they were only starts or stops it. A setup the deploy cannot write
(llama.cpp not built, no `PROXY_SHARED_SECRET`) puts the LAN units back to
`off` rather than leaving the last ones written for a reservation to reopen. In `demand` the gate has no start limit, and
`programmer-local-lan-revive.timer` starts the socket again every five
minutes if it failed (a gate crashing at every connection trips the socket's
own trigger limit), so a failure lasts minutes, not until the next deploy. In
the reservation modes a deploy keeps the socket open only for a hold that is
running or has a *start* job queued; a stop job is a reservation ending.

### The other way round: the local model first

`cloud.opencode.fallback.first: true` (the portal's `OPENCODE_LOCAL_FIRST`)
runs every Programmer turn on the local model and hands it to Go when the local
one fails -- spending Go's allowance only where the 9B could not cope. Go still
takes a turn from the start when:

- **the Studio is rendering.** Lending the card means waiting for the running
  job, up to ~25 minutes; with Go there, that wait buys nothing.
- **the conversation is already on Go**: the local model failed in it once, or
  the person tapped *Retry on OpenCode Go*. It stays there, so an issue the 9B
  could not do is not retried on it every turn.

What counts as failing (`_LocalWatch`), because a 9B in trouble rarely errors --
on #10 it read the same files again and again and committed nothing:

- the same tool call with the same input three times, or one file read four
  times (reads are counted by path, since a long file is read in pieces);
- forty tool calls without an edit;
- tests run red at the end of a turn in which it changed files (red before any
  edit is a bug being reproduced, not a failure);
- an error opencode reports for the model -- its server did not start, the
  history did not fit.

Then the local session is aborted and the same message goes to Go, at once and
even after the local model said something: its partial answer stays, a rule
sets Go's apart, and a hint says why. Go is told the local model went first
and may have changed files. With Go out there is nowhere to hand it, so the
turn stops and says that instead of letting the model circle for half an hour.

A turn that simply finishes on the local model ends with a *Retry on OpenCode
Go* button -- an answer that is wrong without erring is the failure no watch
catches, and the person reading it is the one who can tell. Tapping it sends
that label as the message; HomeCore recognises it, pins the conversation to Go
and tells Go to redo the previous request.

This is still a person driving opencode in a conversation they are watching,
so Go is used as the flat plan intends (see CLAUDE.md): the hand-over happens
inside their turn, never from a timer.

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

## Two opencodes

`cloud.opencode.api` names which opencode is installed: `v1` is OpenCode 1
(1.18.x), `v2` is OpenCode 2 (2.x). They are two programs behind one binary
name and they share no routes.

**How that bit.** On 2026-10-22 OpenCode 2 was installed over
`~/.opencode/bin/opencode` (its installer replaces `opencode` and adds an
`opencode2` wrapper next to it). The running servers kept the old process until
the reboot on 10-24, then crash-looped -- `--log-level INFO` is a startup error
on 2, which wants `info` -- 2,650 times, and the Programmer answered as the
assistant for four days, because the portal's readiness check falls back
quietly. Two things now say so out loud: the deploy asks the binary the unit
actually starts (`_opencode_version_mismatch`, drop-ins included) and refuses to
restart a server whose version disagrees with this setting; and the portal logs
a 401, or v2's web page answering a v1 path, as what it is rather than as
"unreachable".

Until 2026-10-28 `v2` meant something else: the `/api/*` routes on the 1.18
binary, a preview that never mounted MCP tools and sometimes admitted prompts it
never ran. That client is gone; nothing here drives that surface any more.

| | v1 (OpenCode 1.18) | v2 (OpenCode 2.1.3) |
|---|---|---|
| auth | none required | **required**: basic auth, user `opencode`; given no password the server invents one per start |
| session | `POST /session?directory=` | `POST /api/session {agent, title, location: {directory}, model?}` |
| prompt | `POST /session/:id/prompt_async {agent, parts, model?}` | `POST /api/session/:id/prompt {text}` |
| events | `GET /event?directory=` | `GET /api/event`, global -- filtered by `data.sessionID` |
| answer | `message.part.delta` (type from `message.part.updated`) | `session.text.delta`; thinking is `session.reasoning.*` |
| turn over | `session.idle` | `session.execution.succeeded` / `.failed` / `.interrupted` |
| Go out | `session.status` of type `retry` | `session.retry.scheduled`, Go's words in `error.message` |
| stop | `/abort` | `/interrupt` |
| history | `/session/:id/message`, oldest first, `{info, parts}` | `/api/session/:id/message`, newest first, `{type, content}` |
| ready | `/global/health` | `/api/info`, which names its version |
| MCP tools | each its own tool | **one `execute` tool** running JavaScript: `return await tools.alfred.list_projects()` |

Measured against 2.1.3 on 2026-10-28, on a copy of opencode's data and a
spare port:

- **It reads the v1 config as it is.** `opencode.json`'s `permission` becomes
  `permissions` (`bash` rules become `shell` ones, order kept) and `provider`
  becomes `providers`; the agent file and its front-matter model load
  unchanged. The git refusals hold: a bare probe agent was refused
  `git commit` and `git -C` ("Permission denied: shell") and allowed
  `git branch --show-current` -- and `execute`'s JavaScript cannot reach the
  shell at all.
- **Household tools come only through `execute`.** Denying `execute` to an
  agent does not hand the tools over one by one; it removes them, and the model
  went hunting for "alfred" with the shell, grep, skills and finally a
  subagent. With it, gemma4:12b wrapped its JavaScript in JSON about twenty
  times, each spelled a little differently, before opencode injected a hint --
  which the same-call rule could not catch. Hence the agent prompt's
  "Calling your household tools" paragraph, and `_LocalWatch` counting tool
  errors in a row (`execute` reports failed JavaScript as a *success* with
  `metadata.error`). Kimi on Go has not been measured on it yet.
- **The stream goes first.** `/api/event` answers at once (`server.connected`),
  so it is opened before the prompt is posted -- the opposite of 1.18's
  per-session stream, which sent no headers until its session had an event.
- **The local engine works.** A session's model is set when it is made, and
  the local engine has its own session (`local:<chat_id>`), so it is made with
  `{providerID, id}` and Go's is left to the agent's front matter.
- **Rolling back is a binary swap.** The first v2 start migrates
  `~/.local/share/opencode/opencode.db`; 1.18.27 still opened a migrated copy
  (healthy, the agent listed, the MCP bridge connected, all sessions read).

The password is derived (`derive_opencode_password`, the crawl4ai shape): the
deployer writes it into each member's `opencode-serve-<member>.env`, the portal
gets it as `OPENCODE_PASSWORD`, and Caddy sends it on to opencode's own
interface on `dns.code` as `OPENCODE_BASIC_AUTH`, after the portal's ticket
check. It is basic auth over loopback HTTP: a second lock, not the boundary.

**Switching** is `cloud.opencode.api: v2`, then `./home-stack install --opencode`
once (the unit and the readiness script changed), then deploying `alfred-mcp`
(the env files, a restart) and `home-core` (the portal and Caddy). Every
Programmer conversation then starts a new opencode session -- a stored one
records which opencode made it -- and the handover text carries what was said.

v1's own two traps are still worth knowing:

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
and `/event` alike. Believe the running binary's spec over the page. (OpenCode
2's is `GET /openapi.json`, behind the password; its event types are not in it
at all, and were read off the binary.)

That difference is not academic for `/event`, which is why the scoping above is
there. It is **not** needed for `abort`: tested both ways against a live turn,
with and without `?directory=`, and each returns `true`, stops the events
immediately and takes the session to `session.idle`. So `_opencode_abort` sends
none, and that is a checked fact rather than an inference from the spec —
having the parameter listed says nothing about whether omitting it is silently
wrong, which is exactly the trap `/event` set.

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
