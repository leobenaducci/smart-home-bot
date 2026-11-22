---
name: home-assistant
description: "Invoke with JSON: {\"skill\":\"home-assistant\",\"action\":\"...\"}. NOT for live state — 'is the door open', 'what is the temperature' are GetLiveContext, and switching lights is the lights skill (or HassTurnOn/HassLightSet for Home Assistant's copies). Read and edit Home Assistant *configuration* — automations, devices, entities, and what lights are called and where they are in HA. Reads: search_ha(search) | automations_for(thing) — what is wired to a switch or device | list_automations | get_automation(automation) | get_entity(entity_id) | describe_device(device) | list_ha_lights — every HA light with its entity_id, device, area | list_automation_backups([automation]) | new_devices([hours]) — what just paired | device_triggers(device) | listen_button([device], [seconds]) — what a button sends when pressed. Writes: set_automation(automation, config, [confirmed], [allow_removals]) | create_automation(config, [confirmed]) | delete_automation(automation, [confirmed]) | restore_automation(backup, [confirmed]) | enable_automation(automation) | disable_automation(automation) | reload_automations | rename_entity(entity, name) | rename_device(device, name) | set_area(target, area) | create_area(name) | pair_zigbee([seconds]) | identify_device(device) — make a Zigbee device blink. Use it whenever a message is about how something in the house is *wired* — why a button does not work, what an automation triggers on, changing what a switch does. Turning things on and off is NOT this skill: lights are the lights skill, and other devices are the Home Assistant tools."
metadata: {"nanobot":{"translatable":true,"requires":{"env":["HOMEASSISTANT_TOKEN","HOMEASSISTANT_URL"]}}}
---

# Home Assistant configuration

The Home Assistant tools you already hold (`HassTurnOn`, `HassLightSet`,
`GetLiveContext`, …) come from Home Assistant's own Assist integration, and
Assist exposes **device control**. It has no idea what an automation triggers
on. This skill is the other half: what is wired to what, and changing it.

To use it, write the JSON block as text in your answer — the system intercepts
it and runs it. Don't use exec/curl and don't write the Python. Earlier attempts
at this question hand-wrote seventeen numbered probe scripts, guessed at
websocket commands, and spelled a hostname into a file. That is what this exists
to stop.

**Start here, not with the Home Assistant tools.** `GetLiveContext` and the
other `Hass*` tools answer "what is the state of things right now"; they cannot
see an automation's triggers at all, so on a wiring question they return
something plausible and useless — a switch's on/off state when what was asked is
what the switch is wired to. Reaching for them first and then trying to change
course mid-turn is how one of these questions burned eighty-nine tool calls
without answering. If the question is about *wiring*, the first thing you emit
is this skill's JSON block.

**And the converse: a question about *state* is not for this skill.** "Is the
door open", "what is the temperature", "is the alarm armed" — those are
`GetLiveContext` and the `Hass*` tools, which you already hold and which answer
in one call. **Anything about a light — which one is on, what colour, find it,
turn it off — belongs to the `lights` skill**, which owns them and says so.
The exception is **Home Assistant's own record of a light** — what HA calls it,
which area it is in, which device it is: that is configuration, and it is this
skill (see *Lights in Home Assistant* below). This skill cannot see live state at
all: its reads are `search_ha`, `list_automations`, `get_automation`,
`describe_device` — configuration, not what is happening right now.

