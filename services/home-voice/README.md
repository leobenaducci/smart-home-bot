# home-voice — Alfred in the rooms

One HTTP service the house's voice devices talk to. A device records audio and
gets audio back; it does not know that whisper, nanobot or piper exist.

The brain is not here — it is `nanobot-house`, deployed from
`../nanobot/docker-compose.house.yml`. This is the mouth and ears.

## How a sentence travels

```
[voz puck]  I2S mic → 40 ms frames, continuously
      │  WS /v1/stream?token=…      binary up
      ▼
[voice-gateway]  compute:8083
      ├─ token → room                     (devices.json, re-read per request)
      ├─ openWakeWord "alfred"  ── fires ─► {"state":"listening"}  ring blue
      ├─ VAD, in audio seconds  ── ends ──► {"state":"thinking"}   ring amber
      ├─ POST 127.0.0.1:8000/transcribe    → faster-whisper (already running)
      ├─ POST hub.home:21399/v1/chat/…    → nanobot-house, chat_id = room
      └─ piper --output_raw                → raw PCM, held ~2 min
      │  {"type":"speak","audio_id":…,"audio":{"sample_rate":22050,…}}
      ▼
[puck]  stops sending, then
      │  GET /v1/audio/<id>  → raw int16 mono PCM, streamed
      ▼
   I2S out → speaker,  then {"type":"resume"}
```

**The wake word runs here, not on the device.** No on-device engine ships an
"Alfred" — WakeNet has 29 built-in words and Alfred is not one, and Espressif's
custom route wants 20,000 corpus entries or a purchase order. Training our own
and running it on this side means the model is ours, retuning it never means
reflashing anything, and the firmware has no wake engine and no VAD in it.

It costs one real thing: a puck's microphone streams to this server whenever it
is powered — 256 kbps, never off the LAN. It costs nothing in availability. See
[`wakeword/README.md`](wakeword/README.md).

Unprompted speech (a timer, a finished background task) goes the other way:
Alfred's `announce` skill posts to `/v1/announce`, the gateway synthesizes and
publishes `home-voice/<room>/announce` on the broker, and the device — which
holds a subscription — fetches the clip and plays it.

**A room is a `chat_id`, not a deployment.** One `nanobot-house` serves the whole
house; nanobot keys sessions on `channel:chat_id` with a lock per key, so the
kitchen and the living room are concurrent turns with separate histories.
**Adding a room is a line in `devices.json`** — no deploy, no restart, because
the gateway re-reads that file per request.

## Why there is no Home Assistant here

An earlier draft of this put HA's Assist pipeline in the middle, which drags in
the Wyoming protocol and a translation layer for whisper. That is the right
answer if you buy off-the-shelf satellites, whose stock firmware only knows how
to talk to HA. It is the wrong answer for this house, which builds its own ESP32
devices and already has the two patterns this needs: a device token in NVS
naming the device (`minuta`), and MQTT for pushing to a device
(`docs/mqtt-conventions.md`).

Dropping it deleted the Wyoming bridge, three Wyoming containers, an HA custom
component and a deploy job. HA is still the MCP server Alfred uses for the rest
of the house — it is just not in the audio path.

## The device protocol

Everything the firmware needs. All of it is plain HTTP with no TLS: this is
LAN-only, like the lights API and the camera registry.

### `WS /v1/stream?token=<device token>`

The main path. Binary frames up (raw int16 mono 16 kHz, 40 ms each), small JSON
frames down:

| Down | Meaning |
|---|---|
| `{"type":"state","state":"idle\|listening\|thinking"}` | drive the light |
| `{"type":"speak","audio_id":…,"text":…,"transcript":…,"audio":{…}}` | fetch and play it |
| `{"type":"error","message":…}` | show a fault |

Up, besides audio: `{"type":"resume"}` when playback has finished. Until that
arrives the session stays busy and ignores incoming audio — **a device must stop
sending while its speaker is on**, which is the whole echo-cancellation strategy
here, and say `resume` afterwards or it will never be listened to again.

An unknown token is refused at the handshake with close code 4403.

### `POST /v1/turn`

One-shot alternative for a device that would rather record than stream — how
this worked before the wake word moved server-side. Still supported, still
tested, unused by the current firmware.

```
Header:  X-Device-Token: <the device's token>
Header:  X-Sample-Rate: 16000        (only if the body is raw PCM)
Body:    a WAV file, or raw int16 mono PCM
```

A body starting with `RIFF` is treated as WAV; anything else is raw PCM at
`X-Sample-Rate`. Sending raw is simplest on an ESP32 — no header to construct.
Cap is 30 s of 16 kHz audio.

