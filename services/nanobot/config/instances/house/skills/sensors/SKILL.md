---
name: sensors
description: "Know what a room is actually like: temperature, humidity, light and whether anybody is there. The room's own panel measures them. Use it before assuming it is cold or hot, or before changing the air conditioning. Actions: sensors(room)."
metadata: {"nanobot":{"emoji":"🌡️","requires":{"bins":["curl"],"env":["VOICE_GATEWAY_URL","VOICE_GATEWAY_TOKEN"]}}}
---

# Room sensors

The panel measures the room it is in: temperature, humidity, light and motion.
Not every panel carries every sensor — some carry none.

```bash
curl -s "$VOICE_GATEWAY_URL/v1/sensors?room=living" \
  -H "Authorization: Bearer $VOICE_GATEWAY_TOKEN"
```

Without `?room=` it returns every room at once, which is what answers "where is
it coldest?".

Per room:

| Field | What it is |
|---|---|
| `sensors` | `false` = that panel has no sensors, or never published |
| `celsius` | temperature |
| `humidity` | relative humidity, as a percentage |
| `lux` | ambient light — 0 is dark, ~100 a room with the light on, >1000 is daylight |
| `motion` | whether there is movement **right now** |
| `seconds_since_motion` | how long ago movement was last seen |

## How I say it

**Rounded, and in one sentence.** "Twenty-one and a half" or "twenty-two
degrees", not "21.7°C, humidity 48.3%". Nobody asked for a report.

Humidity and light only if they are relevant or if they were asked for. The
temperature on its own answers almost everything.

## What I actually use it for

- **Before touching the air conditioning.** If they tell me "it's hot", I look.
  Setting it to 18 when the room is at 21 is the difference between helping and
  somebody getting up to switch it off. See `ir`.
- **To answer without inventing.** "Is it cold in the small bedroom?" has a
  measured answer. I don't estimate it from the time of day or the season.
- **To know whether it is worth speaking.** An `announce` in an empty room is
  lost. If `seconds_since_motion` is hours, there is probably nobody there, and
  something the person *has* to know goes to their phone.

## Watch out

- **`motion` is "something moved", never who.** The sensor does not tell people
  apart and neither do I: it is a property of the room. I don't say "so-and-so
  is in their room" — I say "there is movement" if it genuinely needs saying,
  and mostly it doesn't.
- **It is not a log.** I only have the latest value, not the one from midday. If
  they ask about earlier, I say so instead of inventing it.
- **An old value is an unplugged panel.** If the numbers don't match what the
  person feels, the most likely thing is that panel being unplugged. Worth
  saying.
- **`sensors: false` is not zero degrees.** It is a room with no sensors. The
  difference matters: one says "I don't know", the other says something false.
