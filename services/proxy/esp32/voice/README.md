# Alfred voice — the voice puck

The thing you talk to in a room. Say **"Alfred"**, speak, and Alfred answers out
loud.

It has **no screen**. An LED ring is the whole interface, and it knows nothing
about whisper, nanobot or piper.

It can also, optionally, blast infrared at the room's air conditioner and
television, learn codes off their original remotes, and report what the room is
like — temperature, humidity, light, whether anyone is in it. All of that
compiles out if the parts are not fitted.

**The wake word does not run on this device.** It streams its microphone to the
voice gateway continuously and does what it is told. That is not a shortcut — it
is what makes the word "Alfred" possible at all (see below) — and it means this
firmware has no wake engine, no voice-activity detection and no recording
buffer in it. The protocol is in
[`../../../home-voice/README.md`](../../../home-voice/README.md); the client is
`stream.cpp` (microphone up, control down) and `gateway.cpp` (which room am I,
fetch a clip).

> **This sketch has never run on hardware.** The gateway side of it *is*
> verified end to end — a synthesized sentence was streamed up the same socket
> this firmware uses, whisper transcribed it verbatim, and the wake gate was
> confirmed to both fire and correctly not fire. Nothing here has been flashed.
> See "What to verify first" at the bottom.

## Hardware

| | |
|---|---|
| MCU | ESP32-S3. PSRAM is no longer required — nothing here buffers audio — but it is what the pin defaults assume |
| Mic | I2S MEMS, INMP441 / ICS-43434 / SPH0645. Tie L/R to GND |
| Amp | I2S class-D, MAX98357A → 4 Ω speaker |
| Ring | WS2812 / NeoPixel, 12 pixels |
| Power | Mains. It never sleeps |

Everything below is **optional**. Set the pin to `-1` (or the `SENSOR_*` define
to `0`) and that part compiles out; a puck without any of them is the puck as
originally designed and behaves identically.

| | |
|---|---|
| IR LED | 940 nm, **through an NPN** — see below. This is what talks to the air conditioner and the television |
| IR receiver | TSOP38238 / VS1838B, 38 kHz demodulator. Only needed to *learn* codes off a remote |
| Climate | SHT31 (0x44) or AHT20 (0x38), I2C |
| Light | BH1750 (0x23), I2C |
| Presence | PIR, AM312 preferred (3.3 V) over HC-SR501 (5 V and two trimmers to set wrong) |

Pins live in `board_config.h` and nowhere else. Defaults:

```
mic   BCLK 4   WS 5    DIN 6
spk   BCLK 15  WS 16   DOUT 17   SD(enable) 18
ring  DIN 8
IR    TX 9     RX 10
I2C   SDA 11   SCL 12
PIR   13
```

**The IR LED does not go on the GPIO.** It wants 100 mA or more to cross a
room; a GPIO sources 40. GPIO 9 → ~1 kΩ → base of a 2N2222/BC337, LED and
~100 Ω on the collector off the **5 V** rail, emitter to ground. Wired direct it
will appear to work at 30 cm and fail at the sofa, which is the most annoying
possible failure because it looks like a wrong code.

The receiver wants 100 Ω in series with its VCC and 10 µF to ground beside it.
The LED ring puts real noise on that rail and a hungry demodulator reads it as
infrared.

Both climate and light are I2C and share one bus, so fitting both costs no extra
pins. The driver probes for them at boot: whatever answers is used, whatever
does not is simply absent for the run. A dead sensor never stops the puck being
a microphone.

The mic and speaker are on **separate I2S peripherals** on purpose: the mic runs
at 16 kHz because that is what whisper wants, and the reply arrives at the piper
voice's 22050 Hz. The S3 has two controllers, so this costs pins and removes a
teardown/rebuild from the middle of every turn.

## Arduino IDE settings

Same trap as the other two panels — **these are not optional**, and they differ
per board. See `../README.md`.

| Setting | Value |
|---|---|
| Board | ESP32S3 Dev Module |
| Core | esp32 by Espressif **3.x** |
| PSRAM | OPI PSRAM if your module has it (not required any more) |
| Partition | Huge APP (3MB No OTA/1MB SPIFFS) |
| Flash Size | match your module |
| USB CDC On Boot | **Enabled** (unlike the CrowPanel — this board has no CH340) |