```json
{
  "room": "cocina",
  "transcript": "what is the temperature in here?",
  "reply": "Hay dos: la del living y la de tu pieza.",
  "audio_id": "9f2c…",
  "audio": { "url": "/v1/audio/9f2c…", "encoding": "pcm_s16le",
             "sample_rate": 22050, "channels": 1, "bytes": 132300 }
}
```

### `GET /v1/audio/<id>`

Raw int16 mono little-endian PCM, `Content-Length` set, streamed in 1 KB
chunks — hand each chunk straight to the I2S DMA buffer. No WAV header to skip,
no decoding, no resampling: configure I2S to the `sample_rate` from the JSON
(22050 Hz for the current voice) and write bytes.

Clips expire after two minutes and at most 32 are held, so fetch it right away.

### `GET /v1/whoami`

```
Header:  X-Device-Token: <the device's token>
  → {"room": "cocina", "mqtt_topic": "home-voice/cocina/announce"}
```

Asked once at boot. A device needs its own room name for exactly one thing —
subscribing to its announce topic — and asking beats configuring it a second
time in the device's own NVS, where the two copies would drift the first time a
puck moved between rooms. `devices.json` stays the single place a room is
written down.

### `POST /v1/announce`

Alfred's path, not a device's — gated on the gateway token rather than a device
token:

```json
{ "room": "kitchen", "text": "The water has boiled." }
```

Publishes `home-voice/<room>/announce` with `{"audio_id", "text", "sample_rate"}`.
A 404 lists the rooms that do exist.

### `POST /v1/ir/send`, `POST /v1/ir/learn`, `GET|POST|DELETE /v1/ir/codes`

Alfred's path too, gated on the gateway token. The puck carries an IR LED and a
receiver, so the room's air conditioner and television — neither of which has a
network and neither of which ever will — are reachable by pointing a diode at
them.

Send something the room already knows, or something being tried out:

```json
{ "room": "living", "device": "tele", "command": "encender" }
{ "room": "living", "protocol": "NEC", "code": "0x20DF10EF", "bits": 32 }
{ "room": "living", "device": "aire", "ac": { "power": true, "mode": "cool", "degrees": 23 } }
```

The third form is the interesting one. An air conditioner's remote does not send
"one degree warmer" — it sends the entire state in every burst, so a stored
capture can only ever reproduce the temperature it was captured at. Naming the
protocol instead (`ac_protocol` on the device) lets the firmware *build* a
frame, and 23 degrees works without anyone having pressed 23.

`POST /v1/ir/learn` with `{room, device, command}` puts the room's ring on cyan,
waits up to 30 s for somebody to press a button on the original remote, and
stores whatever came out. A remote the library recognises is stored as protocol
+ code; an air conditioner as protocol + state; anything unrecognised as raw
timings, which replay perfectly well — they just cannot be reasoned about.

Codes live in `/state/ir-codes.json`, which the gateway **writes**. That is why
it is not in `/config` with `devices.json`: a learned code arrives at runtime and
cost somebody a walk to a drawer, so it is state, it is mounted writable, and it
is still outside any Jenkins workspace. See `ir-codes.example.json` for the
shape.

Everything here answers with what the puck actually reported: `ok: false` is a
puck that refused, **504 is a puck that never answered**, and the difference
matters — one is worth trying another code, the other is worth checking whether
the thing is plugged in. There is no feedback path from the appliance itself, so
`ok: true` means "the LED fired", never "the television turned on". Confirming
that is a question for whoever is standing in the room, which is exactly what
the `ir` skill makes Alfred ask before he saves a looked-up code.

A listing carries `confirmed` beside each command, because the listing is the
only place Alfred ever reads a stored code from — a flag that stopped at the
file was a flag nothing could act on, and he would have stated a code nobody had
watched work as fact. Every code, however it arrives — looked up, or captured off
a real remote — goes through the same normaliser, so a protocol name and a hex
code mean the same thing whichever door they came in by. A number that is not a
number is a **400 that names the field**, never a 500: the caller is a model, and
hex needs its `0x`.

### `GET /v1/sensors`

```json
{ "rooms": { "living": { "sensors": true, "celsius": 21.4, "humidity": 48,
                         "lux": 96, "motion": false, "seconds_since_motion": 812 } } }
```

Optional parts on the same puck: an I2C climate sensor, an I2C light sensor and
a PIR. `?room=` narrows it. A room whose puck has no sensors fitted answers
`{"sensors": false}` — deliberately distinct from a room that does not exist,
and from a temperature of zero. `sensors` follows the readings and not the
message: a puck with no parts on it still publishes a retained payload, so the
flag has to mean "there is a number here", not "something answered".

This reads **retained** MQTT state and never asks a device anything, so it
answers instantly and it answers even when the puck has been unplugged for an
hour. The puck publishes `home-voice/<room>/sensors/state` (retained, every
minute or on a real change) and `home-voice/<room>/sensors/event` (not retained,
when motion starts or stops).

