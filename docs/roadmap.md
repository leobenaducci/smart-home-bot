# Intended work

Three things are planned and not started. Each is written down with what it
would touch and what it runs into, because each collides with something this
package states as an invariant, and the collision is the interesting part.

None of them is a commitment to a design. They are here so the next person
does not have to rediscover the constraints.

## Let the assistant adapt a code project as an extension

**The idea.** Point the assistant at a code project and have it become an
extension — the skills and tools derived from the project, rather than written
by hand alongside it.

**What exists today.** Two surfaces, and both need a person:

- A **plugin** (`docs/plugins.md`) contributes a service plus a skill: a
  `floor:` directory holding a hand-written `SKILL.md`, the `env:` the skill
  reads, and an `allowed_env_keys:` entry so its code may read that address.
- **`services/nanobot/nanobot/skills/<name>/SKILL.md`** in-tree, for the ones
  this package ships.

Nothing reads a project and produces either. That is the gap.

**What it runs into.**

- **Where a generated skill may live.** `SkillsLoader` checks workspace, then
  remote, then builtin — so anything written into the workspace permanently
  beats the live copy the service serves. That is why `skill_floors:` stages
  into *builtin*, and a generator has to respect the same order or it will
  silently pin whatever it wrote first.
- **`tools.exec.allowedEnvKeys` is a security boundary**, and additive on
  purpose. An extension that widens it to make itself work has removed the
  thing that keeps a skill from reading every credential in the process.
- **The house instance takes untrusted input from a room.** An extension
  reachable from there is a different trust question from one on a member's own
  instance, and `disabledSkills` is how that line is currently drawn.
- **A skill's whole description enters every prompt** (see
  `build_skills_summary`), so a generated one that changes between deploys
  makes prompts unreproducible and moves the cache-hit rate the profiler
  measures — the thing the panel exists to explain.

**Open question.** What the unit even is: a repository, a CLI, an HTTP service.
Each implies a different generated skill and a different failure when the
project changes underneath it.

## A demo branch, deployed to the VPS

**The idea.** A branch that stands the stack up as a public demo, on the VPS.

**What it runs into, first.** The VPS invariant, stated in `CLAUDE.md` and in
`docs/optional-cloud.md`: *it runs no application code and stores no household
data.* It terminates the public connection and forwards everything down a
reverse tunnel to the machine at home, which serves the pages, holds the user
store and verifies passwords. A demo deployed **on** the VPS inverts that
sentence.

So the demo needs one of two shapes, and it is worth deciding which before any
code:

1. **The demo runs elsewhere and the VPS stays a proxy.** The invariant holds
   unchanged, and the demo is just another origin behind the same tunnel.
2. **The demo is its own deployment, with its own config and no household
   data at all** — in which case the invariant is about *the household's* VPS
   and should be reworded to say so, rather than quietly acquiring an
   exception.

**The rest, which is known and cheap to write down now:**

- **Deploy serially.** Concurrent docker builds on the test VPS fail on disk
  and on tag races — `nanobot-house` and the per-member units build the same
  `alfred-nanobot:latest` tag. Measured, not predicted.
- **It will not fit the whole stack.** `services.home-cameras.gpu: off` toggles
  an overlay and nothing else: the web server's Dockerfile is
  `FROM nvidia/cuda:...` either way, so the build pulls the CUDA runtime on a
  box with no card. `docs/migration.md` puts that image near 13 GB and
  whisper's near 5 GB. Choose the subset deliberately.
- **The admin page must not be reachable.** `admin_is_not_proxied()` asserts no
  route to it exists in either proxy, and it is checked because it is one
  Caddyfile line away from being untrue. A public demo that exposes it hands
  over the docker socket, the credentials file and the Deploy button.
- **Seed data has to be invented, not borrowed.** The family directory ships
  empty on purpose and `_FAMILY_SEED` is `{}` with a comment explaining why. A
  demo needs content to be worth looking at, that content must not be a
  household's, and `deploy/sanitize.py --check` still has to pass on the branch.
- **`OPENCODE_API_KEY` is the only credential that leaves the network.** A
  public demo spends it on whoever finds the demo. That wants a spend limit, a
  separate key, or a stubbed model — decided before the thing is reachable,
  not after.

## A coding harness behind the broker — built, and removed

**The idea was** to put a second model behind the broker: the assistant asks for
a change, a coding model makes it in the checkout, the assistant reviews and
commits. It was built end to end — [omp](https://omp.sh) pinned by version and
digest in its own image stage, a `POST /v1/projects/{slug}/work` verb answering
before the run finished, a job store, one run per member, and a persona telling
Alfred to delegate. Verified in isolation: told to change one word in a file it
changed it, answered, and left the commit alone.

**It was removed the same day, and the reasons are worth more than the code.**

- **A prompt cannot stop a tool the model holds.** Asked for a change with
  "Who writes the code: not you" in his 10,798-character standing block, Alfred
  edited three files himself with 26 `exec` calls, on `master`, without
  branching. The persona reached him; he had it and edited anyway. The harness's
  own safety rests on `--tools` being a whitelist validated before startup —
  *"what keeps this safe is the argument list, and nothing else"* — and that
  standard was applied to omp and not to the thing calling it.
- **Delegation doubles the failure surface.** The harness ran on
  `opencode/kimi-k2.7-code`, which answered 503 all evening. A perfect
  delegation would still have failed, and the assistant would have been reporting
  somebody else's outage as its own.
- **The visibility never arrived.** Sub-agents report progress through
  `_subagent_event`, and the skill can post to `/chat/agent-event`, but the
  `chat_id` names a conversation rather than an instance, so it cannot come from
  the environment and the model has to pass it. Worse, a capture run showed omp's
  RPC stream emitting no tool-call frames at all for a file-editing prompt — the
  thing to stream may not be on the wire.

**What made it unnecessary** was already true: `tools.restrict_to_workspace`
defaults to `False` and is unset here, so `allowed_dir` is `None` and the
assistant's own `edit_file` / `write_file` already reach the checkout. He has a
real edit tool that takes `old_text` and `new_text`. He reached for `sed -i`
because the skill named `exec` for the checkout and pointed everything else at
`work()`.

**So the assistant is the harness.** The persona and the skill now say: branch
before the first edit, edit with the file tools and not the shell, read before
writing, tests, then commit. The enforcement stays where it always was and where
it works — `commit` refuses on trunk, holds the protected-paths floor, and
prefixes the branch — because that is a rule in code rather than a sentence in a
prompt.

