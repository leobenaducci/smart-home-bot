# Self-improvement

For whoever builds the rest of this, or has to switch it off. It describes a
pipeline that reads what went wrong in the assistant's turns, proposes a fix,
proves the fix is better, and ships it -- and which parts of that are a model's
judgement and which are code that cannot be talked out of a rule.

**Status:** phase 1 (the evidence) is built, and phase 2's house side --
collect and redact. The evaluator, the request path, the improver and the gate
are the plan.

## The shape

```
nightly, on the house        when a person asks, in the Programmer space      no model
collect -> redact     ->     evaluate: label, cluster, replay cases     ->    gate: paths, suites,
   (inbox)                   fix: one issue, one commit, in the repo          privacy, bench,
                             it belongs to                                    a person approves
Alfred, asked in chat  ->    (a request becomes an issue the same way)        -> deploy
          ^---------- did the issue come back in the next 7 days? -----------'
```

**Where the models run, and why only there.** Every model step -- evaluating,
fixing, judging a fix -- runs in `opencode serve` on OpenCode **Go**, Kimi K2
code, started by a person in the Programmer space. `CLAUDE.md` allows Go only
for that: opencode's own binary answering somebody at the keyboard. A nightly
job that opened an opencode session with nobody there would be the unattended
load Go's terms police, just routed through opencode, so **nothing model-driven
is scheduled**. What runs on its own is collect and redact, which are local.
(Measured alternative, 2026-09-29: GPT-6 Sol on Zen, per token, would have
been about $10-20 a month for the same evaluation, unattended.)

The judge is the weak point of one model doing everything, and three things
cover it: it is a fresh session that never sees the fix, the diff or the
fixer's claim; its rubric is written from the issue before any fix exists; and
the gate, which is code, can refuse what the judge liked.

A model proposes and a model judges; code decides. The roadmap has the reason
("A coding harness behind the broker -- built, and removed"): *a prompt cannot
stop a tool the model holds*. Every rule that matters here -- which paths may
change, that the evaluator is not among them, that nothing deploys over a live
turn or a benchmark -- is enforced by the gate, not asked of the improver.

## Phase 1: the evidence

Until 2026-09-29 the most useful facts about a turn were the least durable:
latency, failed calls and failed tools lived in the profiler's in-memory rings
(`nanobot/utils/profiling.py`), the runner's stop reason and its warnings --
DSML written as text, a skill block that resolved to nothing, an empty answer
retried, a refused action -- went only to the container log, which rolls over
in days, and the classifier's escalation fields were sent and dropped.

Now each usage report carries them, and HomeCore keeps them in `usage.db`
beside the bill:

| `token_usage` column | What |
|---|---|
| `stop_reason` | the runner's: `completed`, `max_iterations`, `repeated_tool_calls`, `tool_error`, `error`, `empty_final_response`, `bad_invocation` |
| `turn_id` | joins the rows one turn bills in parts -- an escalation reports the cheap attempt and then the answer, a plan reports each step |
| `latency_ms` | this row's part of the turn; NULL is "not reported" (older rows, the heartbeat), not instant |
| `call_errors`, `tool_errors`, `retry_wait_ms` | since the previous row of the same turn, so nothing is counted twice |
| `classifier_ms`, `escalated`, `escalated_from` | the routing's own numbers (`docs/routing.md`) |

`turn_events` holds what the runner noticed, as `(turn_id, code, n)`. The codes
are the whole vocabulary -- nothing a person wrote is ever in one:

| Code | Means |
|---|---|
| `parse:dsml` | DeepSeek's tool-call markup arrived as text and was rewritten |
| `parse:block_beside_calls`, `parse:block_unresolved`, `parse:block_no_call` | a skill-invocation block written as text: beside real calls, resolved to nothing, or the whole answer |
| `parse:dup_calls`, `parse:calls_ignored` | calls dropped as already run; calls under a finish reason that cannot carry them |
| `retry:empty`, `retry:finalize` | an empty answer retried; given up and forced to finish |
| `stop:repeated_tool_calls` | the loop guard fired |
| `llm:empty`, `llm:truncated`, `llm:fallback` | a completion with nothing in it; cut by `length`; answered by another model |
| `tool_error:<tool>` | a tool returned an error |
| `refused:whatsapp`, `refused:ask`, `refused:go_ahead`, `refused:read_only` | an action the runtime would not run, and why |
| `skill:no_code:<skill>`, `skill:bad_code:…`, `skill:timeout:…`, `skill:failed:…` | translating a skill call to Python failed |
| `context:repair`, `context:starved` | history repair failed; the system prompt filled the window |

A code is added with `note_event("…")` anywhere a turn runs; it lands on the
turn's profiler span and leaves with the next usage report. Codes are cleaned
to `[A-Za-z0-9_:.-]`, 80 characters, 40 per row, and age out with the rest of
`usage.db` (`USAGE_KEEP_DAYS`).