Measured on 2026-09-20, all from one question ("find the blue light in
Paula's room"): a turn that reached for this skill instead looped twenty-five
times and answered nothing; a second hand-wrote Python and stalled; a third
wrote a block that resolved to no call. The same question asked so that
`GetLiveContext` ran answered correctly in a single tool call. Reaching here
first is not a slower route to the answer — it is a different question.

**If you are not sure which kind of question it is**, ask yourself whether the
answer changes when somebody flips a switch. If it does, it is state: use
`GetLiveContext`. If it does not — what a button is wired to, what an
automation triggers on — it is configuration, and it is this skill.

**And `HassTurnOn` / `HassTurnOff` are not a way to find something out.** Asked
which automations used a switch, one run called `HassTurnOn` on it, then
`HassTurnOff`, then `HassTurnOff` again — as investigation. That device happened
to be a battery remote, so the commands went nowhere; the same reflex on a relay
switches something in the house to answer a question nobody asked it to act on.
Reading is what this skill is for. Never operate a device to learn about it
**unless the person asked you to** — "flash them so we know which is which" is
a request, and then switching a light is the answer, not an investigation.

## Finding the thing being asked about

Names people use ("the switch by Nico's bed") are not entity ids. Start here:

```json
{"skill": "home-assistant", "action": "search_ha", "search": "cama nico"}
```

Returns matching entities, devices and automations.

**The switch is almost never called what the automation is called.** The device
here is `Switch Cama Nico`; the automation that uses it is `Boton Cama Nico`.
Asking for the switch by name as an automation finds nothing, and "nothing"
reads like "nothing is wired to it", which is the wrong answer. So when somebody
names a button or a device, ask what mentions it:

```json
{"skill": "home-assistant", "action": "automations_for", "thing": "Switch Cama Nico"}
```

It matches on device id, on the entity ids of everything on that device, and on
the radio address a `zha_event` trigger names — which is the only thing a
battery remote appears as anywhere.

Then look at one:

```json
{"skill": "home-assistant", "action": "list_automations"}
{"skill": "home-assistant", "action": "get_automation", "automation": "Boton Cama Nico"}
{"skill": "home-assistant", "action": "describe_device", "device": "Switch Cama Nico"}
```

`automation` accepts the alias, the `automation.*` entity id, or the numeric id.

## Reading an automation: the `_resolved` block is the point

Automations refer to devices and entities by opaque 32-character ids.
`get_automation` hangs a `_resolved` note next to every one of them, so a
trigger reads as the thing it actually points at:

```json
{ "trigger": "device", "domain": "button", "type": "pressed",
  "entity_id": "03e5fb77fda6178a431d98b031655750",
  "_resolved": { "entity_id": { "entity_id": "button.…_identify_2",
                                "name": "Identify",
                                "entity_category": "diagnostic" } } }
```

That is a trigger on ZHA's *Identify* button — a diagnostic entity that makes
the device blink. No physical switch can press it. Read the `_resolved` block
before saying an automation is fine: a wrong id and a right one look identical
without it.

Things worth checking when somebody says a button misbehaves:

- **A trigger on a `diagnostic` entity** (Identify, Battery, RSSI) is almost
  always a mistake — those are not the buttons on the wall.
- **Battery remotes** (Tuya TS004F and friends) emit `zha_event`, not entity
  state changes. If there is no `zha_event` trigger, the physical press is not
  wired to anything.
- **`light.toggle` carrying `brightness_pct`** applies that brightness every
  time it turns the light on, and ignores it turning off. The lamp can then only
  ever come on at that one level.
- A `disabled_by` in a `_resolved` block means the entity is switched off, so
  the trigger is dead whatever it names.

## Changing one

`set_automation` **replaces** the whole automation — the Home Assistant config
API has no patch. So the shape is always: read it, change what you mean to
change, send the whole object back.

It refuses a config that drops a top-level key the current one has, because a
config rebuilt from memory that quietly lost `mode` or half its `actions` writes
cleanly and only fails later, as "the button stopped working".

It also writes nothing on the first call:

```json
{"skill": "home-assistant", "action": "set_automation",
 "automation": "Boton Cama Nico",
 "config": { "id": "…", "alias": "…", "triggers": [ … ], "conditions": [],
             "actions": [ … ], "mode": "single" }}
```

comes back as `{"pending": true, "diff": "…"}`. **Show that diff to the person
and get a yes.** Then send it again with `"confirmed": true`. That call backs
the old config up first and tells you where:

```json
{"ok": true, "backup": "…/ha-backups/1780842228464-20260831-190412.json", "diff": "…"}
```

If it turns out wrong, `restore_automation(backup, confirmed=true)` puts the old
one back. `list_automation_backups` finds them again.

## Lights in Home Assistant

The house's bulbs exist twice: in the `lights` skill (the plugin that drives
them locally, and the one to use to switch them) and in Home Assistant, which
reaches the same bulbs through SmartThings, plus Zigbee relays only HA can
switch. HA's names and areas are what automations, dashboards and Assist use,
so they are worth keeping right.

```json
{"skill": "home-assistant", "action": "list_ha_lights"}
```

Every `light.*` entity with its `entity_id`, the name HA shows, its device, area,
state and integration — including the ones Assist is not allowed to see.
`GetLiveContext` gives names and state but no entity ids, and renaming needs one.

```json
{"skill": "home-assistant", "action": "rename_entity", "entity": "light.living_paula_light_lampara_living", "name": "Luz Paula"}
{"skill": "home-assistant", "action": "rename_device", "device": "Paula Light", "name": "Lámpara Paula"}
{"skill": "home-assistant", "action": "set_area", "target": "light.mora", "area": "Dormitorio principal"}
{"skill": "home-assistant", "action": "set_area", "target": "Paula Light", "area": "Dormitorio Paula"}
```

- `rename_entity` changes the name the light goes by — in Assist, `GetLiveContext`
  and the dashboards. That is almost always the one meant.
- `rename_device` changes the device's name, which is what the device pages and
  `describe_device` show. **`""` does not undo a rename**: it drops the name a
  person set and goes back to the integration's (`Tomi Light` becomes `Tomi`). To
  undo, rename it back to the `was` value the call returned.
