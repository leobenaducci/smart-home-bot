# Routing: which model a turn starts on, and what happens when it fails

Since 2026-09-21. Before it, the model for a turn was the caller's choice
alone: HomeCore marks the turns inside a Profesión as `powerful`, the main
model marks a sub-agent it spawns with `complex=true`, and everything else
ran on the cheap model. Nothing looked at the request. *Buscá las luces
azules y verificá que el nombre coincida en WiZ, lights y HA* -- three
systems and a comparison -- ran on the model that answers *prendé la luz del
living* in prose without invoking anything (measured that day: 5/7 tool
cases, against the strong model's 7/7).

Two mechanisms, and they are only safe together.

## 1. The classifier: a guess before the turn

`services/nanobot/nanobot/agent/classify.py`. A small local model reads the
request and answers one JSON line with one of three labels:

| label | means | tier | effort | iterations |
|---|---|---|---|---|
| `chat` | conversation, a fact, a translation, thanks | everyday | as configured | as configured |
| `action` | one or two steps in ONE system: a light, a photo, a list, a reminder | everyday | as configured | as configured |
| `complex` | several steps, more than one system, verifying or comparing, research, code, a document | **powerful** | powerful's | `complex_iterations` |

It sees the message, the previous assistant turn (so *sí, dale* inherits
what was proposed), and whether attachments came with it. Two fast paths
never ask the model: an attachment is `complex`, and so is anything over
`long_message_chars`. The caller's own flags always win and skip the
classifier: a Profesión, `powerful: true`, an image turn, a named profile.

It is a hint. The turn waits at most `timeout_s` for it; a timeout, a dead
Ollama or an unparseable answer all mean `action` -- the cheap tier with
escalation armed -- and say so in `source`. The classifier can never cost the
household a turn.

The model is `gemma4:e4b` on the local Ollama: already resident as `titles`,
`notifications` and `heartbeat`, it triages notifications at effort `none`
today, and a title costs it a median 0.29 s. Nothing here needs an embedding
API or a router trained on somebody else's chat.

## 2. Escalation: the fact after it

When an everyday-tier attempt ends with a `stop_reason` in `escalate_on`,
the strong model **continues the same turn**, once, before anything reaches
the person:

- `bad_invocation` -- the model wrote a skill block that resolved to no call.
  New with this work: it used to end the turn as `completed` with a note
  glued into the content, which is exactly the failure nobody could see.
- `error`, `empty_final_response` -- nothing usable came back.
- `repeated_tool_calls`, `max_iterations` -- it looped or ran out of budget.

Continuation, not re-run. When nothing executed, the failed attempt's
trailing message is dropped and the strong model answers the conversation
fresh. When tools *did* run, their results stay and one bracketed line tells
the strong model why it is here -- re-running `add_grocery` because the model
looped afterwards is the mistake that rule exists to stop. Sub-agents follow
the same rule in `subagent.py`.

After an escalation the session starts on the strong tier for `sticky_turns`
turns: a hard conversation should not pay the cheap failure on every message.

## Configuration

`/var/lib/home-stack/config/home-stack.yml`:

```yaml
assistant:
  models:
    classifier: ollama:gemma4:e4b
  reasoning_effort:
    classifier: none
  routing:
    mode: active          # off | shadow | active
    timeout_s: 3.0
    long_message_chars: 600
    sticky_turns: 3
    max_escalations_per_turn: 1
    escalate_on: [bad_invocation, error, empty_final_response, repeated_tool_calls, max_iterations]
    subagents: true       # classify a spawned task when the spawner did not say
    complex_iterations: 80
    everyday_iterations: 40
```

`shadow` classifies and records every turn and changes nothing -- a day of
labels against real chats before trusting them. `off` asks nothing: the
ladder reads only the caller's flags, as before. No `classifier` model
configured is the same as `off`.

Sub-agents: the `spawn` tool's `complex` is now tri-state. `true` and `false`
are the spawner's word and win; left out, the task text is classified like a
chat turn.

## The first day, measured

The request that started this -- *buscá las luces azules y verificá que el
nombre y la configuración coincidan en wiz, lights y home assistant* -- sent
through user1's Alfred unflagged, four times on 2026-09-21 as the pieces
landed:

1. Routed to `deepseek-v4-pro` (`complex`, by the model). Ran the lights
   skill, then sent `echo x`, `echo y` ... `echo done52` -- 83 calls in five
   minutes, none repeating by exact arguments, so the guard never fired.
   → the guard counts every no-op exec as one call.
2. The classifier **timed out** at 1.5 s (the real prompt costs
   `gemma4:e4b` 600-700 ms warm; the first call after a restart is slower),
   defaulted to `action`, and `deepseek-v4-flash` spent 42 rounds of
   hand-written `exec` and `grep` before the API timeout.
   → `timeout_s` 3.0, a warm-up at start, `everyday_iterations` 40.
3. Routed right; the guard fired at 8 no-ops after 13 calls and 93 s, and
   the turn ended with "I stopped instead of looping" -- while the lights
   list and the HA context sat in the tool results.
   → once the guard fires on a turn in which tools ran, one call with the
   tools off asks for the answer from what it has.
4. Routed right, 22 tool calls, guard at 8, and the tools-off call wrote
   the answer: a table of every light across the `lights` skill, Home
   Assistant and WiZ, naming the two Dormitorio Principal lights that carry
   different names in HA, the duplicated Living entry, `Luz Paula`
   unavailable, three lights with no HA entity and three Zigbee repeaters
   with no `lights` entry. 96.5% of its prompt tokens were cache hits.

The classifier alone, on the 28 labelled cases in `bench/cases.json`:
`complex` 5/5, every miss a chat↔action swap (same tier), median 397 ms.

## Watching it

Every usage record carries `tier`, `label` and `route_source`
(`forced` / `sticky` / `model` / `fast_path` / `default` / `shadow:*` /
`escalation`). `/stats` has a **By routing** table. The number to watch is
**escalations per label**: an escalation from `action` is the classifier
being wrong; a large `complex` share with no escalations is it being
cautious, which costs money but not answers.

Score the classifier alone, in seconds, on the cases that name a `tier`:

```
docker exec nanobot-user1 python /app/bench/model_bench.py --classify-only
docker exec nanobot-user1 python /app/bench/model_bench.py --classify-only --model ollama:gemma4:12b
```

The `routing` list in `bench/cases.json` is the labelled set; today's failed
chats are the right thing to add to it.
