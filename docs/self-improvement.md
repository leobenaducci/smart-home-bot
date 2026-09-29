# Self-improvement

For whoever builds the rest of this, or has to switch it off. It describes a
pipeline that reads what went wrong in the assistant's turns, proposes a fix,
proves the fix is better, and ships it -- and which parts of that are a model's
judgement and which are code that cannot be talked out of a rule.

**Status:** phase 1 (the evidence) is built. Everything after it is the plan.

## The shape

```
nightly, on the house              on request, sandboxed            no model
collect -> redact -> label   ->    improver: pi + a coding   ->    gate: paths, suites,
-> cluster into issues             model, one issue, one           privacy, blind judge,
   + replay cases                  commit in a worktree            bench -> a person
                                                                   approves -> deploy
          ^---------- did the issue come back in the next 7 days? -----------'
```

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

What phase 1 does not have yet: a signal from the person. Stop (`interrupted`)
and fork (`branch_of`) are already in the portal's history; a 👍/👎 on an answer
is the next change.

## Phase 2: the evaluator (planned)

`deploy/improve/`, run nightly on the host, state in `{paths.state}/improve`.

- **collect** joins `token_usage`, `turn_events`, the portal's history, the
  sessions, `bgtasks.db`, the notification triage and the Studio's jobs into
  one episode per turn.
- **redact** before anything leaves the house: the live config's members, the
  user store's logins, phones and emails, and `sanitize-rules.local.py`
  become the invented cast, consistently within an issue; a local model finds
  the names no list knows. A self-test fails the run if a live identifier
  survives. The mapping goes to `bench-house-names.json`, as the benchmark's
  already does, so a replay can still reach a real device.
- **label and cluster**, by the evaluator model (Claude Sonnet 5), on the
  redacted episodes only: every episode with a hard signal (👎, Stop, a
  rephrase inside two minutes, a failing stop reason, a tool error, a
  fallback) and a small sample of the ones that look fine. Hard signals
  override its verdict.
- **replay cases** per issue, in `bench/cases.json`'s format, kept in state
  and not in git because they come from household data.

## Phase 3: the improver and the gate (planned)

The improver is pi (`@mariozechner/pi-coding-agent`, the version the harness
pins) with its coding tools, in a throwaway container whose only writable
mount is a git worktree of the working checkout. No docker socket, no state,
no env file; its one network route is a local proxy that adds the provider
key, so neither the model nor its shell ever holds it. One issue in, one
commit out.

The gate is code: allowed paths (never `deploy/`, `admin/`, the manifest, the
bench, or `deploy/improve/` itself), no loosened assertion, the service
suites, `sanitize.py --check` and `publish_check.py` on the branch, the
standing benchmark within its measured noise, the prompt's token count, and a
blind A/B judgement of the replay cases by a fresh evaluator that never sees
the diff or the improver's claim. Then a person approves, and the deploy
follows `CLAUDE.md`'s rules: a clean checkout, no deploy running, no
assistant mid-turn, no benchmark, one change per deploy, admin last.

Nothing here pushes. Commits are generic by construction, and publishing
stays the person's (`CLAUDE.md`, "Publishing").