`motion` is "something moved" and never "who". A room device in this house holds
nothing personal, and presence is a property of the room.

### Firmware notes

The firmware that implements all of this is
[`proxy/esp32/voice/`](../proxy/esp32/voice/) — an ESP32-S3 with an I2S mic,
an I2S amp and an LED ring. `gateway.cpp` there is the client side of everything
above.

- **Record at 16 kHz mono 16-bit** and send it raw, with `X-Sample-Rate`. That is
  what whisper wants and it saves building a WAV header on the device.
- **Nothing above depends on how a turn starts.** The puck uses a wake word; a
  button or a touchscreen would need no change here.
- **Play at whatever `sample_rate` the JSON says**, not at a compiled-in
  constant. It is 22050 Hz today because of the piper voice; changing the voice
  is a gateway rebuild and should not mean reflashing every device.
- **Fetch the clip immediately.** Clips live two minutes and only 32 are held.
- A device that was offline **misses an announcement entirely** — those messages
  are not retained, deliberately: an announcement replayed an hour later is
  worse than one lost.

## Deploy

Manual, like everything here. Order matters the first time:

1. Put `devices.json` **and `wake.onnx`** on compute at
   `/home/homestack/home-lab/configs/home-voice/` — copy
   `devices.example.json`, generate real tokens, and train the wake word (see
   [`wakeword/README.md`](wakeword/README.md)). **Not in the repo**: both are
   live config, one holds device secrets and the other is a file you will
   iterate on, and this house has lost live config to a Jenkins checkout four
   separate times.
2. Jenkins credentials: `NANOBOT_API_SECRET_HOUSE` (shared with the Alfred House
   job) and `VOICE_GATEWAY_TOKEN`.
3. Run `deploy-alfred-house` — the brain must answer before anything calls it.
4. Run `deploy-home-voice`. The build bakes the piper voice into the image, so
   the first build pulls ~60 MB and later ones do not.

The deploy verifies itself end to end: piper speaks a sentence, whisper hears it
back, Alfred answers it. It also refuses to go green if `devices.json` is
missing — a gateway with no registry 403s every device and looks like a firmware
bug.

## Testing without hardware

```bash
# the whole chain over the socket a puck actually uses. Needs the gateway
# started with WAKE_THRESHOLD=0, since a Spanish TTS voice cannot say the
# wake word convincingly — this tests everything the model hands off to.
docker run --rm --network host -v "$PWD/test:/test:ro" voice-gateway:latest \
    python /test/stream_smoke.py --base http://compute.home:8083 \
    --token <a device token> --gateway-token <gateway token>

# the one-shot path
python3 test/smoke.py --base http://compute.home:8083 \
    --token <a device token> --room cocina --gateway-token <gateway token>

# just the brain, as a room
curl -s -X POST http://hub.home:21399/v1/chat/completions \
  -H "Authorization: Bearer $NANOBOT_API_SECRET_HOUSE" \
  -H 'Content-Type: application/json' \
  -d '{"channel":"voice","chat_id":"cocina","messages":[{"role":"user","content":"hola"}]}'

# make a room speak
curl -s -X POST http://compute.home:8083/v1/announce \
  -H "Authorization: Bearer $VOICE_GATEWAY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"room":"cocina","text":"Probando el parlante."}'

# the infrared and sensor halves, which need a puck with the parts on it
python3 test/ir_smoke.py --base http://compute.home:8083 \
    --gateway-token <gateway token> --room living

# what every room is like right now — works with no IR hardware at all
curl -s http://compute.home:8083/v1/sensors \
  -H "Authorization: Bearer $VOICE_GATEWAY_TOKEN"
```

Without a puck, `ir_smoke.py` still checks everything on this side — the store
round-trip, the four ways a request can be wrong, the shapes a code can take —
and reports the send and learn steps as *unverified* rather than passing them.
A green run with no hardware means the gateway is right, not that the room is.

## Changing the voice

Two separate things: **what Alfred sounds like** (a rebuild) and **how he
speaks** (a restart).

`PIPER_VOICE`/`PIPER_BASE` build args in `voice-gateway/Dockerfile` pick the
voice. It is baked rather than downloaded at boot so the house keeps talking
when the internet does not.

The delivery is four environment variables, and it turned out to matter about
as much as the voice:

| | default | piper's default | what it does |
|---|---|---|---|
| `PIPER_LENGTH_SCALE` | 1.18 | 1.0 | slower. Most of the effect is here |
| `PIPER_NOISE_SCALE` | 0.55 | 0.667 | steadier pitch — composed, not expressive |
| `PIPER_NOISE_W` | 0.65 | 0.8 | less variation between phonemes |
| `PIPER_SENTENCE_SILENCE` | 0.45 | 0.2 | a beat between sentences |

