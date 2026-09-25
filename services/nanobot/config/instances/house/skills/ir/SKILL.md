---
name: ir
description: "Control a room's appliances over infrared — the air conditioning, the TV, a fan — from the IR emitter on that room's panel. Also learn new codes by pointing the original remote at the panel. Actions: ir_list(room), ir_send(room, device, command), ir_ac(room, device, state), ir_learn(room, device, command), ir_save(room, device, command, code)."
metadata: {"nanobot":{"emoji":"📺","requires":{"bins":["curl"],"env":["VOICE_GATEWAY_URL","VOICE_GATEWAY_TOKEN"]}}}
---

# IR — the room's remote control

Every room's panel has an infrared LED. Whatever an ordinary remote tells the
air conditioning or the TV, this can tell it too. The appliance is not connected
to anything and never will be: this is literally pointing a LED at it.

**It is always the room I am in** — the turn's `Chat ID` — unless somebody asks
for another one explicitly ("turn on the living room TV" from the kitchen).

## First: what this room knows how to do

```bash
curl -s "$VOICE_GATEWAY_URL/v1/ir/codes?room=living" \
  -H "Authorization: Bearer $VOICE_GATEWAY_TOKEN"
```

Returns that room's devices and the commands each one has saved. I check this
**before** saying something can't be done: the household chose the names and I
don't guess them.

## Sending a command that already exists

```bash
curl -s -X POST "$VOICE_GATEWAY_URL/v1/ir/send" \
  -H "Authorization: Bearer $VOICE_GATEWAY_TOKEN" -H "Content-Type: application/json" \
  -d '{"room": "living", "device": "tv", "command": "power-on"}'
```

- **200 with `"ok": true`** — it went out. I answer short: "done".
- **200 with `"ok": false`** — the panel could not send it. I say so.
- **404** — that device or that command does not exist there. The answer carries
  the ones that do; I use one of those or **offer to learn it** (below).
- **504** — the panel did not answer: it is unplugged or off the network. That
  is not fixed by trying another code.

## The air conditioning is sent by state, not by button

An air conditioning remote does not send "one degree more": it sends *the whole*
state in every burst — power, mode, temperature, fan. That is why the air
conditioning has its own shape, and why **I can set 23 degrees without anybody
ever having recorded 23 degrees**:

```bash
curl -s -X POST "$VOICE_GATEWAY_URL/v1/ir/send" \
  -H "Authorization: Bearer $VOICE_GATEWAY_TOKEN" -H "Content-Type: application/json" \
  -d '{"room": "living", "device": "ac",
       "ac": {"power": true, "mode": "cool", "degrees": 23, "fan": "auto"}}'
```

`mode`: `cool`, `heat`, `dry`, `fan`, `auto`. `fan`: `auto`, `min`, `low`,
`medium`, `high`, `max`. Switching off is `{"power": false}`.

This needs the device to have a brand configured (`ac_protocol`). If it doesn't,
the gateway answers 400 saying so, and then I ask what make the unit is and save
it with `ir_save` (below). If the make is not supported, what is left is the
commands recorded one by one — it still works, only "a little cooler" doesn't.

**The air conditioning has no memory of what I sent it.** If somebody asks to
"turn it up a degree", the previous state is known by me, not by the unit: I
remember it from the turn, or I ask what it is set to. I don't invent the
current temperature.

## Learning a code from the real remote

This is what makes any appliance work, of any make, even one no database knows.

```bash
curl -s -X POST "$VOICE_GATEWAY_URL/v1/ir/learn" \
  -H "Authorization: Bearer $VOICE_GATEWAY_TOKEN" -H "Content-Type: application/json" \
  -d '{"room": "living", "device": "tv", "command": "mute", "timeout_s": 30}'
```

The spoken flow, in this order:

1. **I say so before calling**, because the call waits: *"Point the remote at
   the panel and press the button once. The ring will turn blue."*
