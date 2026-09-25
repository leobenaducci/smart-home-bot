# Plans: a planner that writes steps, a small model that runs them

A request that takes several steps ("test every light and cross-reference them
with Home Assistant") is not answered in one go. The turn's model writes a
**plan**, and each step is carried out by a second, usually smaller, model that
sees only that step. The planner reads each result before running the next,
changes the plan when a result calls for it, and writes the answer.

The split exists so the part that needs judgement (what to do, in what order,
and what the person actually asked to change) runs on a strong hosted model,
and the part that is mechanical (call this skill with these arguments, report
what came back) can run on the house's own card.

Code: `services/nanobot/nanobot/agent/tools/plan.py` (the `plan` tool and
`TurnPlan`), `AgentLoop._step_runner` and `tools_for_step` in
`services/nanobot/nanobot/agent/loop.py`, `build_step_system_prompt` in
`services/nanobot/nanobot/agent/context.py`.

## Who plans, who runs a step

| Role (Models page) | Config | What it does |
|---|---|---|
| Planner | `assistant.models.planner` | Writes the plan, reads each result, answers. Falls back to the everyday model when unset. |
| Plan steps | `assistant.models.plan_steps` | Runs one step. Falls back to the sub-agent's model when unset, and to the everyday model when `routing.plan_executor: everyday`. |

The classifier sends a turn down this path when it labels it `long`; the turn
is then told to call `plan` with `action=set` and three to eight steps, each an
**exact instruction**: the skill or tool to call, its arguments written out,
and what to report. "Call skill lights, action flash_light, for each of:
Oficina Tomi, Luz Paula. Report which calls succeeded."

A step the step model cannot finish (it errors, dead-ends or answers nothing)
runs again on the planner's model, with every tool, and with what the first
attempt already did.

## What a step gets

A step's prompt is slim on purpose. The full assistant prompt is ~22k tokens,
and on a local model that prompt *is* the cost: Qwen3.8-27B on one RTX 3060 read
it at ~500 tokens/s, two minutes before the first word of every step.

- **Prompt:** the identity, `TOOLS.md`, one catalogue line per skill, and in
  full: the always-loaded skills the request names, and **any skill a step
  calls by name** ("Call skill lights", "with the chores skill"). Offered only
  its catalogue line, Bonsai 2 read the lights `SKILL.md` seven times in one
  step and never called it.
- **Tools:** only what the step's text calls for (`tools_for_step`): tools it
  names, Home Assistant when it says Home Assistant, web search and fetch when
  it says web, `cron` when it says schedule, `describe_image` for images, and
  `read_file`. With all ~60 of the assistant's tools in its list, a small model
  answered "flash each light" by scraping example.com and a chores step by
  writing seven files. A skill is not a tool here: it is a `{"skill": ...}`
  block the runtime runs, so a step that only calls skills is offered almost
  nothing, which is the point.
- **No shell.** `exec` is registered but never offered, and runs only commands
  the runtime generated itself: translated skill calls and the runtime's own
  notes. A step that writes a shell command is told there is no shell.
- **History:** earlier steps' skill calls appear as calls named after the
  skill, not as an `exec` with a comment -- a model copied that comment back as
  a command.
- **Budget:** `routing.plan_step_calls` model calls (24), thinking off
  (`routing.plan_step_effort: none`).

## Steps that change things

The planner lists, in `acts`, the steps that change something the person asked
to change: switch, add, send, schedule, rename, delete. **Every other step can
only read**, and the guard is the runtime's, not the model's:

- a read-only step is told so before it starts;
- acting tools are not in its list (`cron`, `message`, `write_file`,
  `edit_file`, and Home Assistant's `Hass*` intents except `HassGet*`);
- a skill action that is not a read is refused when called;
- a refusal reaches the planner in the runtime's words -- "Not done: this step
  only reads, and it tried to change something -- lights.turn_on" -- because a
  stopped step still tends to report the change as made.

This exists because it went wrong both ways: a step asked to cross-reference
the lights turned every lamp in the house on, and a read-only step scheduled a
retry the planner then scheduled a second time.

## Long plans

A plan still running `routing.plan_detach_seconds` (240) into a chat turn is
**detached**: the chat gets one line, and the same run -- planner, step model,
what is already done -- continues in the background and posts its answer to
the conversation. The limit sits under the API server's 300 s request timeout,
which otherwise cut the turn and restarted it from scratch.

## Measuring it

Two groups in the model benchmark (`services/nanobot/bench/`, cases in
`cases.json`), both reachable from the Models page's Test button:

- **`steps`** -- one step through the real step runner (the slim prompt, the
  tool selection, read-only unless the case says the step acts). What the Plan
  steps role is judged on. The planner is stood in for, so a step that
  dead-ends is a failure rather than quietly rescued.
- **`planner`** -- the plan a model writes for a request, with the steps
  answered without running: does each step name what to call, are the steps
  that change something in `acts`. What the Planner role is judged on.

```bash
docker exec nanobot-user1 python /app/bench/model_bench.py --model <role model> --roles steps
docker exec nanobot-user1 python /app/bench/model_bench.py --model deepseek-v4-flash --roles planner
```

The lights cases flash real bulbs, so they have to name real ones. The package
names invented ones; a household maps them to its own in
`/shared-state/bench-house-names.json` (`{"invented": "real"}`) on the
assistants' shared state volume -- outside the repository.

Measured 2026-09-25: deepseek-v4-flash 5/5 as planner; gemma4:e4b on the steps
group took two model calls a case, three to fifteen seconds each, fully on one
12 GB card. Of the local models tried, Qwen3.5-9B made shell calls it had not
been offered and Bonsai 2 27B (PrismML's ternary build) needed every one of the
changes above and still wandered between tools.