Libraries: **Adafruit NeoPixel**, **PubSubClient**, **ArduinoJson** (v7),
**WebSockets** (by Markus Sattler / Links2004), and **IRremoteESP8266** if the
blaster is fitted. `ESP_I2S` and `Wire` ship *inside* the core — do not install
them separately.

IRremoteESP8266 rather than Arduino-IRremote, which is what `IoT_projects/IR_Test`
reached for first. The deciding feature is air conditioners: Arduino-IRremote
handles button-shaped remotes and treats an AC frame as an unrecognised blob, so
"23 degrees" would mean capturing 23 degrees, and 24, and 25. This one encodes
the state of some sixty AC protocols, so a temperature is a number in a struct.
The name says ESP8266; ESP32 is a first-class target.

The climate and light sensors are read over raw `Wire` with no library at all —
each is one command and one read, and the alternative was three more libraries
to install at the right versions on a project whose README already has to
explain that the board settings are not optional.

## Configuring it

Two paths, same as the menu panel:

- **Portal** (works on a device already on a shelf): tap RESET twice. It becomes
  an access point `AlfredVoice-XXXX` / `alfred1234`; join it and the form appears.
  Set the gateway URL and this device's token.
- **secrets.h** (faster on the bench): copy `secrets.h.example`, fill it in.
  NVS always wins over it.

The token must exist in the gateway's `devices.json` on compute. **The room is
not configured here** — the puck asks `/v1/whoami` at boot, so `devices.json`
stays the only place a room is written down. Moving this to another room is an
edit there and a reboot here.

## The LED ring

| Look | Meaning |
|---|---|
| White chase | booting, or the gateway socket is down (this is the fault state) |
| One dim pixel breathing | idle — streaming, gateway listening for the word |
| Solid blue | the word fired. Speak |
| Amber comet | Alfred is thinking |
| Green | speaking |
| Purple pulse | config portal is open |
| Three red flashes | that turn failed |

A chirp marks the wake, so someone facing away still knows it heard them.

## The wake word, honestly

It answers to **"Alfred"**, and getting there meant putting the wake word on the
server rather than on this chip.

No on-device engine ships an "Alfred". WakeNet — the only one that runs on an
S3 without a tflite-micro port — has 29 built-in words (Hi ESP, Alexa, Jarvis,
Computer, Sophia, Mycroft…) and Alfred is not among them. Espressif will train a
custom one, but you must supply 20,000 corpus entries from 500+ speakers
including 100 children, or pay them. Neither is a household.

So the model is ours, it is trained with openWakeWord, and it runs on compute
where there are 56 idle cores. What that buys:

- The word is whatever we want, and **retuning it never means reflashing this**.
- This firmware lost its wake engine *and* its VAD instead of gaining them.

What it costs: the microphone streams to the house server whenever this is
powered — 256 kbps, never off the LAN. It costs **nothing** in availability: an
on-device wake word would not help when the gateway is down, because a puck that
heard you with no Alfred to ask has nothing to say.

Training and tuning it: [`../../../home-voice/wakeword/README.md`](../../../home-voice/wakeword/README.md).

## How a turn works

The loop is: read 40 ms of microphone, send it up the websocket, repeat. The
gateway decides everything else and says so:

| It sends | This does |
|---|---|
| `state: listening` | ring blue, chirp — the wake word fired |
| `state: thinking` | ring amber — uploaded, waiting on Alfred |
| `speak` + an audio id | stop sending, fetch the clip over HTTP, play it, `resume` |
| `error` | three red flashes |

**Not sending while the speaker is on is what keeps Alfred from waking himself**
— there is no echo canceller anywhere in this design. After playback the I2S
input buffer is flushed, because everything in it is Alfred, and the gateway
would otherwise run its wake model over his own voice.

A wake with **no speech after it produces no sound at all** — the gateway simply
resets. A puck that says "I didn't hear you" every time the television trips the
wake word is a puck that gets unplugged.

## Announcements

Alfred can speak unprompted — a timer, a finished background task. The gateway
publishes `home-voice/<room>/announce` and this device, subscribed since boot,
fetches the clip and plays it.

Those messages are **not retained** (`gateway.py`), so a puck that was offline
missed it. That is deliberate: an announcement replayed an hour later is worse
than one lost.

## Infrared and sensors

Optional, and the reason they are on *this* device rather than their own: it is
already mains-powered, already in the middle of the room, already pointed at the
television, and already holds an MQTT session. A separate node would be a second
thing to build, flash, name and find a socket for.