- `set_area` takes an entity id or a device name. Moving a device moves the
  entities that were in its old room with it; one placed in a different room on
  purpose (`kept_their_own_area`) stays, and you say so. A name HA does not have
  comes back with the list it does — `create_area` only when the person asked
  for a new room.
- Every write returns `was` and `now`. Report both, so a wrong one can be put
  back.

**To tell which lamp is which**, when the person asks you to: a WiZ bulb is
blinked with the `lights` skill's `flash_light` — Home Assistant's copy of it
goes through the cloud and its on/off state there is often stale, so do not
switch it from here and trust what HA reports. `find_in_ha` in the `lights`
skill says which HA light a local bulb is. A Zigbee device is blinked with
`identify_device`. Ask which lamp blinked, then name it here (`rename_entity`)
and in the `lights` skill (`rename_light`) so both say the same thing — the
same name in both is also how the two are matched from then on.

**Names are not wiring.** Renaming an entity does not change its `entity_id`,
so automations keep working. It is still worth an `automations_for` on a light
before telling someone it is "set up right" — the name is what they see, the
automation is what the button does.

## Setting up a new device

The order that works, for a Zigbee button, bulb or relay:

1. **Pair it.** `pair_zigbee` opens the network for two minutes; tell the person
   to put the device in pairing mode now (most: hold the button ~5 s until it
   blinks).
2. **See what arrived.** `new_devices` lists what HA added recently. A fresh
   device is named after its model (`_TZ3000_gdsvhfao TS0001`) and has no area.
   If two arrived, `identify_device` makes one blink — ask which it was.
3. **Name and place it.** `rename_device` with the name the person gives, then
   `set_area`. The areas are the ones HA already has; `create_area` only when
   the person names a room HA does not have yet, and say you created it.
4. **Wire it** (buttons) — below.

```json
{"skill": "home-assistant", "action": "pair_zigbee"}
{"skill": "home-assistant", "action": "new_devices", "hours": 2}
{"skill": "home-assistant", "action": "identify_device", "device": "_TZ3000_gdsvhfao TS0001"}
{"skill": "home-assistant", "action": "rename_device", "device": "_TZ3000_gdsvhfao TS0001", "name": "Boton Cocina"}
{"skill": "home-assistant", "action": "set_area", "target": "Boton Cocina", "area": "Cocina"}
```

A new **WiZ** bulb does not pair here: the `lights` skill finds it on the
network by itself (`list_lights`, named by its MAC), and HA only gets a copy
once it is added to the WiZ/SmartThings app. Name it and identify it with the
`lights` skill (`flash_light`, `rename_light`); `find_in_ha` and `room_from_ha`
there tie it to its HA copy.

## Wiring a button

**Never write a button's trigger from memory.** Find out what it sends:

```json
{"skill": "home-assistant", "action": "device_triggers", "device": "Boton Cocina"}
{"skill": "home-assistant", "action": "listen_button", "device": "Boton Cocina", "seconds": 30}
```

- `device_triggers` gives the presses the device *declares*
  (`remote_button_short_press` / `turn_on`, …), ready to copy whole. Many Tuya
  buttons declare none — their `presses` list is empty, and the one
  `button: pressed` is the Identify button, which no finger can press.
- `listen_button` is the reliable one: ask the person to press each button
  (short, double, long) **before** you call it, and it returns what HA actually
  received, each with a `trigger` ready to use. Nothing arrived means a battery
  button that was asleep — ask again and listen again.

Then write the automation, which — like `set_automation` — writes nothing the
first time:

```json
{"skill": "home-assistant", "action": "create_automation", "config": {
  "alias": "Boton Cocina",
  "triggers": [{"trigger": "event", "event_type": "zha_event",
                "event_data": {"device_ieee": "a4:c1:38:…", "command": "toggle"}}],
  "actions": [{"action": "light.toggle", "target": {"entity_id": "light.cocina"}}]}}
```

comes back `pending` with the automation as it would be written. Show it, get a
yes, send the same config with `"confirmed": true`. One automation per press
(`Boton Cocina (doble)`), named after the button, so `automations_for` finds
them. The light's `entity_id` comes from `list_ha_lights`, never from memory.
`delete_automation` removes one (also two calls) and keeps a backup that
`restore_automation` puts back.

## Rules

- Never write an automation without showing the diff first. The two-call shape
  is the confirmation, not a formality: skipping to `confirmed: true` on a
  config you never diffed is how a working automation gets replaced by a
  plausible one.
- Never invent an entity id or a device id. Look it up.
- Drop the `_resolved` blocks before sending a config back — they are notes for
  you, not part of the automation.
- Say what is wrong in the person's own language, and name the entity so they
  can find it in the UI.
