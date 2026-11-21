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
replaced by an empty registry. Floor plans sit beside it in `plans/`: each as
the JSON the page draws and the DXF it was read from, so a better reader can
re-read them later without anyone uploading again.

## Floor plans

Draw the house in any CAD program and save it as ASCII DXF; one plan per
floor. `home_devices/floorplan.py` keeps lines, text and block inserts (doors,
furniture), and leaves out hatches, dimensions and layers that are off or
frozen. Coordinates stay in drawing units, and so do device positions, so a
plan redrawn in place keeps every device where it was.

Rooms are *found*, not drawn: walls are loose lines in a real drawing, and the
only closed outline is usually the whole flat. Every line is rasterised (about
600 cells along the long side) and every label floods outward at once, never
crossing a line; a cell belongs to the label that reaches it first. A door's
leaf and swing close its opening, an archway splits about halfway, a spot
inside furniture takes the nearest room, and a note outside the walls is not a
room. Not every label names a room: within each walled-off space only labels
at least 80% of the largest text height there are rooms, so write room names
bigger than "Ventana" or "Cama". The smaller ones are landmarks.

## Where things are, for the assistant

`find` and `set` take a `floor` (a plan's name: "everything upstairs"), and
`find` takes `near`: a landmark or a placed device, answered nearest first with
`distance_m`, in metres whatever unit the drawing uses (`places.py`). Every
placed device reports its floor and, within 1.5 m of a landmark, `near` ("the
lamp by the window"). A device with a room but no spot is on the floor whose
plan has that room.

## Roadmap

1. HA backend, registry, inbox, page
2. Control through HA; an MCP server (`find`, `set`) for the assistant
3. WiZ bulbs locally over UDP, preferred over a cloud copy; identify (blink) and merge two records of one device
4. Floor plans: import a DXF from any CAD app, drag devices onto it, rooms found from the walls
5. Location-aware answers: floors, landmarks and distances ("the lamp by the window", "everything upstairs")
6. The assistant switches over; overlapping HA entities leave Assist
7. MQTT devices

## Tests

`pytest -q` in this directory; the deployer runs the same suite against the
built image before replacing the container.
