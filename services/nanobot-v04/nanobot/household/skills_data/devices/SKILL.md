---
name: devices
description: "Invoke with JSON: {\"skill\":\"devices\",\"action\":\"...\"}. The household's phones and tablets that run the Alfred app, by the name each was given (\"the kids' tablet\"): set_volume(device, level 0-100 | step up/down/mute/unmute) | open_app(device, app) | ring_device(device, [seconds]) | stop_ring_device(device) | list_devices() | list_apps(device). Use it when a message asks to turn a phone or tablet up or down, mute it, open an app on it (Disney+, YouTube, Netflix…) or make one particular device ring. Not for TVs, speakers or media players -- those are Home Assistant."
# On demand, not `always`: the description above carries the invocation
# convention and the whole API, because that is all a model sees until it
# reads this file.
metadata: {"nanobot":{"translatable":true}}
---

# Devices

Every phone and tablet with the Alfred app registers under the name somebody
gave it in the app (menu → **This device**). `device` takes that name, or
words from it and from its owner: "la tablet de Tomi" finds Tomi's tablet
whatever it is called. `app` takes the app's name as it appears on the device.

Write the JSON block as plain text in your reply; the system intercepts and runs
it. Never exec, curl, or Python you write.

```json
{"skill": "devices", "action": "set_volume", "device": "tablet de Tomi", "step": "down"}
{"skill": "devices", "action": "set_volume", "device": "tablet de Tomi", "level": 20}
{"skill": "devices", "action": "open_app", "device": "tablet de Tomi", "app": "Disney+"}
{"skill": "devices", "action": "ring_device", "device": "tablet de Tomi"}
{"skill": "devices", "action": "list_devices"}
{"skill": "devices", "action": "list_apps", "device": "tablet de Tomi"}
```

What the answer means -- say it plainly, and never claim more than it says:

- `confirmed: true` and `result.ok: true`: done. For the volume, `result.level`
  is where it ended up.
- `result.detail: "notified"` on `open_app`: the app is **not** open. It is a
  notification on the device to tap, because "Display over other apps" is not
  allowed for Alfred there. Say so, and that allowing it (the app asks when
  remote control is turned on) makes the next one open by itself.
- `confirmed: false`: the command went out but the device did not answer --
  off, asleep without data, or the app closed. Say it may not have arrived.
- `error` with `remote_off` in the text: remote control is off on that
  device. It can only be turned on **on the device itself**; tell them where.
- `candidates`: the name matched several devices or none. Ask which one,
  naming them; never pick one yourself.

Only a parent can control somebody else's device; anybody can control their
own. A "no" from the system on that is final -- do not try another way.
