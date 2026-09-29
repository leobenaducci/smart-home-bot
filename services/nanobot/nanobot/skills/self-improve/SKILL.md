---
name: self-improve
description: "Invoke with JSON: {\"skill\":\"self-improve\",\"action\":\"...\"}. Ask for a fix to Alfred himself: request_fix(problem, [context]) | list_fix_requests(). Use it when the person says something about how YOU work is broken or wrong and asks for it to be fixed or changed -- a skill that fails or talks to the wrong place, a rule you keep getting wrong, a feature of yours that misbehaves. Not for a device that is off, an answer they just want redone, or their own projects."
# On demand: the description carries the invocation and the API.
metadata: {"nanobot":{"translatable":true}}
---

# Self-improve

A fix to Alfred is made in the Programmer, by its coding agent, with the person
watching -- never by you in this chat, and never in the background. This skill
files what the person asked and gives back a card that opens the Programmer
with the fix already written out, for them to read and send.

Write the JSON block as plain text in your reply; the system intercepts and runs
it. Never exec, echo, curl, or Python you write.

## request_fix

- `problem`: what the person said is wrong and what they want, in their words
  and their language. Do not soften it or diagnose it.
- `context` (optional): the facts you have that they did not say -- which skill
  or tool, the exact error it returned, what you tried. Facts only: no guess
  about the cause, no secrets, no tokens.

```json
{"skill": "self-improve", "action": "request_fix", "problem": "The lights skill points to the wrong server; fix it.", "context": "skill lights, flash_light: connection refused at its default address."}
```

It answers `{"ok": true, "id": 7, "card": ":::goto ... :::"}`. Reply with one
short line saying the fix is ready to hand to the Programmer, then the `card`
**exactly as returned**, on its own lines. Do not rewrite it or add a link of
your own.

`"ok": false` with an error means there is nowhere to hand it -- usually the
Programmer is not on for this person. Say that, in one line, and stop.

## list_fix_requests

```json
{"skill": "self-improve", "action": "list_fix_requests"}
```

The person's last requests, newest first, with their status.

## Rules

- One request per problem. Two problems, two requests.
- Never try to fix it yourself here, and never say it is fixed: filing it is
  all this does.
