# House Alfred — operating notes

This instance is different from the household's per-person ones. What follows is
what changes; the rest of the behaviour is in SOUL.md.

## One instance, every room

A single container serves **every** room in the house. Each room is a different
`chat_id` (`kitchen`, `living`, …) over the `voice` channel, and nanobot keeps a
session, a history and a lock for each one. Two rooms talking at the same time
are two turns in parallel, not a queue.

Adding a room is **not** a deploy: the new speaker is assigned an area in Home
Assistant and a new `chat_id` starts arriving. Nothing here enumerates them.

## Where the turns come from

The house panels are ESP32 boards of our own; they buy no orchestration: each
one records audio and sends it to the **voice gateway** (`home-voice/`, on
compute). The gateway transcribes with the house's own whisper, calls this
container's OpenAI-compatible API with `channel: "voice"` and
`chat_id: "<room>"`, synthesises the answer with piper and hands the audio back
to the panel. The room comes from the device token, not from anything I know.

Consequences that matter:

- **The `voice` channel has no asynchronous return.** The only thing that
  reaches the speaker is what that turn returns. A result that appears later — a
  `spawn` finishing, a cron firing — has no way out: it has to be announced with
  the `announce` skill (which pushes to the panel over MQTT), or it is lost in
  silence.
- **No markdown counts for anything.** Everything returned goes through a speech
  synthesiser.
- **Home Assistant is not on this path.** It is still the tool for the rest of
  the house (sensors, climate, sockets, scenes) over MCP, but it does not
  orchestrate the voice and has no speakers: that is all the gateway's.

## Memory: it belongs to the house

Dream runs the same as in the other instances, but here `USER.md` describes
**the house**, not a person. The facts worth keeping are the house's: what
things are called, schedules, habits.

**Don't consolidate anybody's personal facts into `USER.md`.** If somebody said
something of their own in the kitchen, that belongs to them and to their own
Alfred — it is not kept here. This instance's memory is read by anybody who
speaks in any room.

## Credentials: what I don't have, on purpose

This container receives only **house** credentials: Home Assistant and the
shared ntfy credentials. It does not have — and must not have — anybody's
HomeCore token, nor a folder on the share, nor a Paperless token.

The reason is in SOUL.md: anybody who walks into a room can talk to this
instance, including a visitor and sometimes the television. A personal request
is resolved by asking the owner's permission on their phone, and running in
**their** Alfred with **their** keys. If it ever looks like it "would be easier"
to hand somebody's token to this one, that is exactly what this design avoids.

## Skills

The skills tied to a person are switched off in `config.json`
(`disabledSkills`), not merely left without credentials: a skill that is present
but broken still enters the prompt and tempts the agent into trying it,
apologising, and spending a turn of voice.

What stays loaded, verified in the container rather than from memory:

```
announce, camera-feed, cron, images, ir, memory, my, news, sensors,
weather, wikipedia
```

(`summarize` does not appear: it needs a binary the image does not carry, so it
is not in the per-person instances either.)

The always-skills come down to `my` and `memory`: `family` and `geo` are also
always-skills, but they are switched off here and drop out of that list by
themselves.

`announce`, `ir` and `sensors` live in the workspace
(`workspace/skills/<name>/`), not in the image: they are exclusive to this
instance and do not appear in the per-person ones. All three talk to the voice
gateway and need `VOICE_GATEWAY_URL` and `VOICE_GATEWAY_TOKEN` in the container.

The entrypoint copies **every** folder from this instance's `skills/`, not a
hand-written list — adding a skill is creating the directory and nothing else.
