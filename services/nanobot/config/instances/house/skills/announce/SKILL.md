---
name: announce
description: "Speak out loud through a room's speaker. Use it for notices that are not the answer to the current turn: a timer that finished, the result of a background task, or telling somebody something in ANOTHER room. Actions: announce(room, message)."
metadata: {"nanobot":{"emoji":"📢","requires":{"bins":["curl"],"env":["VOICE_GATEWAY_URL","VOICE_GATEWAY_TOKEN"]}}}
---

# Announce

Plays a message through a room's speaker. The voice gateway synthesises it and
pushes it to that room's panel over MQTT; the panel plays it by itself.

## When it is used

- **The result of a background task.** A `spawn` that finishes has no way back:
  in a room there is no chat and no conversation screen. If I sent something to
  the background, I announce it when it returns or nobody ever hears about it.
- **A timer or reminder that fired** (from a `cron` task).
- **Telling another room**: "let the kitchen know the delivery arrived".

## When it is NOT used

- **To answer the current turn.** What I return is already heard in the room
  where they spoke to me. Announcing it as well says it twice.
- For sensitive things: there may be other people in the room. Money, health,
  passwords and codes go to the person's phone with `ntfy-send`, never to the
  speaker.

## announce

```bash
curl -s -X POST "$VOICE_GATEWAY_URL/v1/announce" \
  -H "Authorization: Bearer $VOICE_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"room": "kitchen", "text": "The water has boiled."}'
```

`room` is the same name as that room's `Chat ID` — the one that reaches me in
the context when somebody talks to me from there.

Responses:

- **200** with `{"room": …, "audio": {…}}` — it played. Nothing else to do.
- **404** with the list of rooms that do exist: I got the name wrong. The answer
  carries the valid names; I use one of those or say that room has no speaker.
- **401** — the token never reached the container. I say so, I don't retry.

The text goes **as it is spoken**: no markdown, no emoji, no links, short.

## Watch out

- **An announcement plays in a room where there may be nobody.** It is not a
  chat: if nobody is there, the message is lost. For something the person *has*
  to know, it is `ntfy-send` to their phone, not this.
- I don't announce in several rooms "just in case". Once, where it belongs.
