---
name: chores
description: "Invoke with JSON: {\"skill\":\"chores\",\"action\":\"...\"}. The household's chores, points and prizes (HomeCore). In Spanish chores are usually \"tareas\": \"¿qué tareas tengo?\", \"ya hice la tarea\" and \"mis tareas de hoy\" mean these, unless it is clearly school homework. Everyone: list_chores | complete_chore | revert_chore | excuse_chore | excuse_day | postpone_chore | my_points | list_prizes | redeem_prize | list_redemptions. Parents only: adding, editing and deleting chores, recurring chores and prizes, review_queue, approve_chore, reject_chore, adjust_points. Use it the moment a message is about chores -- what somebody has pending today or this week, marking one done, not having managed one, being reminded later, points or a prize -- and answer with list_chores, never from memory. Use when a message is about chores or tasks: what is pending, marking one done, or the points and prizes attached to them."
# Named `tasks` until 2026-09-11. "Tasks" is also what HEARTBEAT.md, cron and
# spawned subagents call their work, so the name pointed a model at every kind
# of task but this one; "chores" is what this skill actually holds. The old name
# still resolves -- `SKILL_ALIASES` in agent/skills.py -- because stored
# conversations are full of {"skill": "tasks"} blocks and a model reading them
# writes the old name back, and because a `disabledSkills` entry that says
# "tasks" has to keep switching this off.
# The "tareas" sentence is there because that is the word the family uses, and
# it is also the word for homework and for any to-do: the benchmark's chores
# question is "¿qué tareas tengo pendientes para hoy?".
#
# `always: true`, so the body below is in every prompt -- which is why this
# description no longer restates the whole API. Measured 2026-09-10: `tasks` was
# the worst case in the model benchmark at 2/12, against 8/12 for `geo`, the one
# always-loaded skill. `grocery` carries as good a routing sentence as geo and
# also scored 2/12, so the wording was never the lever -- being in the prompt is.
# What still has to be here: the `Invoke with JSON` prefix (the convention
# AGENTS.md tells the model to follow), the everyday action names and enough
# trigger words to route a message here. Home Assistant's to-do list, which
# four of twelve models reached for instead, is no longer offered to the model
# at all (`disabledTools` in config.json) -- telling it not to use a tool it had
# in hand was never enough, so the warning that stood here went with the tool.
# If this skill is ever demoted to on-demand, the description needs to grow back.
metadata: {"nanobot":{"translatable":true,"always":true}}
---

# Chores, points and prizes

The family's chores, stored in HomeCore. Each member has their own list;
completing one sends it to a parent for review, and approval grants points that
buy prizes.

## Call it first, always

The moment a message touches chores -- "what's left?", "already did it", "I
couldn't", "remind me later", points, a prize, or just a task's name -- run
`list_chores` BEFORE you reply or ask anything. The lists live server-side and
change constantly. Never answer from memory or from the chat history, and never
ask "which chore?" before you have looked.

## How to call it

Write the JSON block as plain text in your reply. The system intercepts and
runs it. Never through exec, curl, read_file or Python you write yourself.

```json
{"skill": "chores", "action": "list_chores"}
{"skill": "chores", "action": "list_chores", "scope": "all", "user": "all"}
{"skill": "chores", "action": "complete_chore", "task_id": 12}
```

Every answer explains itself: `list_chores` returns `scope_note` saying what it
filtered to, and `my_points` returns `penalty_rules`. Read those instead of
guessing.

## Actions

Everyone: `list_chores` `complete_chore` `revert_chore` `excuse_chore`
`excuse_day` `postpone_chore` `my_points` `list_prizes` `redeem_prize`
`list_redemptions`

Parents only (the server answers 403 to anyone else): `add_chore` `edit_chore`
`delete_chore` `add_recurring_chore` `edit_recurring_chore`
`list_recurring_chores` `review_queue` `approve_chore` `reject_chore`
`add_prize` `fulfill_redemption` `cancel_redemption` `adjust_points`

Parameter names and defaults are in the skill's own code -- you do not need
them here. Send only the fields you are changing.

## What to decide before you call

- **Whose.** `list_chores([scope],[user])` -- no `user` means yours, a member's
  name means theirs, `"all"` means the household. A name that does not exist
  comes back as an error, not an empty list. **An empty list without `user`
  means "not yours", NOT that nobody did it**: on 11 Aug 2026 that answered
  "nobody, yesterday" about a chore another member had already done and a parent
  had approved.
- **When.** `scope` is `today` by default, or `week`, or `all`. Anything on a
  past day needs `all` -- `today` and `week` both miss a chore already finished
  yesterday.
- **Finished something.** Match it by title against `list_chores` and
  `complete_chore` it. If nothing matches, say so rather than completing
  another task.
- **Could not do it.** One task is `excuse_chore` with their reason. A whole
  day ("I'm ill", a parent saying somebody is away) is `excuse_day`, which
  needs no `list_chores` first. "Remind me later" is `postpone_chore`.
- **Deleting asks twice.** `delete_chore` without `confirm` does not delete; it
  returns the title so you can ask "are you sure?". Only send `confirm: true`
  after they clearly say yes, never in the same turn. Prefer excusing over
  deleting: it keeps the record.
- **A 403** means only the parents can do that. Say so kindly; never work
  around it.

## Automatic reminders

A message starting `[HomeCore system] Automatic chore reminder` is the reminder
system asking you to nudge. Answer with ONE short warm line about that task and
do NOT invoke this skill for it. Handle whatever they reply next as above.

Answer in the household's language, with points as ⭐.
