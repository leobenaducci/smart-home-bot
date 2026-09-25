---
name: geo
# `always: true`, so the body below is in every prompt — which is why this
# description is short. It used to restate the whole API, ~680 tokens of it,
# sitting in the same prompt as the body that says it all again. What still has
# to be here: the `Invoke with JSON` prefix (the convention AGENTS.md tells the
# model to follow) and enough trigger words to route a message here. The
# interceptor only reads the name and the `SKILL.md` path off this line.
# If this skill is ever demoted to on-demand, this needs to grow back.
description: "Invoke with JSON: {\"skill\":\"geo\",\"action\":\"...\"}. Where the household is, saved places, reminders for arriving at or leaving a place, sharing my location, and ringing a lost phone. Use it as soon as a message asks where somebody is, talks about arriving at, leaving or saving a place, or asks to make a phone ring. Use when a message is about where somebody is, a saved place, or arriving at or leaving one."
metadata: {"nanobot":{"translatable":true,"always":true}}
---

# Location, places and reminders

Saved places, where the household is, and reminders that fire on arriving at or
leaving a place. It lives in HomeCore; the phone does the geofencing. **This is
the source for location, not Home Assistant.**

Write the JSON block as plain text in your reply; the system intercepts and runs
it. Never exec, curl, or Python you write. Answer in the person's language.

## The rule that decides everything

**"Where is X?" is `where_is`, always, first.** Never `locate` as a first
answer: `locate` and `track` wake the other person's GPS and drain their
battery. Offering real time is fine; running it before they accept is not. Go
real-time only on an explicit ask -- "right now", "exactly", "the address",
"follow her" -- or after they accept your offer.

Locating or tracking **another** person is **admins only**; everyone else gets a
403. Do not offer it to somebody who cannot use it. On a 403, explain kindly and
offer `where_is`, which anyone can use.

## Where is a member of the household?

```json
{"skill": "geo", "action": "where_is", "person": "user4"}
{"skill": "geo", "action": "where_is"}
```

Returns `locations` with `person`, `place`, `status` and `ago_minutes`; without
`person`, everybody's. **APPROXIMATE by design**: it knows saved places from
phone enter/exit events, not live GPS. **Always say how old it is** -- it is the
last place we heard about, not where they are now.

| Result | Means | Do |
|---|---|---|
| `status:"in"` | arrived and hasn't left; old is good | "at school for 20 min". **Don't offer `locate`.** |
| `status:"left"` | only where they left from | recent is a clue; otherwise say you don't know and offer to locate |
| `known:false` | no data | say so, invent nothing, offer to locate |

## Places

`radius` is optional, metres, default 150. For "here" use the coordinates in the
`[User's current location: lat,lon]` line of the message. **Don't invent
coordinates**: with no such line and no address you do not know where "here"
is -- ask.

```json
{"skill": "geo", "action": "save_place", "name": "home", "lat": -33.4489, "lng": -70.6693}
{"skill": "geo", "action": "list_places"}
{"skill": "geo", "action": "delete_place", "place_id": 3}
```

## Location reminders

`direction` is `enter` (default) or `exit`; `place` is a saved name or id; they
fire once unless `"recurring": true`. The place must exist -- if `add_reminder`
cannot find it, offer `save_place` first. Watching **another** person arrive is
admins only: `target_user` is who arrives, `notify_user` who gets told.

```json
{"skill": "geo", "action": "add_reminder", "text": "buy bread", "place": "home"}
{"skill": "geo", "action": "add_reminder", "text": "tell me she got there", "place": "school", "target_user": "user4", "notify_user": "user2"}
{"skill": "geo", "action": "list_reminders"}
{"skill": "geo", "action": "cancel_reminder", "reminder_id": 5}
```

## Real GPS, only when asked

- `get_location` -- last saved fix, does not wake the phone. Exists only if
  somebody ran `locate`/`track` before; otherwise `where_is` is all there is.
- `locate` -- position now: wakes the phone, ~12s.
- `track` -- follow somebody for `minutes`; stops itself. **Updates arrive on
  their own.** Say you will keep them posted and **never poll in a loop**. If
  they ask again meanwhile, answer now with `where_is`.
- `share_location` -- **the other direction**: sharing MY location with
  somebody. It is **not admin-only**, it is my own phone. For "let X see where I
  am" use this, **never `track`**, which points at someone else's phone.
- `ring_phone` -- make a phone ring even **on silent**, for "I can't find my
  phone". `stop_ring` quiets it.

```json
{"skill": "geo", "action": "get_location", "target_user": "user4"}
{"skill": "geo", "action": "locate", "target_user": "user4"}
{"skill": "geo", "action": "track", "target_user": "user4", "minutes": 45}
{"skill": "geo", "action": "stop_track", "target_user": "user4"}
{"skill": "geo", "action": "track_status", "target_user": "user4"}
{"skill": "geo", "action": "share_location", "with_user": "user2", "minutes": 60}
{"skill": "geo", "action": "stop_sharing"}
{"skill": "geo", "action": "ring_phone", "target_user": "user1"}
{"skill": "geo", "action": "stop_ring", "target_user": "user1"}
```

A GPS answer carries `map_note` when there is a `map` field, and `age_note` when
the fix is old. **Paste the `map` markdown verbatim**, on its own line, after
saying where they are and since when; never build the URL yourself, and if the
field is absent do not invent one. **`where_is` never brings a map** -- it knows
only a place's name, so a map there would show the school's centre as if it were
the person's position. The other phone's GPS runs only while a request is
active; adults are told, children are not.

## System messages

A `[HomeCore system]` message about somebody arriving, leaving, or a tracking
run already carries the fact: **do NOT invoke this skill for it.** Answer short
and warm, one line per update, without repeating yourself.