2. Only then do I make the `curl`. It takes up to half a minute: somebody is
   looking for the remote.
3. **`"learned": true`** → I confirm with the name: *"Done, I saved it as
   mute."* It is saved, nothing else to confirm — it came from the original
   remote.
4. **`"learned": false`** → nothing arrived. They may have pointed it wrong, the
   remote's batteries may be flat, or that room's panel may have no receiver
   (`detail` says which). I offer to repeat once; if it fails again, I leave it.

**One button at a time.** I don't ask for "now press volume up, now volume
down": each one is a call and a confirmation.

I say the command names in plain words, the way the household would: `power-on`,
`power-off`, `volume-up`, `mute`.

## When there is no remote: look it up, try it, confirm

If the remote is lost, I can look the code up and **try it**. An unsaved code is
sent just the same:

```bash
curl -s -X POST "$VOICE_GATEWAY_URL/v1/ir/send" \
  -H "Authorization: Bearer $VOICE_GATEWAY_TOKEN" -H "Content-Type: application/json" \
  -d '{"room": "living", "protocol": "NEC", "code": "0x20DF10EF", "bits": 32}'
```

The order matters and I don't skip it:

1. **I ask for the make and model** if I don't know them. "A TV" is not enough.
2. I look the code up (`web_search` / `web_fetch`, or whatever I already know
   about that make). Common protocols are `NEC`, `SAMSUNG`, `SONY`, `RC5`,
   `RC6`, `LG`.
3. **I send it and I ask**: *"Did it come on?"* — I am a LED pointed at an
   appliance, I have no way of knowing. I never say "done" for a code I have not
   tested.
4. **If it worked**, only then do I save it with `confirmed: true`.
5. **If it didn't**, I try the next candidate. **Three in a row at most**, and I
   say that I am trying. A TV flickering while somebody is watching something is
   worse than having no remote.

Saving what worked:

```bash
curl -s -X POST "$VOICE_GATEWAY_URL/v1/ir/codes" \
  -H "Authorization: Bearer $VOICE_GATEWAY_TOKEN" -H "Content-Type: application/json" \
  -d '{"room": "living", "device": "tv", "command": "power-on", "brand": "LG",
       "protocol": "NEC", "code": "0x20DF10EF", "bits": 32,
       "source": "lookup", "confirmed": true}'
```

The same endpoint, without `command`, registers the device — that is how an air
conditioner's make is configured:

```bash
curl -s -X POST "$VOICE_GATEWAY_URL/v1/ir/codes" \
  -H "Authorization: Bearer $VOICE_GATEWAY_TOKEN" -H "Content-Type: application/json" \
  -d '{"room": "living", "device": "ac", "brand": "Midea", "ac_protocol": "COOLIX"}'
```

Frequent air conditioning protocols: `COOLIX` (Midea, Kelon and half a dozen
other makes), `DAIKIN`, `MITSUBISHI_AC`, `FUJITSU_AC`, `SAMSUNG_AC`, `LG`,
`GREE`, `PANASONIC_AC`, `HAIER_AC`, `TCL112AC`, `WHIRLPOOL_AC`. If I don't know
which it is, I look it up by make and model and **I test it before saving it**:
a wrong `ac_protocol` makes every command fail silently.

## Watch out

- **I have no feedback.** The appliance does not answer. `"ok": true` means "the
  panel emitted", not "the TV came on". If somebody asks whether it ended up on,
  I say what I know and what I don't.
- **Infrared is line of sight.** If the panel cannot see the appliance, nothing
  happens and it looks exactly like a bad code. Worth saying before trying five
  codes.
- **One command per request.** Nobody asks to "turn the TV on" meaning also turn
  the volume up.
- **This belongs to the room, not to the person.** Anybody who is there can turn
  the air conditioning on, and that is fine: it is the same as reaching for the
  remote. None of this goes through the phone confirmation.
- If the room has sensors, `sensors` says what the temperature actually is —
  better than guessing before changing the air conditioning.
