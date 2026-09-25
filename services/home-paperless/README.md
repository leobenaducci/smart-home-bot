# home-paperless

Deployment of [paperless-ngx](https://docs.paperless-ngx.com/) — the household's
document archive (scans, bills, contracts, anything that arrives on paper or by
mail). **There is no application code in this repo**: it is a `docker-compose.yml`
and the Jenkins pipeline that deploys it.

Runs on **`storage`**, reachable as **`http://paperless.home:21030`**.

## The stack

| Service | Image | Notes |
|---|---|---|
| `paperless` | `ghcr.io/paperless-ngx/paperless-ngx:latest` | The app. The **only** service that publishes a port (21030 on the host, 8000 in the container). |
| `paperless-postgres` | `postgres:16` | |
| `paperless-redis` | `redis:7` | Task queue |
| `paperless-gotenberg` | `gotenberg/gotenberg:8` | Office → PDF conversion, JavaScript disabled |
| `paperless-tika` | `apache/tika:latest` | Text extraction |

Everything but `paperless` is reachable only on the compose network, which is why
the internal Postgres credentials are literals in the compose file.

## Storage

All state lives on the host under `/mnt/storage/paperless`, bind-mounted:

| Host path | Purpose |
|---|---|
| `/mnt/storage/paperless/data` | Paperless' own data dir (search index, etc.) |
| `/mnt/storage/paperless/media` | **The documents themselves** |
| `/mnt/storage/paperless/consume` | Watched intake folder — drop a file here and it gets ingested |
| `/mnt/storage/paperless/postgres` | Database |

These are outside any Jenkins workspace by design; a re-checkout or workspace
wipe must never be able to touch the archive.

## Ingestion

Two ways in, besides the web UI:

- **The consume folder** — anything written to `/mnt/storage/paperless/consume`.
- **Email** — IMAP polling every 300 s against the household mailbox
  (`PAPERLESS_ENABLE_MAIL`, `PAPERLESS_MAIL_*`), folder `INBOX`.

## AI

Paperless's **built-in** AI suggests a document's title, tags, correspondent,
type and date. It reads the document's **OCR text** -- the first 4,000
characters -- and never the scan (`paperless_ai/ai_classifier.py`, 3.1.3), so
any text model that writes JSON will do; it does not need vision.

Everything comes from the config, through the deployer (`paperless_*` in
`deploy/deploy.py`), under the names Paperless reads (`PAPERLESS_AI_LLM_*`):

| Setting | What it does |
|---|---|
| `assistant.models.documents` | The model. Blank turns the AI **off**. A household's own Ollama gets Paperless's Ollama client -- structured JSON, thinking off -- at the server's root; anything else its OpenAI-compatible client, which answers through a tool call. |
| `cloud.ollama.local.context` | The window that Ollama serves. Paperless sends a window of its own, and Ollama reloads a model on every request that names a different one -- see `docs/local-ollama.md`. |
| `services.home-paperless.embeddings` | The index (`ollama:embeddinggemma` here). Without it the model never sees the tags, types and correspondents that exist and invents new names for everything. Absent, not empty, when unset: Paperless refuses to start on an empty embedding backend, which is what the unit's `env_omit_empty` is for. |
| `locale.default` | The language suggestions are written in, and with each member's locale the OCR languages (`spa+eng` for a Spanish house). |

**Until 2026-09-12 none of this was connected.** The stack exported
`PAPERLESS_AI_MODEL`/`_ENDPOINT`/`_PROVIDER`/`_API_KEY`, which Paperless does
not read, so the AI was off; and the fallback it named, `minicpm-v:8b`, cannot
call tools, so it would not have answered if it had been on. The separate
`paperless-ai` / `paperless-gpt` sidecars were removed earlier (`3cf4fa2`); their
`paperless-gpt` marker tag was still on most documents and has been deleted.

### Suggestions, and the one workflow

The document page shows the AI's suggestions to accept by hand. What is applied
**automatically** is decided by a Paperless workflow, which lives in its
database (backed up with `paperless-db.sql`), not in this repository:

> **IA: tipo y remitente** -- trigger *Document Added*, any source; action
> *Apply AI suggestions* for **correspondent and document type only**;
> *create missing* off, *overwrite existing* off.

That scope is what a dry run over this house's documents supported, with the
embedding index on (`gemma4:e4b`, 2026-09-12, documents that have text):

| Field | Right | Wrong | No suggestion |
|---|---|---|---|
| Document type | 9 | 0 | 9 |
| Correspondent | 8 | 0 | 11 |
| Tags | 13 | 2 | 3 |
| Title | generic ("Liquidación de Sueldo" five times), and worse than the family's own | | |

Without the index the same run matched one type, no correspondents and one
tag, and invented 85 tag names. Tags stay manual, titles are never automated,
and nothing is ever created or overwritten. Before widening the workflow, run
the dry run again:

```bash
docker cp services/home-paperless/tools/ai_dryrun.py paperless:/tmp/
docker exec -d paperless sh -c 'cd /usr/src/paperless/src && python3 manage.py shell < /tmp/ai_dryrun.py > /tmp/ai-dryrun.log 2>&1'
docker exec paperless sh -c 'wc -l < /tmp/ai-dryrun.jsonl'   # one line per document, then {"done": true}
```

It calls the same functions the workflow does and writes nothing to the
database; ~30 s a document. The container has no `ps` or `pgrep` -- walk
`/proc` to see whether it is still running -- and `docker exec paperless wc -l <
file` reads the *host's* file, not the container's, so keep the redirect inside
`sh -c`.

### Who can see a label

Every tag, document type and correspondent here is **viewable by the whole
family and changeable by the parents** (users and groups, set in Paperless). A
label created without that is invisible to the people whose documents carry it
-- and the AI, which matches names by the document owner's visibility, cannot
match it either: the correspondents above scored 0 until their permissions were
fixed. When creating one outside the UI, copy the permissions from an existing
label.

### Documents it cannot read

A **password-protected PDF** has no text and no thumbnail ("This file requires a
password for access" in the log). With the index on, the AI then borrows from
similar documents instead of saying nothing -- one got a payslip's type that
way. Paperless's *remove password* action, with the password, fixes the
document; until then the workflow only ever fills an empty field, so the damage
is one wrong value on an unreadable file.

## Alfred

Alfred reads this archive through nanobot's `paperless` skill, using **per-user
API tokens** (`PAPERLESS_API_TOKEN_USER_1…5`, Jenkins credentials injected by the
`deploy-alfred` job) — so each family member sees the archive as themselves. That
skill lives in the `nanobot` repo, not here.

## Secrets

Two, both injected at deploy time — nothing secret is committed here:

| Env var | Jenkins credential | Purpose |
|---|---|---|
| `PAPERLESS_SECRET_KEY` | `PAPERLESS_SECRET_KEY` (Secret text) | Django `SECRET_KEY` — signs session cookies and tokens |
| `MAIL_PASSWORD` | `paperless-mail-password` | IMAP password for email ingestion |

`PAPERLESS_SECRET_KEY` is written `${PAPERLESS_SECRET_KEY:?...}` rather than with
a default **on purpose**: an unset key must fail loudly at `compose config` time
rather than quietly start a container whose cookies are signed with a value
anyone can read on GitHub. For a hand-run outside Jenkins, put it in a `0600`
`.env` beside the compose file.

`paperless-api-token` is a *different* credential and is not used by this
pipeline — that one belongs to Alfred's per-user access (see above).

## Deploying

A **manual** Jenkins run (`Jenkinsfile`, agent `storage`) — pushing does not
deploy. Stages: `docker compose config -q` to validate, then `docker compose pull
&& docker compose up -d --remove-orphans`, then a health check that **polls**
`paperless.home:21030` for up to 180 s and fails the build with container state
and the last 40 log lines if it never answers. Paperless runs migrations and
warms its index on boot, so it answers late rather than never.

**`COMPOSE_PROJECT_NAME=paperless` in the Jenkinsfile is load-bearing.** Compose
derives a project name from the directory it runs in, so the same file deploys as
`paperless` by hand from `/home/homestack/paperless` and as `home-paperless`
from the Jenkins workspace. All five services pin an explicit `container_name:`,
and container names are global rather than project-scoped, so the second project
cannot create them — you get `Conflict. The container name "/paperless-tika" is
already in use`. Pinning the project name makes Jenkins adopt the running stack
instead of standing up a second copy. Change it and the next build orphans every
running container.

## Known caveats

- **Every image is `:latest`, with a `pull` on each deploy.** A build can pick up
  a new upstream release with nothing in this repo changing — which is exactly
  what happened on **2026-07-31**: a pulled paperless-ngx image began refusing to
  start without `PAPERLESS_SECRET_KEY` (it used to warn and fall back to
  `change-me`) and the container went into a crash loop. Nothing here had
  changed; the upstream requirement had.
- **`PAPERLESS_OCR_LANGUAGE` follows the household's languages** -- the house
  default, then each member's, then English, as Tesseract names them (`spa+eng`
  for a Spanish house). The deployer derives it (`ocr_language()` in
  `deploy/deploy.py`); it was a literal `eng` until 2026-09-12, although the
  image carries deu, eng, fra, ita and spa. Chinese and Japanese are not in the
  image and are left out. Documents already consumed keep the text they were
  read with: re-running OCR on them is Paperless's `document_archiver
  --overwrite`, and it is slow.
