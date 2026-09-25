# alfred-mcp

Alfred's own capabilities, offered to `opencode serve` as MCP tools.

opencode brings its own `read`, `grep`, `edit`, `bash`, `webfetch` and
`websearch`. This brings the things it has no way to know about: this
household's projects, its file share, and its other professions.

`docs/opencode-programmer.md` in the repo root is the why. This file is the
what and the how.

## The one rule

**Identity comes from the HTTP header, never from a tool argument.**

Not one tool here takes a `user`, a `member` or a `login`, and
`tests/test_identity.py` asserts that none ever will. There is nothing for
them to take: one container serves one person and holds only that person's
credentials, so there is no decision to make about who a call is for.

That matters because the thing calling these tools is a model reading a
prompt, and a prompt is something anybody who can get text in front of it can
write — a page it fetched, a file in a repo somebody else committed, a
forwarded notification. It is the same reason the code broker exists at all:
*"the broker cannot take the agent's word for who is calling."*

## What it does not do

It re-implements nothing. Every decision worth making already belongs to
something else and stays there:

| | owned by |
|---|---|
| may this person open this project | the portal's project registry |
| where it is checked out, which paths are protected, whose name is on the commit, which credential pushes | the code broker |
| may this path on the share be written | HomeCore |
| what the Designer makes and how | the Designer's own Alfred |

This translates one MCP call into one HTTP request and the answer back into
something a model can read. Where it does more than that, it is doing something
wrong.

## The tools

| | |
|---|---|
| `list_projects` | what this member may work on |
| `checkout_project` | clone or refresh, and say where |
| `project_status` | branch, and what is modified |
| `create_branch` | before the first edit, not before the commit |
| `commit_changes` | through the broker, so protected paths and the co-author line apply |
| `push_branch` / `pull_project` | the remote, with the broker's credential |
| `write_deploy_script` | store the script that deploys a project (the script, not a path to one) |
| `deploy_project` | run it, with the project's credential in its environment |
| `save_text` | write text to the person's share folder, return the chat link |
| `share_project_file` | copy a file out of a checkout onto the share |
| `delegate_to_designer` | commission a visual piece |
| `house_context` | which member, which folder, which checkout root |

`bash` and `git` would do several of these, and that is exactly the thing to
keep in mind: **git must go through the broker.** A commit made through
opencode's own shell skips the access list, the protected paths and the
co-author line, and none of that fails visibly — the commit simply lands, with
the wrong identity, possibly in a repo the person was never entitled to.

## Running it

Deployed like any other service:

```bash
./home-stack deploy alfred-mcp
curl -s 127.0.0.1:21071/healthz     # {"ok":true,"member":"user1",...}
```

`/healthz` answers **503 when the broker is unreachable**, because that is the
failure that turns every git verb into a refusal while the container itself
looks perfectly fine. The broker's own `/health` returns 200 while saying
`"ok": false`, and docker called that healthy for a while.

The suite takes no arguments and reaches no network:

```bash
cd services/alfred-mcp && pytest -q
```

`tests/conftest.py` sets the environment **before** `alfred_mcp.server` is
imported, because `config.load()` runs at import. Setting it afterwards is the
same shape as the packaging suites that pointed their path constants at scratch
too late and edited live household state.

## Configuration

All per-member, all supplied by the deployer from `deploy/manifest.yml`
(`alfred_mcp_identity: true`).

| | |
|---|---|
| `ALFRED_MCP_MEMBER` | `user1` — the broker and the checkout are keyed on this |
| `ALFRED_MCP_LOGIN` | the number the person signs in with — every portal request is keyed on this |
| `ALFRED_MCP_FOLDER` | `share_folder(member)` — paths on the share are made of this |
| `ALFRED_MCP_TOKEN` | what opencode must present; derived from `ALFRED_MCP_SECRET` |
| `CODE_BROKER_URL` / `CODE_BROKER_TOKEN` | the broker, by name on `nanobot-net` |
| `HOMECORE_URL` / `HOMECORE_PROXY_TOKEN` | the portal, for the share and delegation |

Three ids name a person and none is derivable from the others. Converting
between them happens once, in the deployer, and never in here.

`ALFRED_MCP_SECRET` is its own key rather than a reuse of the broker's or the
proxy's, and that is the point: the token it derives is written into
`opencode.json` on the **host**, where a person can read it. If it were the
broker's token it would open the broker directly; if it were the proxy's it
would open that person's whole portal account. It opens this bridge, which
authenticates every call again.