And a signal from the person. The runner's codes only see what the runtime
noticed, so an answer that ran cleanly and was simply wrong reads, from
there, as a success. Every answer in the chat now has 👍 and 👎 beside its
time, and a 👎 asks, optionally, what was wrong. `POST /chat/feedback` files it
on the answer in the portal's history, as `feedback: {rating, at, note}`,
where the question and the rest of the conversation already are. The answer
is found by its text and the nearest time rather than an id -- the page and
the server each file a reply with their own clocks, and only those two travel
with both. Stop (`interrupted`) and a fork (`branch_of`) were already there.

## Phase 2: collect and redact (built)

`./home-stack improve collect` (`deploy/improve/`), by default yesterday;
`--day`, `--days N`. It reads and never writes the house's records, and writes
one file per day to `{paths.state}/improve/inbox/` -- 0700, this user only.

- **collect** (`collect.py`) turns a day into episodes: each answer in the
  chat or a profession, joined to the usage rows its turn billed (an
  escalation's two rows are one episode), and every turn nobody reads in the
  chat -- events, background tasks -- from its usage alone. Each carries its
  **signals**: 👎, Stop, a correction or a rephrase within two minutes, a
  failing stop reason, failed calls or tools, an escalation, a slow answer,
  the runner's codes. Every episode with one is kept, and 8% of the rest
  (`--sample`), so the evaluator can say how often "no signal" is still wrong.
  A heartbeat is never sampled.
- **redact** (`redact.py`), before anything is written:
  1. the sanitizer's map (with the local rules) turns the household's names,
     logins and domains into the invented cast;
  2. e-mail addresses, private links and bare machine names (`box.home:5010`)
     become tokens, by shape;
  3. what the house knows about itself -- members and their relationships,
     logins, the family directory, saved places, WhatsApp contacts, e-mail
     accounts, the site's hosts, every credential in the env file --
     becomes a token, whole names and their parts, with or without accents;
  4. IPs, MACs, coordinates, phone numbers and long numbers, by shape;
  5. the local text model names what no list knows (a friend, a shop, a
     street), and code it offers -- a variable, a file, a call -- is kept:
     it identifies nobody and is what a technical failure is about.
  Tokens are `[persona-3f2a]`, keyed on `private/salt`, so one name is one
  token on every run and no table of real names is kept. Who asked is
  `member-xxxx`, the same way.
- **the guard** looks for every harvested value again, folded, in each
  finished episode, and withholds any that still has one; the run says how
  many, never which. On the first live run it withheld five of six, over a
  member's `whatsapp: true` harvested as a phone -- "true" is in every episode
  as JSON -- which is why flags are not harvested.

## Phase 3: evaluate, fix, judge (planned)

- **The evaluator** is an opencode agent (`deploy/host/opencode/agents/`)
  with no shell and no file tools: it reads the inbox and writes labels and
  issues through tools that serve only the redacted inbox. Hard signals
  override its verdict. It groups failures into issues and writes replay
  cases in `bench/cases.json`'s format, kept in state, not git.
- **A request is an issue too.** "The lights skill points to the wrong
  server -- fix it", said to Alfred, goes through a `self-improve` skill: it
  files an issue with the person's words and the episodes around it, and
  answers with a link that opens the Programmer space with the fix already
  asked. The person presses send, which is what keeps it interactive. Only a
  member marked `programmer` may; from WhatsApp or another member's question
  it is refused like every other acting skill.
- **Where a fix belongs** is part of the issue, because it is not always this
  repository. `assistant.improve.repos` in the live config names the
  checkouts the improver may work in -- this one, and a plugin's own
  repository (the lights skill is served by a plugin that lives outside this
  tree). A fix that is a setting (a URL, a model) is proposed as a change on
  the admin page, never written into code; a fix that only makes sense for
  this house goes to its plugin or its config, never here (`CLAUDE.md`,
  "Household data never enters git").
- **The fixer** works in a git worktree of the repo the issue names, one
  issue, one commit, on its own branch.
- **The judge** is a fresh session: blind A/B of the replay cases against
  the baseline, three runs each, rubric from the issue.

The gate is code: allowed paths (never `deploy/`, `admin/`, the manifest, the
bench, or `deploy/improve/` itself), no loosened assertion, the service
suites, `sanitize.py --check` and `publish_check.py` on the branch, the
standing benchmark within its measured noise, the prompt's token count, and the
judge's verdict. Then a person approves, and the deploy follows `CLAUDE.md`'s
rules: a clean checkout, no deploy running, no assistant mid-turn, no
benchmark, one change per deploy, admin last.

Nothing here pushes. Commits are generic by construction, and publishing
stays the person's (`CLAUDE.md`, "Publishing").
