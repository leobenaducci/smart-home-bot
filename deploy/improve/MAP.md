# How Alfred is built — for whoever fixes him

Read this, then `RULES.md` and `CLAUDE.md` beside it, then `lessons.md`,
before you propose anything. Everything here is checked against the code; when you find it wrong,
say so, and it is corrected.

## Where things are

- `services/` is the applications; `deploy/`, `admin/`, `i18n/`, `config/` are
  the packaging around them. `CLAUDE.md` is the authority on the rules;
  `services/nanobot/AGENTS.md` on the assistant.
- **The assistant is nanobot**: one container per member (`nanobot-<member>`)
  and one for the rooms (`nanobot-house`, voice; most personal skills are off
  there, `profession` included).
- **Its configuration** is `services/nanobot/config/config.json` plus
  `config/instances/<name>/` for what an instance changes. The per-member files
  are rendered by the deployer; an overlay deep-merges dictionaries and
  **replaces lists** (a member overlay with `disabledSkills` must repeat the
  base's). `null` deletes a key.
- **What belongs to this house** — a service it does not use, an address, a
  model — is in the live config (`/var/lib/home-stack/config`, edited on the
  admin page), in a plugin's own repository, or in state. Never in the stack's
  shipped files: the repository is public and serves every household.

## Skills

- A skill is `nanobot/skills/<name>/SKILL.md` (and usually `SKILL_PYTHON.md`).
  A plugin serves its own at `GET <its API>/skill`; the assistants fetch those
  every five minutes into `.skills-remote/`, with no assistant deploy.
- **Always-loaded** (`always: true`): the body is in every prompt. **On
  demand**: only the one-line description is, in the catalogue; the body is read
  when the model decides to. Measured on 2026-09-10: a skill the model only saw
  as a description was called wrongly far more often (chores 2/12 against geo's
  8/12, always-loaded). Demoting one needs its description to carry the actions
  and how to invoke them, and the benchmark before and after.
- **How a call runs**: the model writes `{"skill": …, "action": …}` as text; the
  runner (`agent/runner.py`) intercepts it. For a translatable skill it builds
  Python *locally*: `SKILL_PYTHON.md`'s code, which **defines the functions**,
  then one printed call (`_static_skill_translation`), run by the exec tool in
  the container. None of that reaches the model; without the guide the call is
  a NameError.
- Actions whose names start with a read prefix (`list`, `get`, `search`, …) are
  reads; anything else acts, and is refused from WhatsApp, from another member's
  question and in a read-only plan step.

## Turns, routing and professions

- A local classifier (`agent/classify.py`) labels each turn: chat, action,
  complex, long, background. `long` goes to pi in the background when the
  household turned `harness.long_tasks` on; `background` to a sub-agent.
- **Professions (Designer, Teacher, Programmer…) are profiles of the same
  member's instance** — a model swap, not another assistant. Delegating
  (`profession` skill → `/chat/api/delegate`) runs a turn in the same container
  with `profile=<space>`. A skill disabled in an instance is disabled for every
  profession in it.
- The Programmer space can run on `opencode serve` for a member (you); fix
  requests reach you through the `self-improve` skill, never through
  `profession`, which goes to the nanobot profile.
- The system prompt is the startup files (`AGENTS.md`, `SOUL.md`, `TOOLS.md`,
  the member's `USER.md`…), the always-loaded skill bodies, the skill catalogue
  and the portal's standing context; history is capped
  (`ContextBuilder._MAX_HISTORY_CHARS`). Most of it is served from cache.

## Evidence

- `improve measure usage|stops|events|tools` — what turns cost, how they end,
  what the runner noticed, which tools they call. From `usage.db`.
- `improve test <id> <repo>` — the suites of what you changed.
- `improve bench <id> <repo> [--roles …]` — the benchmark on the running code
  and on yours. Its cases are not yours to read.
- `improve commit` refuses without a passing test run for the exact change, and,
  for a change to the assistant's behaviour, without a benchmark run that did
  not get worse.

## Deploying

- The stack: `improve deploy` deploys the services whose files a fix changed,
  then admin. Deploying an assistant cuts off the turns it is running.
- A plugin or extension: the services its `plugin.yml` declares
  (`repos.json`'s `deploys`).
- You run publish and deploy only when the person says so, and never by hand.
