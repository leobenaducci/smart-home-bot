---
name: self-improve
description: "Invoke with JSON: {\"skill\":\"self-improve\",\"action\":\"...\"}. Ask for a fix to Alfred himself: request_fix(problem, [context]) | publish_fix([id], [deploy]) | list_fix_requests(). Call it AT ONCE, before looking into anything yourself -- the Programmer investigates. Use it when the person says something about how YOU work is broken or wrong and asks for it to be fixed or changed -- a skill that fails or talks to the wrong place, a rule you keep getting wrong, a feature of yours that misbehaves. Not for a device that is off, an answer they just want redone, or their own projects."
# On demand: the description carries the invocation and the API.
metadata: {"nanobot":{"translatable":true}}
---

# Self-improve

A fix to Alfred is made in the Programmer, by its coding agent, with the person
watching -- never by you in this chat. This skill files what the person asked
and starts the Programmer investigating it at once, in a conversation of its
own; the card you get back opens that conversation. It asks the person before
changing anything.

Write the JSON block as plain text in your reply; the system intercepts and runs
it. Never exec, echo, curl, or Python you write.

## request_fix

- `problem`: what the person said is wrong and what they want, in their words
  and their language. Do not soften it or diagnose it.
- `context` (optional): what you have that they did not say -- which skill or
  tool, the exact error it returned, what you tried. Mark what you **checked**
  apart from what you **could not check**: you cannot read a skill's own
  environment or code, so "the variable is not set" is a guess unless you saw
  it. No guess about the cause, no secrets, no tokens.

```json
{"skill": "self-improve", "action": "request_fix", "problem": "The lights skill points to the wrong server; fix it.", "context": "skill lights, flash_light: connection refused at its default address."}
```

It answers `{"ok": true, "id": 7, "investigating": true, "card": ":::goto ... :::"}`.
Reply with one short line -- the Programmer is already looking into it, and
will ask before changing anything -- then the `card` **exactly as returned**, on
its own lines. Do not rewrite it or add a link of your own. With
`"investigating": false` it could not start: say the card opens the Programmer
with the request written out, for them to send.

`"ok": false` with an error means there is nowhere to hand it -- usually the
Programmer is not on for this person. Say that, in one line, and stop.

## publish_fix

When the person asks to **publish** (or publish and deploy) a fix that was
already made -- "publicá el último fix", "publish the lights fix and deploy it"
-- this is the whole job. It tells that request's own Programmer conversation
to publish it (and deploy, with `deploy: true`), as the person's word, and the
Programmer does it there with its checks. Never file a new request for this,
and never try it another way.

- `id` (optional): the request's number when the person names one; left out,
  their latest open request.
- `deploy` (optional): `true` when they asked to deploy as well.

```json
{"skill": "self-improve", "action": "publish_fix", "deploy": true}
{"skill": "self-improve", "action": "publish_fix", "id": 6}
```

It answers `{"ok": true, "id": 6, "card": ":::goto ... :::"}`: one short line --
the Programmer is publishing it and will say how it went -- then the `card`
exactly as returned. `"ok": false` says why; say that in one line.

## list_fix_requests

```json
{"skill": "self-improve", "action": "list_fix_requests"}
```

The person's last requests, newest first, with their status.

## Rules

- **Call it first.** Do not read the skill's code, its config or the logs
  before filing: you cannot see most of it, and the Programmer can -- it
  checks everything against the running system. What you already know from
  the conversation is enough context.

- It is one call, and it is the whole job -- in the chat or in the background
  alike. The person pressing send in the Programmer is the design, not a gap:
  do not try the fix another way first, and do not hedge about having filed it.
- One request per problem. Two problems, two requests.
- Never try to fix it yourself here, and never say it is fixed: filing it is
  all this does.