The same sentence takes 12.4 s at piper's defaults and 14.8 s at these. Until
2026-08-13 the gateway passed **none** of them, which is why Alfred read things
out rather than announcing them.

### The listening bench

`GET /v1/tts/voices` and `POST /v1/tts` exist so the next voice can be chosen
the same way this one was. In HomeCore's chat, **Debug → 🔊 Voz de Alfred** turns
on reading his replies aloud, with a dropdown for the engine and one for the
voice; **▶️ Probar** speaks a fixed line so two candidates are compared on the
same sentence rather than on whatever was said last.

It is in a browser because there is no puck on any wall yet, so the only
speaker available is the phone the person is already holding. It stays useful
afterwards — trying a voice at a desk beats walking to the kitchen.

Extra piper voices go in `/config/voices` as `<name>.onnx` + `<name>.onnx.json`,
beside the wake model and for the same reason: comparing voices is iterative
and a turn of that loop should be copying a file. A voice missing its `.json`
is skipped rather than half-loaded — otherwise it is offered in the dropdown
and fails only once somebody picks it.

**Choosing in the bench never changes what the house speaks with.** Rooms,
announcements and replies all follow `DEFAULT_TTS_ENGINE` and the baked voice.
Promoting a winner is an env change and a restart, deliberately — a dropdown
that silently repoints the whole house is not a comparison.

#### Qwen3-TTS, as a sidecar

`docker compose --profile bench up -d qwen-tts` — a separate container, not part
of the deploy. It wants torch and CUDA (which would take the gateway's image
from ~1 GB to several) and it wants VRAM, which is shared: `nvidia-smi` shows a
12 GB RTX 3060 with ~8.4 GB free at idle, and Ollama loads `qwen3-vl:4b`
(3.3 GB) on demand for Alfred's vision turns — the house moved down from the
8b (6.1 GB), so there is more room than this paragraph used to claim. So it
defaults to the **0.6B**
checkpoint, loads on first use rather than at boot, and hands the card back
after five idle minutes. `QWEN_TTS_IDLE_UNLOAD_S=0` keeps it resident, which is
what you want only if it stops being a bench.

Weights are **not** baked, unlike the piper voice, and the difference is the
point: the piper voice is 60 MB and the house must keep talking with no
internet; this is gigabytes and the house does not depend on it. Point the
gateway at it with `QWEN_TTS_URL=http://127.0.0.1:21012` (the gateway is
host-networked and the sidecar publishes 21012); unset, the engine is simply not
offered, and the dropdown says why rather than failing when picked.

**The first ▶️ Probar after starting it answers 503, not audio.** That first
call is what triggers the download-and-load, which is minutes rather than
seconds, so the sidecar says "loading, come back" and returns immediately
instead of holding the socket past the gateway's timeout and reporting itself
dead. Click again once `curl localhost:21012/health` shows `"loaded": true`.

`restart: "no"`, unlike everything else here: it is started by hand for a
session, and `unless-stopped` would bring it back on every reboot from then on,
reserving the card forever for a bench nobody is using.

Cloned voices: drop a reference recording into `/config/qwen-voices` and
`mayordomo.wav` becomes the voice `mayordomo`. **Record it in Spanish** —
cross-lingual cloning carries the accent of the reference, so an English clip
speaking Spanish sounds exactly like one.

### The voice was chosen by listening, 2026-08-13

Piper's entire Latin American Spanish catalogue is three voices — `es_MX-ald`,
`es_MX-claude`, `es_AR-daniela` — so two male options exist in total. A bake-off
of those against the Castilian ones, same line, butler cadence throughout,
picked **`es_ES-davefx-medium`**. The previous voice (`es_MX-claude-high`) had
the better argument on paper: it phonemizes as es-419, which is the right
Spanish for a Chilean house. It lost on hearing. The formality that makes a
Peninsular voice sound foreign in casual speech is the same formality that
makes it sound right coming from a butler.

**What it costs:** Chilean vocabulary in a Peninsular mouth. In the bake-off
the house's own whisper transcribed davefx saying *"living"* as *"línea"*. That
class of word — *living, palta, micro* — is where this voice will grate, and it
will grate on the twentieth hearing rather than the first. If it does, `es_MX-ald`
is the fallback and nobody has to redo the comparison.

Piper's Spanish number handling is good, which is worth knowing because it
rules out at least one alternative engine: all five candidates round-tripped
`20:15` → *"veinte quince"*, `400` → *"cuatrocientos"* and `21,5` →
*"veintiuno coma cinco"* through whisper. Kokoro has a documented Spanish
number bug and would mangle exactly the sentences Alfred says most.
