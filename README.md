# home-stack

A self-hosted smart-home stack for one household: a family portal, a personal AI
assistant for each person, a shared assistant for the rooms, voice panels,
cameras, a document archive, notifications and an MQTT broker, plus the
installer, deployer and admin page that run them on your own machine.

It started as a personal project to make one family's everyday life simpler:
a shared grocery list everyone actually keeps, chores that get done (with
points the kids track), a weekly menu, and an assistant that can do the small
things for each of us. It grew from there, one real need at a time, and is
published in case it is useful to another household.

**Everything runs on one PC by default.** The only things that leave your
network are the assistant's model API key (one hosted provider), and an optional
reverse proxy on a VPS you own, which forwards traffic home and stores nothing.

> [!IMPORTANT]
> **This project is entirely vibe coded.** Every line of it — the services, the
> packaging, the deployer, the admin page, the tests and these docs — was
> written by AI coding agents (mostly [Claude Code](https://claude.com/claude-code)),
> directed and tested by one person running it for their own family. It was
> extracted from that live household and has been used daily, but no human has
> reviewed all of it line by line. Read it before you trust it with your home,
> your cameras or your credentials, and expect rough edges.

## What it does

- **A family portal** — chat with your assistant, shared files, chores with
  points, the shopping list, the weekly menu, notification rules, themes.
- **An assistant per person** (a fork of [nanobot](https://github.com/HKUDS/nanobot)):
  its own memory, skills for the house (lights, chores, groceries, weather,
  cameras, documents, Home Assistant), scheduled reminders, geofenced
  reminders, documents it writes for you (PDF, spreadsheets), and long tasks
  run in the background.
- **Plans for multi-step requests** — a strong hosted model writes the steps,
  a small local model carries them out, and the runtime (not the model)
  decides which steps may change anything. See [docs/plans.md](docs/plans.md).
- **Voice panels** in the rooms, talking to a shared room-facing assistant
  through local speech-to-text and text-to-speech.
- **Cameras** — a registry, a camera wall, motion recording and on-demand
  scene descriptions from a local vision model.
- **Local models on your own GPUs** — Ollama or llama.cpp (including PrismML's
  build for ternary models), several servers pinned to cards, a model library
  that tests each model before you can pick it, a VRAM preview, and a
  benchmark with a per-role Test button. On a 12 GB card, **gemma4:e4b** is
  the tested pick for the local roles; see the recommended models in
  [docs/local-ollama.md](docs/local-ollama.md).
- **A document archive** (Paperless-ngx), **your own notifications** (ntfy),
  **your own web search** (SearXNG) and page reader (crawl4ai).
- **Seven languages**: English, Spanish, French, Italian, German, Chinese and
  Japanese.
- **Backups** with a verified restore, and a plaintext mirror for whatever
  uploader you already use.

## Services

This ships twenty services, twelve of them on by default; the rest wait until
you ask for them.

| Service | What it does | Default |
|---|---|---|
| `home-core` | The portal: chat, files, tasks, shopping, menu, the entry page, the house-facing proxy | on |
| `admin` | Settings, members, credentials, models, deploys | always |
| `nanobot` | The assistant, one instance per household member | on |
| `nanobot-house` | The shared room-facing assistant the voice panels talk to | on |
| `home-cameras` | Camera registry and the camera wall | on |
| `faster-whisper` | Speech to text | on |
| `home-voice` | Voice gateway: panel → speech → assistant → speech | on |
| `mqtt` | Broker and message dashboard | on |
| `ntfy` | Notifications on your own network | on |
| `home-paperless` | Document archive | on |
| `crawl4ai` | Reads a web page into clean Markdown, locally | on |
| `local-proxy` | The public name, answered from inside the house | on |
| `audio-cpp` | One C++ runtime for speech in and out, CPU or GPU | off |
| `home-search` | SearXNG metasearch for the assistants | off |
| `homeassistant`, `nodered`, `n8n` | Devices, flows, workflow automation | off |
| `browser-use` | An agent that drives a real browser | off |
| `alfred-mcp` | The assistant's capabilities offered to a coding agent | off |
| `registry` | An image registry, only for Kubernetes on several machines | off |

Outside that list, an optional **cloud proxy** runs on a VPS or VM you own: a
pure reverse proxy that forwards every request down a tunnel to the machine at
home and stores no household data. See
[docs/optional-cloud.md](docs/optional-cloud.md).

Services are placed by **role** (`hub`, `compute`, `storage`), and all three
point at `127.0.0.1` out of the box. Give a role a real address and only that
role moves.

## Requirements

- Linux with Docker and the compose plugin, `rsync`, `python3` ≥ 3.9,
  `openssl` and `curl`. `ssh` only if a role moves to another machine.
- Disk: tens of gigabytes. The camera and speech images are large (the CUDA
  camera image is ~13 GB even with the GPU off).
- An NVIDIA GPU is optional. Without one, speech runs on the CPU and the
  assistants use a hosted model. With one or two 12 GB cards you can run the
  small local models (vision, notifications, plan steps) on your own hardware.
- One API key for the hosted model. The stack is written for
  [OpenCode Zen](https://opencode.ai) (pay per token); read the note on Zen vs
  the Go plan in [docs/optional-cloud.md](docs/optional-cloud.md) before you
  point anything at the flat plan.

`./home-stack install --check` verifies the prerequisites and changes nothing.

## Deploying it: use Claude Code

**The recommended way to install, deploy and run this stack is with
[Claude Code](https://claude.com/claude-code) working in the checkout.**

The stack was built that way, and the knowledge needed to operate it is written
down for an agent to read: [`CLAUDE.md`](CLAUDE.md) at the root, and an
`AGENTS.md` in the services that need one. They cover what each setting really
does, which mistakes have already caused outages and how to avoid them (deploy
one service at a time, never deploy an assistant mid-conversation, the live
config is not the repository copy, confirm a deploy by what the container
runs), and how to check that a change worked instead of assuming it.

```bash
git clone <this repo> smart-home-bot && cd smart-home-bot
claude
```

Then ask for what you want, in plain words:

- *"Check the prerequisites and install the stack on this machine."*
- *"Add my partner and our two kids as household members."*
- *"Turn on home-search and deploy it."*
- *"My second GPU is free. Set up a local model for plan steps and benchmark it."*
- *"The voice panel in the kitchen stopped answering. Find out why."*

Keep an eye on it the way you would on a new admin: read what it proposes
before approving commands that deploy, delete or touch secrets, and use a
permission mode you are comfortable with. It works on your real house.

## Installing by hand

```bash
git clone <this repo> smart-home-bot && cd smart-home-bot
./home-stack install
```

`home-stack` is a script at the root of the checkout and the only one you run;
it dispatches to `deploy/install.sh` and `deploy/deploy.py`. Called with no
arguments, it reads the config and offers whatever is next.

The installer checks prerequisites, asks which services you want, generates
every credential it can, prepares the state directories and asks for the two
passwords it deliberately does not generate. Then:

1. **Set the one credential it cannot generate** in
   `secrets/smart-home-bot.env`: `OPENCODE_API_KEY=...`
2. **Set the timezone, who lives here, and the names you will type**, on the
   admin page or in `config/home-stack.yml` (comments survive a save).
3. **See the plan, then deploy:**

   ```bash
   ./home-stack plan          # the whole plan, changing nothing
   ./home-stack deploy        # everything enabled, in dependency order
   ./home-stack deploy admin  # or one service
   ./home-stack deploy --bg   # in the background; `./home-stack logs` to watch
   ```

4. **Manage it from the admin page** at `http://<hub>:21002/`, from your own
   network. Neither proxy routes to it. On a machine with a public address,
   bind it to `127.0.0.1` and use `ssh -L 21002:127.0.0.1:21002 <hub>`.

Everything is deployed into `~/.local/share/home-stack`, owned by you, so
nothing in the normal path needs root. The live config and secrets live under
`/var/lib/home-stack/config` once installed; the copies in the checkout are
seeds.

**A push is never a deploy.** Nothing deploys on a commit, and saving on the
admin page records which services a change affects and offers to deploy exactly
those. Every service is verified after it starts, with checks that assert a
payload rather than a status code, so they can actually fail.

## Configuration

| File | Holds | Tracked |
|---|---|---|
| `config/home-stack.yml` | Where things run, what is on, who lives here, languages, models | No (`.example` is) |
| `secrets/smart-home-bot.env` | Credentials | No (`.example` is) |
| `deploy/manifest.yml` | How each service is built and how you know it worked | Yes |

A service receives exactly the secrets it declares in the manifest, and the
deploy asserts it. `./home-stack check` compares what the deployer exports
against every `${VAR}` the compose files read and refuses relative bind mounts
— the failure it exists to prevent is state living inside a directory a
re-deploy replaces.

**Names come from `dns:`.** Services reach each other by the names configured
there, and nothing in the code invents a hostname; where `dns:` has no entry
the value is empty and fails visibly. Point the names at the hub in your
router or Pi-hole.

## Your own services

Anything your household runs beyond this — with its names, addresses and
credentials — goes in a **plugin**: a directory outside this tree with a
`plugin.yml` in the manifest's schema. Plugins deploy, contract-check and
state-guard exactly like the services here, and keep this repository free of
your data. See [docs/plugins.md](docs/plugins.md).

## Layout

```
home-stack  the one script you run; dispatches to deploy/
config/     site configuration (example committed, live copy gitignored)
secrets/    credentials (example committed, live copy gitignored)
deploy/     installer, manifest, deployer, backups, sanitizer, the Ollama and
            llama.cpp host helpers, units for the services built from images
admin/      the admin page
i18n/       seven catalogues and the Python/PHP/JS bindings
services/   the twelve service directories the shipped services build from
docs/       the conventions that span more than one service
```

## Documentation

| Doc | About |
|---|---|
| [CLAUDE.md](CLAUDE.md) | How the whole stack fits together, its rules, and what has gone wrong before. Start here. |
| [docs/architecture.md](docs/architecture.md) | The two layers: services and packaging |
| [docs/admin.md](docs/admin.md) | The admin page, its security, deploying from it |
| [docs/plans.md](docs/plans.md) | Multi-step requests: planner, step model, read-only steps, benchmarks |
| [docs/routing.md](docs/routing.md) | Which model a turn starts on, and escalation |
| [docs/local-ollama.md](docs/local-ollama.md) | Local models: servers, setups, llama.cpp, the model library, GPU sharing |
| [docs/optional-cloud.md](docs/optional-cloud.md) | The hosted model, Zen vs Go, the optional VPS proxy |
| [docs/provider-routing.md](docs/provider-routing.md) | Putting a routing proxy in front of the model callers |
| [docs/token-spend.md](docs/token-spend.md) | Three weeks of measured token use, and where it goes |
| [docs/backups.md](docs/backups.md) | Every path holding state, and how backups verify |
| [docs/uploader-contract.md](docs/uploader-contract.md) | The mirror an external uploader ships |
| [docs/plugins.md](docs/plugins.md) | Bringing your own services |
| [docs/mqtt-conventions.md](docs/mqtt-conventions.md) | Topic grammar across the stack |
| [docs/opencode-programmer.md](docs/opencode-programmer.md) | The coding assistant space |
| [docs/migration.md](docs/migration.md) | Moving an existing install |
| [docs/roadmap.md](docs/roadmap.md) | Work that is intended and not started |

Each service also has its own README, and `services/nanobot/AGENTS.md` explains
what this stack changed in its nanobot fork.

## Privacy

This repository contains no household data. The family that runs it appears
in tests and examples only under invented names, the shipped databases are
empty, and `deploy/sanitize.py --check` re-checks the tree for personal data
(add your own identifiers to `deploy/sanitize-rules.local.py`, which git
ignores, so it can look for them too).

## License

MIT — see [LICENSE](LICENSE). Third-party code included in this repository
keeps its own license, listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md),
which also names the pulled-in dependencies that are *not* permissive (the
camera detector is AGPL-3.0, n8n is source-available) — worth reading before
you distribute built images or run the stack for other people.

Built on, among others: [nanobot](https://github.com/HKUDS/nanobot),
[Ollama](https://ollama.com), [llama.cpp](https://github.com/ggml-org/llama.cpp),
[faster-whisper](https://github.com/SYSTRAN/faster-whisper),
[Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx),
[SearXNG](https://github.com/searxng/searxng),
[crawl4ai](https://github.com/unclecode/crawl4ai),
[ntfy](https://ntfy.sh), [Eclipse Mosquitto](https://mosquitto.org) and
[Home Assistant](https://www.home-assistant.io).