**Out** (`ir.cpp`) — `home-voice/<room>/ir/set` carries a command, and the puck
answers on `home-voice/<room>/ir/event`. Every command is answered, including
the failures: the gateway is standing in an HTTP request waiting for it, and
"the puck refused" and "the puck is unplugged" need different fixes.

A code can be a recognised protocol and a value, an air conditioner's whole
state, or raw timings for a remote nothing recognised. All three replay. The AC
also takes a *described* state — mode, temperature, fan — which the firmware
encodes on the spot, so 23 degrees works without anyone ever having pressed 23.

**Learning** puts the ring on cyan and turns the receiver on for one frame, then
off again. It is off the rest of the time on purpose: left running it would
spend an interrupt on every remote in the room and every fluorescent tube, on a
device whose actual job is a continuous 256 kbps uplink.

**In** (`sensors.cpp`) — temperature, humidity, light and presence go to
`home-voice/<room>/sensors/state`, **retained**, once a minute or whenever
something changes by more than noise. Motion also fires
`home-voice/<room>/sensors/event`, not retained. Retained state is what lets
Alfred answer "is it cold?" instantly instead of waiting for the next reading.

Presence is a boolean and a timestamp. A PIR says "something moved" and never
who, which is exactly as much as a device in a shared room should know.

Sending IR blocks the loop for as long as the frame takes — up to ~200 ms for a
chatty air conditioner — so the microphone buffer is flushed afterwards, the
same as after playing a reply. Half a word arriving at the wake model is worse
than no word.

## What to verify first

In rough order of how likely each is to be wrong:

1. **Two `I2SClass` instances both come up.** The mic and speaker are separate
   peripherals at different sample rates. If the second allocation fails,
   `spkEnsure` in `audio.cpp` already does a reconfigure — it would just need to
   tear the mic down first.
2. **The uplink keeps up.** A 40 ms frame every 40 ms, forever. If the serial
   log shows the socket dropping under load, the frame size in `board_config.h`
   is the knob.
3. **`audioFlushInput()` actually drains.** If Alfred sometimes answers himself,
   the flush is not clearing what the mic captured during playback.
4. **Mic gain against the wake model.** The model is trained on ordinary speech
   levels; an INMP441 with L/R floating (rather than tied to GND) answers on the
   other slot and yields silence, which looks identical to "the wake word never
   works".
5. **`WebSocketsClient` reconnects.** Pull the gateway's plug and confirm the
   ring goes white and then recovers on its own without a power cycle.

If the IR and sensor parts are fitted, then in this order:

6. **The blaster reaches across the room.** Test at the sofa, not at the bench.
   A LED driven straight off the GPIO works at 30 cm and fails at 3 m, and the
   symptom is indistinguishable from a wrong code. A phone camera sees IR: point
   one at the LED and send something.
7. **A learned code replays.** Learn a button off the real remote, then send it
   back. This is the whole feature in one test, and it exercises the capture
   buffer size — an air conditioner's frame is hundreds of transitions, and a
   buffer too small truncates it silently into something that will never work.
8. **`MQTT_BUFFER_BYTES` is actually applied.** PubSubClient defaults to 256
   bytes and drops anything larger **without an error at either end**. A learned
   raw code is several kilobytes. If learns of ordinary remotes succeed and
   unrecognised ones vanish, this is why.
9. **IR sending versus WiFi jitter.** The library bit-bangs the carrier with
   interrupts enabled, and this device has a radio transmitting continuously. If
   a code works two times in three, that is the cause; `repeats` in the payload
   is the knob, not a different code.
10. **The PIR settles.** These lie for up to a minute after power-on. Motion is
    not believed until then, deliberately — otherwise every power blink
    announces that somebody walked in.
11. **I2C survives the wiring.** Long dupont leads on a wall at 100 kHz is
    already a compromise. `[sensors]` lines at boot say which parts answered; a
    sensor that is fitted and absent from that log is a wiring problem, not a
    code one.

Verify the server side first, without any of this hardware — it streams a
synthesized sentence up the same socket:

```bash
docker run --rm --network host -v "$PWD/test:/test:ro" voice-gateway:latest \
    python /test/stream_smoke.py --base http://compute.home:8083 \
    --token <this device's token> --gateway-token <gateway token>
```
