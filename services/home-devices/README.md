# home-devices — Devices / Dispositivos

One list of every device in the house: what each one is called, which room it
is in, where it sits on the floor plan, and which backend reaches it. The
assistant reaches devices through this list rather than through each
integration's own copy, so one bulb is one name in one place.

The portal carries the page at `/home-devices/` (a `mounts:` entry in the
manifest), signed in as the member.

## Discovery

Every backend is scanned continuously and merged into the registry
(`home_devices/registry.py`):

- **New** candidates land in the page's *New devices* inbox with status `new`.
  Nothing becomes `active` without someone accepting it, so a scan that finds
  forty sensors does not hand the assistant forty things to switch.
- Re-scans refresh what the backend reports and never overwrite the names,
  rooms and aliases the household typed.
- A device missing from a complete scan is marked `missing`, not deleted.

Home Assistant (`home_devices/ha.py`) is read over its WebSocket API: the
registries on connect, again within seconds of any registry change (a device
paired in ZHA, a Tuya device linked through the Smart Life app), and in full
every five minutes. Entities group by HA device; configuration, diagnostic,
hidden and disabled entities are left out, as are integrations' service
devices (the weather, the sun).

## State

`{paths.state}/home-devices/devices.json`, written atomically and backed up as
essential (`docs/backups.md`). A damaged file is refused at start rather than
replaced by an empty registry.

## Roadmap

1. HA backend, registry, inbox, page *(this)*
2. Control through HA; an MCP server and skill for the assistant
3. WiZ bulbs locally over UDP (true state; HA's cloud copy of them is stale)
4. Floor plan: import a DXF from any CAD app and drag devices onto it
5. Location-aware answers ("the lamp by the window", "everything upstairs")
6. The assistant switches over; overlapping HA entities leave Assist
7. MQTT devices

## Tests

`pytest -q` in this directory; the deployer runs the same suite against the
built image before replacing the container.
