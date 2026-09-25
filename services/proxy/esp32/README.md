# ESP32 devices

Three devices, three jobs, **three separate sketches**. Before anything else,
make sure you are following the right README: their Arduino IDE board settings
contradict each other, and the wrong ones fail in confusing ways rather than
cleanly.

| | [`menu/`](menu/) | [`control/`](control/) | [`voice/`](voice/) |
|---|---|---|---|
| Job | Weekly menu dashboard | Lights and cameras | Talk to Alfred |
| MCU | ESP32-C3 | ESP32-S3 (CrowPanel 7") | ESP32-S3 |
| Display | WeAct 4.2" e-paper, 400×300 | 800×480 IPS, RGB565 parallel | none — an LED ring |
| Input | None | GT911 capacitive touch | microphone (streamed) |
| UI layer | None — blits a server-rendered bitmap | LVGL 9.1 | none |
| Power | Battery, deep sleep between wakes | Mains | Mains |
| `USB CDC On Boot` | **Enabled** (native USB) | **Disabled** (CH340 bridge) | **Enabled** |
| Status | Working end to end | App is new, unflashed | Unflashed |

## Why they share no library

Each sketch carries **its own copy** of `portal.cpp` / `portal.h`, and that is a
decision rather than an accident.

Arduino makes real sharing awkward — a sketch folder cannot include a sibling
directory, so the options are a library installed into `~/Arduino/libraries` (an
extra install step on every machine, and a version that drifts from the repo) or
copies. With three devices this size, copies win.

**The cost is real and falls on you: a fix to `portal.cpp` must be applied three
times.** Each copy says so at the top and names the other two. What differs
between them is small and deliberate — the NVS namespace, the `secrets.h` macro
names, the AP name prefix, the wording on the form, and in `control/` the fact
that only WiFi is configurable because the services it talks to need no
credential.

Everything below the provisioning layer genuinely has nothing to share. The MCUs
differ, the displays differ, and:

- **Different MCU.** The C3 is single-core RISC-V with no PSRAM; the control
  panel needs 8 MB of it for a 750 KB framebuffer, a 460 KB camera canvas and
  the JPEG it decodes from. The puck needs none — it holds no audio at all.
- **No LVGL on the e-ink.** A screen with no touch, no animation and a few
  redraws a day does not need a UI toolkit; `drawImage()` over a 15 KB buffer
  covers it.
- **Rendering moved to the server** for the menu, so layout changes are a
  HomeCore deploy instead of a trip to the kitchen with a laptop.
- **No display at all on the puck.** Twelve LEDs and a chirp are the whole
  interface, and it holds no audio: it streams the microphone and plays what it
  is handed.

## What each one talks to

- **menu** → HomeCore `GET /menu/api/render.bin` over HTTPS with a
  self-signed certificate (hence `setInsecure`), authenticated with a shared
  `X-Device-Token`. Server decides the sleep interval via `X-Sleep-Seconds`.
- **control** → the lights server (`hub.home:5010`) and camera server
  (`cameras.home:21020`) directly, plus MQTT on `mqtt.home` for light state.
  Plain HTTP, no credentials — going through HomeCore's `/luces/` and `/camaras/`
  mounts would need a session cookie and a CSRF token.
- **voice** → the voice gateway (`compute.home:8083`) over a websocket that
  carries the microphone up and control messages down, plus MQTT for
  announcements. **The wake word runs on the gateway, not on the device** — no
  on-device engine ships an "Alfred" — so this firmware has no wake engine and
  no VAD in it. See [`../../home-voice/`](../../home-voice/).

## Provisioning

All three use the same flow: **tap RESET twice**, the device becomes an access
point, join it from a phone and fill in the form. Settings live in NVS and
survive reflashing. Each also supports an optional `secrets.h` for flashing a
pre-provisioned device on the bench; NVS always wins over it.

`secrets.h` is gitignored (`home-chat/.gitignore`).

## Layout

```
esp32/
  menu/      ESP32-C3   e-ink menu panel        (working)
  control/   ESP32-S3   lights + cameras panel  (new app on verified bring-up)
  voice/     ESP32-S3   voice puck              (new)
```
