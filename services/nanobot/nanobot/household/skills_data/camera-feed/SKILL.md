---
name: camera-feed
description: "The house cameras. Invoke with JSON: {\"skill\":\"camera-feed\",\"action\":\"...\"} — actions: list_cameras | snapshot(camera) = a photo of what the camera sees RIGHT NOW, shown inside the chat | detect(camera) = which objects are in view RIGHT NOW (people, cars, animals, furniture) with confidence, plus the photo | stream_url(camera) = link to the live video, reachable only on the house network. `camera` accepts the camera name or its id. Use it whenever someone asks what a camera can see, who is at the door, or whether the car is outside."
metadata: {"nanobot":{"translatable":true}}
---

# Cameras

To use this skill, output one of these JSON invocation blocks as plain text in
your response — the system intercepts and executes it automatically. Do NOT
use exec or curl for this skill, and do NOT write the Python yourself.

**There is no HTTP endpoint for skills.** No `/proxy/skill`, no port 5000, no
service you can POST an invocation to — writing one costs a turn and fails.
Emit the block, end your turn, and wait: the result comes back to you.
Ask for one camera per block, and never restate a block in the answer you write
after the result arrives.

**List all cameras:**
```json
{"skill": "camera-feed", "action": "list_cameras"}
```

**Take a snapshot** — a photo of what the camera sees right now. `camera` is the
name, as the household says it:
```json
{"skill": "camera-feed", "action": "snapshot", "camera": "patio"}
{"skill": "camera-feed", "action": "snapshot", "camera": "living"}
```
The skill resolves the name itself (it also accepts the id or the device's IP).
**Don't invent IPs or ids.** If you don't know which cameras exist, run
`list_cameras` first; if the name doesn't match, the skill hands you the list of
the ones that do.

**Live stream URL** (opens in a browser, **only on the home network**):
```json
{"skill": "camera-feed", "action": "stream_url", "camera": "patio"}
```

## Adding and removing cameras

**Add a camera.** `device_ip` is required — its address on the home network.
`name` is what the family will call it afterwards, so ask for one rather than
inventing it:
```json
{"skill": "camera-feed", "action": "add_camera", "device_ip": "192.168.88.52", "name": "patio"}
```
Optional, and only worth sending when the person actually said so:
`stream_type` (`rtsp` by default), `port`, `stream_path`, `username`,
`password`, `fps`, `width`, `height`, `rotation`. Everything you leave out keeps
the wall's own default — sending a guess overwrites a working default with it.

**Remove a camera.** Takes the name, the id, or the IP:
```json
{"skill": "camera-feed", "action": "remove_camera", "camera": "patio"}
```

**A camera that says `error` has usually just moved.** The router hands out
new addresses and the wall keeps dialling the old one, so a camera that is
powered on and streaming reads as broken. Look for it before telling anybody
it is down:
```json
{"skill": "camera-feed", "action": "rediscover_camera", "camera": "living"}
{"skill": "camera-feed", "action": "rediscover_camera", "camera": "patio", "dry": true}
```
It scans the subnet and asks each camera whether these credentials open it, so
only the right one can answer. It keeps the camera's id, its motion zones and
its recording rules — this is not delete-and-re-add. `dry` reports where it is
without moving it. If it finds nothing the camera really is off, or on another
network: say that, and do not offer to add it again under a guessed address.

**Confirm before removing.** Ask which camera and wait for a plain yes, in the
same turn structure you use for any other destructive step. Removal stops the
feed, drops the camera from the wall and clears its recording rules; nothing in
this skill puts it back, and re-adding it is a new camera with a new id — its
recordings do not come back with it. "Delete the patio one" from somebody who
meant "hide it" is the mistake this rule exists to stop.

**You cannot do either as the house assistant.** Alfred Casa runs with no
member credentials on purpose, so `add_camera` and `remove_camera` come back
saying so. That is a boundary, not a fault: say that managing cameras has to be
asked from a member's own Alfred, and do not retry.

**An IP is not a camera.** If `add_camera` succeeds the wall accepted the
address; it does not mean something is streaming there. Take a `snapshot`
afterwards to prove it, and say plainly if the snapshot fails — an entry that
was added but shows nothing is the failure people notice a week later.

## Showing the photo in the chat

`snapshot` returns a **`message`** field: an answer already written, with the
link inside it. **Send it exactly as it is and add nothing else.** It is the
only thing that makes the photo appear in the conversation (it can be tapped to
see it large).

**That `message` IS the complete answer.** Don't announce it beforehand ("let
me take a look"), don't close it afterwards ("there you go"), don't split it in
two. Measured: a photo of the patio arrived as three messages in a row — the
sentence, a sign-off, and the loose image — because the model sent the first
half and lost the link. Three phone notifications for a photo that needed one.

```
snapshot → {"message": "Front, right now:\n[Front](download:media/cam_front_1785208081.jpg)", ...}

Your answer:
Front, right now:
[Front](download:media/cam_front_1785208081.jpg)
```

If you would rather write the sentence yourself, then the `download_link` goes
in it **pasted exactly as it came**. An answer that announces the photo ("here
you go 👇", "here's the camera") without the link is an answer **with no
photo**: the household sees an arrow pointing at nothing. If your message does
not contain `](download:`, you sent nothing — write it again with the link.

- **Don't save it to the person's files.** Asking for a camera is wanting to
  *see*, not to archive. The photo is already where the chat can show it;
  copying it to the share with the `file-share` skill leaves a file nobody
  asked for and shows nothing on top of that. Only do it if they say so
  explicitly ("save it").
- Don't invent the path or write the link from memory: use the `download_link`
  the skill returned, with its real filename.
- If they ask for "the camera" without saying which and there are several, run
  `list_cameras` first and ask, or show the one that matches by name.

## If they ask WHAT IS in the image → `detect`

**You do not see the photo.** `snapshot` returns a path, not the image. Saying
"it all looks quiet" while looking at a path is invention, and the household has
no way to tell that apart from something real.

When the question depends on the content — "what's in the patio?", "is the car
there?", "is anybody there?", "who arrived?" — use **`detect`**, which brings
you the photo in the same result:

```json
{"skill": "camera-feed", "action": "detect", "camera": "patio"}
```

It returns what the detector recognised, with its confidence, where it is in
the frame and how much of it it fills — plus the `message` with the photo, the
same as `snapshot`:

```
→ {"tracked": {"any_present": false,
               "person":  {"present": false, "active": 0, "stationary": 0, "since": null, "seconds_since_seen": 412.0},
               "vehicle": {"present": false, ...}, "animal": {"present": false, ...}},
   "counts": {"chair": 2, "potted plant": 1},
   "detections": [
     {"label": "chair", "confidence": 0.92, "certainty": "high",
      "position": "middle-centre", "area_pct": 8.4},
     {"label": "potted plant", "confidence": 0.33, "certainty": "low",
      "position": "top-centre", "area_pct": 2.1}],
   "nothing_detected": false,
   "message": "Patio, right now:\n[Patio](download:media/cam_patio_...jpg)"}
```

You write the sentence — the detector doesn't write prose, it hands over data.
Always put the photo link at the end.

### `tracked` is the answer to "is anyone there"

`detections` is one frame. `tracked` is what the camera's tracker confirmed
across several frames in the last half minute: `present` (a confirmed person
/ vehicle / animal seen recently), `active` (moving) and `stationary` (there,
but still -- a parked car, somebody sitting). **Answer presence from
`tracked`, not from one detection**: a `person` at 0.31 in `detections` with
`tracked.person.present: false` is the detector guessing at a shape in the
dark, and the right sentence is "no veo a nadie ahora". `tracked: null` means
the camera has no tracker state yet (just started); then, and only then,
fall back to the detections with their certainty.

### `certainty` is not decoration

It is the only thing separating a fact from a guess, and the household cannot
see the difference unless you tell them.

| certainty | How you say it |
|---|---|
| `high` | As a fact: "there's a car parked out front". |
| `medium` | With a hedge: "there seems to be a chair". |
| `low` | As a possibility, **never** as fact: "there might be somebody, but it's hard to make out". |

Measured in the patio at night, this detector reported `person` at 0.31 and read
a pergola as two `bed`. **Nobody was there.** A "there's somebody in the patio"
said at that confidence frightens the household for nothing — and it sounds more
certain than anything a vision model would have said. If all you have is `low`,
say it as a doubt and show the photo so they can look themselves.

- `nothing_detected: true` means it **recognised nothing**, not that the camera
  is broken nor that the patio is necessarily empty. At night that is normal.
  Say it like this: "I'm not detecting anything in the patio; here's the photo
  in case you want to look".
- The detector knows common objects (people, cars, animals, chairs, plant pots,
  backpacks). **It does not read text, does not recognise faces and does not
  know whose car it is.** If they ask "who arrived?", say there is a person, not
  a name.
- Don't add anything that isn't in `detections`: not the time, not the weather,
  not "all quiet".
- Need to read something written in the image — a number plate, a sign, a piece
  of paper? The detector doesn't do that: for those, `snapshot` +
  `describe_image`.

## Rules

- For "show me", "I want to see the patio", "send me a photo" → `snapshot` and
  show the photo, **and nothing else**. Don't call `describe_image`: asking to
  *see* is not asking to be *told*, and looking at it costs about forty seconds
  nobody asked to wait. The photo alone is the complete answer.
- For "what can you see?", "what's there?", "is anybody there?", "look who's
  there" → `detect`, which brings the objects and the photo in a single call.
  It is ~0.15s against the 10-40s it cost to look at the image with
  `describe_image`.
- `stream_url` is live video and **cannot be embedded in the chat** (the page
  is https and the stream is http, so the browser blocks it) and does not work
  outside the house. Only hand it over if they ask to open the live video while
  on the local network, and say so.
- After the system returns the result, present it to the person. Don't invoke
  the skill again.
- **Once per camera only.** If you already have the photo, that is the photo:
  repeating `snapshot` on the same camera only makes new files of the same
  moment, and the household ends up getting the same image three times.
- **Never in the background.** It is one call and done; `spawn` for a camera
  delivers the image when it is no longer any use to anybody.
- Answer in the person's own language.
