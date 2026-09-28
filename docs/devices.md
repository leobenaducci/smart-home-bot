# Devices: naming a phone or tablet, and controlling it from Alfred

Every phone and tablet with the Alfred app registers itself with the portal
under a name somebody gave it, so a parent can say "bajá el volumen de la
tablet de Juana" or "abrí Disney+ en la tablet" from their own phone.

## Setting a device up

In the app, on that device: menu → **📱 This device**.

- **Name**: what the household calls it ("Tablet de Juana"). Empty, it is what
  Android calls the device. Alfred also finds a device by words from its
  owner and its kind, so "la tablet de Juana" works whatever it is named.
- **Remote control**: off until switched on here. Only on the device itself,
  never from the portal or the admin page. A device that did not opt in
  refuses every command.
- Turning it on asks for **Display over other apps**. Android lets one app
  open another from the background only with that permission. Without it, an
  app Alfred is asked to open arrives as a notification to tap, and Alfred says
  so rather than claiming it opened.

A device belongs to whoever is signed in on it, and moves with a new sign-in.
The id is random and made by the app, not the hardware's.

## Who may do what

The same rule as ringing a phone: anybody may command their own devices, and a
parent (the portal's admins) may command anyone's. Children see only their own
devices.

| Action | What happens on the device |
|---|---|
| `volume` | media volume to a level (0-100) or a step (up, down, mute, unmute), shown on screen |
| `open_app` | the app opens, or a notification to open it (see above) |
| `ring` / `ring_stop` | that one device rings through silent mode, as "find my phone" does for all of a person's |

An app is matched against the list the device reported: "disney" is Disney+,
and "YouTube" is not YouTube Kids. An app the device does not have is an error
before anything is sent.

## How a command travels

1. Alfred's `devices` skill posts to `/devices/api/command`.
2. The portal checks the permission, the device's remote-control switch and
   the app, then pushes `device_cmd` on the owner's control channel
   ([proxy-notifications.md](proxy-notifications.md)).
3. The named device acts and answers `/devices/api/ack`. The portal waits up
   to eight seconds for that answer. With no answer, Alfred says the command
   went out but was not confirmed.

Every device signed in as the owner receives the push; the others ignore it.
An app older than this feature shows it as a raw notification. That is the
same transition the family chat went through, and it ends when that device
updates.

## Where things are

- Portal: the devices section and `app_devices` in `devices.db`
  (`services/home-core/local/app.py`); `test_devices.py`.
- App: `services/proxy/android/.../device/` (`DeviceIdentity`,
  `DeviceCommands`); the dialog in `MainActivity.showDeviceSettings`.
- Assistant: `services/nanobot/nanobot/skills/devices/`.
