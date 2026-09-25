# The house

This file describes **the house**, not a person. The other Alfred instances
have their owner's profile here; this one has no owner, so what goes here is
whatever helps in serving anybody who speaks in a room.

## Basics

- **Location**: your city
- **Time zone**: your `site.timezone`
- **Language**: English (en)

## Who lives here

| Name | Role | ntfy topic |
|------|------|------------|
|      |      |            |

Fill this table in with whoever lives in the house. The ntfy topic is each
person's personal notification channel (normally their name); leave it empty for
anybody without a phone.

Mark in the Role column which of them are administrators of the house.

## The rooms

The `Chat ID` of each conversation is the room's identifier. The ones that exist
today:

| Chat ID | Room |
|---------|------|
| `kitchen` | Kitchen |
| `living` | Living room |

When a panel is added a new `Chat ID` appears without anybody touching this
file. If somebody talks to me from a room that is not in this table, I serve
them anyway: the place reaches me in the context. To announce there I use that
same name as `room` in the `announce` skill, and if I get it wrong the gateway
answers me with the list of rooms that do exist.

## The house from the inside

- **Home Assistant**: the rest of the house — sensors, climate, sockets,
  scenes, automations.
- **Cameras**: with the `camera-feed` skill. In a room they are for looking, not
  for showing: I describe what is there, I don't send a photo nobody can see.
- **Kitchen panel**: there is an e-ink screen on the kitchen wall showing the
  day's menu. I don't control it.

## Things the house already knows

<!-- Written below over time. These are facts about the house — schedules,
     habits, what things are called — not about any one person. -->
