# Control panel (ESP32-S3, 7" touch)

Arduino firmware for the wall panel by the door: **tap a light to toggle it, tap
a camera to see it.**

> This sketch was `alfred/` until the voice assistant moved to its own
> screenless device ([`../voice/`](../voice/)). That suits both better — a
> microphone wants to be where people stand, a screen wants to be at eye level
> near a doorway — and it is why `board_config.h` still records I2S pins nothing
> here uses.

> **Its light half has no service to talk to any more.** Home Assistant owns
> every smart device in the house now, and the `home-lights` service this
> firmware speaks to — its MQTT topics, its `/api/control`, HomeCore's `/luces/`
> mount — is gone. The camera half still works as written. Porting the light
> half means talking to Home Assistant instead, which is a different protocol
> and a reflash; nothing here has been rewritten to guess at it, so what
> follows describes the firmware as it stands rather than as it would need to
> be.

> **The app is new and has never run on hardware.** The display, touch and LVGL
> bring-up underneath it is the same code that was verified on the panel; the
> lights, cameras and portal layers on top are not. See "What to verify first".

Two other panels, both separate projects on different hardware:
[`../menu/`](../menu/) (e-ink menu) and [`../voice/`](../voice/) (voice).
Their READMEs' board settings **actively contradict** this one's
(`USB CDC On Boot` in particular) — make sure you are reading the right one.

## What it talks to

Two LAN services, directly, both plain HTTP and neither asking for any
credential:

| | |
|---|---|
| Lights | `hub.home:5010` for the boot snapshot, then **MQTT** for everything |
| Cameras | `cameras.home:21020` — `/api/cameras`, then `/snapshot/<id>?w=640&q=60` |

Going through HomeCore's `/luces/` and `/camaras/` mounts instead would need a
session cookie, a CSRF token and `setInsecure` for a self-signed certificate.
Direct needs none of that.

**Lights come over MQTT rather than polling**, for two reasons that are worth
knowing before changing it:

- `home-lights/lights/state` is **retained**, so the list is populated the
  instant the broker connects, and single-bulb changes arrive on
  `home-lights/lights/<MAC>/state` without republishing the house.
- **There is no `toggle` over HTTP.** `/api/control` takes `on|off|brightness|rgb`
  only; toggle exists solely on the MQTT `set` topic.

The catch, handled in `mqttEnsure()`: the broker runs `persistence false`, so
retained state does **not** survive a broker restart. Every connect therefore
also does one HTTP `GET /api/state`.

**Cameras are snapshots, never the stream.** `/stream/<cam>` is MJPEG and the
server signals the previous generator to exit when a new one opens — one viewer
per camera. A panel holding a stream would silently kick whoever was watching
that camera in a browser. The `?w=`/`?q=` parameters were added to HomeCameras
for this panel: a full frame is 130–175 KB, at `w=640&q=60` it is about 40 KB.

## Hardware

Elecrow **CrowPanel ESP32 HMI 7.0"** (model DIS08070H).

| | |
|---|---|
| MCU | ESP32-S3-WROOM-1-N4R8 — 4 MB flash, 8 MB OPI PSRAM |
| Display | 800×480 IPS, 16-bit RGB565 parallel, EK9716BD3 + EK73002ACGB |
| Touch | GT911 capacitive, I2C on GPIO 19/20 |
| Backlight | GPIO 2 (PWM) |
| USB | USB-C via a **CH340** bridge — not the ESP32-S3's native USB |
| Power | DC 5V 2A — USB power alone may brown out at full backlight |

All pins live in [`board_config.h`](board_config.h).

> **Board revision matters.** Elecrow shipped V1.0, V2.0 and V3.0 of this panel.
> V3.0 added a PCA9557 I/O expander that gates the GT911 reset line. The sketch
> assumes V3.0 (`BOARD_HAS_PCA9557 1`). If your board is older, touch will fail
> to initialise — set that define to `0`. The revision is silkscreened on the
> back of the driver board.

## Arduino IDE setup

### 1. CH340 driver

The USB-C port is a real UART, but it goes through a **CH340** bridge rather
than the ESP32-S3's native USB. Without the driver the board shows up under
*Other Devices* in Device Manager with no COM port at all.

Install WCH's official driver — the `CH341SER` package covers CH340 too:

- Windows: <https://www.wch-ic.com/downloads/CH341SER_EXE.html> (WHQL-signed, Win 7–11)
- macOS: <https://www.wch-ic.com/downloads/CH34XSER_MAC_ZIP.html>
- Linux: built into the kernel as `ch341`, nothing to install

Replug after installing and a COM port appears. Elecrow also bundles this same
driver in the `Tool/CH340` folder of their tutorial download, but the copy from
WCH is newer.

### 2. ESP32 core

Install **esp32 by Espressif Systems** (3.x) via Boards Manager.

### 3. Board settings

Select **ESP32S3 Dev Module**, then set:

| Setting | Value |
|---|---|
| Flash Mode | QIO 80MHz |
| Flash Size | **4MB (32Mb)** |
| Partition Scheme | Huge APP (3MB No OTA/1MB SPIFFS) |
| PSRAM | **OPI PSRAM** |
| USB CDC On Boot | **Disabled** |
| Upload Speed | 921600 |

The three bold rows are the ones that bite:

- The IDE defaults to 16 MB flash, which this board does not have. The symptom
  is not a clean failure but cascading weirdness in touch, LVGL and WiFi.
- Without OPI PSRAM there is nowhere to put the 750 KB framebuffer and the
  display never comes up; the sketch logs `FATAL: no PSRAM detected`.
- **USB CDC On Boot must stay Disabled.** GPIO19/20 are the ESP32-S3's native
  USB D-/D+ pins, and this board reuses them for the touch I2C bus. Enabling CDC
  leaves the USB_SERIAL_JTAG peripheral holding those two pins, and touch dies
  while the display, WiFi and serial all keep working — a confusing failure.
  Serial comes over the CH340, so you lose nothing by leaving it off.

### 4. Libraries

| Library | Version | Source |
|---|---|---|
| `lvgl` | **9.1.0** | Library Manager |
| `LovyanGFX` | latest 1.1.x | Library Manager |
| `TAMC_GT911` | latest | Library Manager |
| `PCA9557` | latest | **Not in Library Manager** — get it from [Elecrow's repo](https://github.com/Elecrow-RD/CrowPanel-7.0-HMI-ESP32-Display-800x480) and install as a ZIP |
| `PubSubClient` | latest | Library Manager — the lights bus |
| `ArduinoJson` | **v7** | Library Manager |
| `TJpg_Decoder` | latest | Library Manager — camera snapshots |

LVGL 9.1 specifically. The 9.2+ API moved enough that this sketch will not
compile unchanged, and 8.x is a completely different API.

### 5. `lv_conf.h`

LVGL will not build until you configure it, and Arduino requires the config to
sit **next to** the library rather than in the sketch:

1. Copy `Arduino/libraries/lvgl/lv_conf_template.h` to `Arduino/libraries/lv_conf.h`
   (one level up — a sibling of the `lvgl` folder, not inside it).
2. Change the `#if 0` on line ~15 to `#if 1`.
3. Set:
   ```c
   #define LV_COLOR_DEPTH        16
   #define LV_USE_CANVAS          1   /* the camera still is drawn into one */
   #define LV_USE_TABVIEW         1   /* Luces / Camaras */
   #define LV_USE_GRID            1   /* the light buttons */
   #define LV_USE_FLEX            1   /* the camera button row */
   #define LV_USE_STDLIB_MALLOC  LV_STDLIB_CLIB   /* let LVGL use the PSRAM-backed heap */
   ```

This file is deliberately **not** committed here — it belongs to your Arduino
libraries folder, not to this repo, and committing a copy guarantees the two
drift apart.

### 6. WiFi credentials

Two ways in, and the second is the one that matters once the panel is mounted:

- **Config portal** — tap RESET twice. The panel becomes an access point
  `AlfredPanel-XXXX` / `alfred1234`; join it from a phone and a form appears.
  Settings live in NVS and survive reflashing.
- **secrets.h** — `cp secrets.h.example secrets.h` and fill in the SSID and
  password. Faster on the bench. NVS always wins over it.

Nothing else needs configuring: the lights and camera servers ask for no
credential, and their addresses are compiled in from `board_config.h`.

## Build and flash

Open `control.ino` in the Arduino IDE and upload. Serial monitor at **115200**.

Expected serial output:

```
[boot] CrowPanel ESP32 HMI 7.0" (DIS08070H)
[boot] PSRAM: 8192 KB
[boot] wifi 192.168.1.x
[lights] 7 known
[cam] 3 cameras
[mqtt] connected
```

Expected on screen: two tabs. **Luces** fills with one button per bulb, amber
when on; tapping one toggles the real light and the button follows a moment
later when the new state arrives over MQTT — it is deliberately not optimistic,
so the panel never claims a bulb changed when it did not. **Camaras** lists the
cameras; tapping one shows a still that refreshes every five seconds.

## Troubleshooting

| Symptom | Cause |
|---|---|
| No COM port; *Other Devices* in Device Manager | CH340 driver not installed — see step 1 |
| Blank/black screen, `no PSRAM detected` | PSRAM not set to OPI PSRAM |
| Boot loop or garbled behaviour | Flash Size left at 16 MB |
| Image tears, shimmers, or is horizontally skewed | Pixel clock too high — drop `LCD_PCLK_HZ` to `16000000` in `board_config.h` |
| Everything works except touch | **USB CDC On Boot** enabled — it holds GPIO19/20, which this board uses for touch I2C |
| Display works, touch dead (CDC already disabled) | V1.0/V2.0 board — set `BOARD_HAS_PCA9557` to `0` |
| Touch mirrored or rotated | Swap the `TOUCH_MAP_*` pairs in `board_config.h` |
| Upload fails to sync | Hold **BOOT**, tap **RESET**, release BOOT, then upload |
| `Bus already started in Master Mode` warning | Harmless, ignore |

## Layout

```
control/
├── control.ino       setup()/loop(), LVGL wiring, the two tabs. No hardware knowledge.
├── lights.h/.cpp     MQTT state + toggle, with the HTTP boot snapshot.
├── cameras.h/.cpp    Camera list, snapshot fetch, JPEG decode into a canvas.
├── portal.h/.cpp     WiFi provisioning. A COPY — see ../README.md.
├── board_config.h    Every GPIO, panel timing value, and service address.
├── display.h         DisplayDriver interface — the seam between UI and hardware.
├── display_rgb.cpp   CrowPanel 7.0" implementation (LovyanGFX + GT911 + PCA9557).
└── secrets.h.example WiFi credential template.
```

## Swapping the panel

Add a `display_*.cpp` implementing `DisplayDriver` and change which instance
`board_display()` returns; `control.ino` reads geometry, colour format and
render mode off the driver, so it needs no changes.

> This interface was originally written expecting the e-ink panel to become a
> second `DisplayDriver` here. **It didn't.** The menu dashboard turned out to
> want a different MCU (ESP32-C3), no touch, no LVGL and server-side rendering,
> so it lives on its own in [`../menu/`](../menu/) with nothing shared. The
> seam is still the right shape for swapping *this* panel for another colour
> touch panel — it just isn't the path to e-ink.

## What to verify first

The display layer below has run on this hardware. Everything added on top of it
has not, so in rough order of how likely each is to be wrong:

1. **JPEG byte order.** `camerasBegin()` sets `TJpgDec.setSwapBytes(true)`,
   which is right for TFT_eSPI's convention and may be backwards for a raw LVGL
   RGB565 buffer. Blue faces and orange sky mean flip it.
2. **`MQTT_BUFFER_BYTES`.** The retained whole-house state message is several KB
   and PubSubClient drops anything past its buffer **silently** — the symptom is
   a panel that subscribes successfully and never receives. Raise it if `[lights]`
   only ever populates from HTTP.
3. **The LVGL 9.1 canvas API.** `lv_canvas_get_draw_buf()` and the `stride`
   field are 9.1-era; 9.2 moved them.
4. **Grid sizing on 800×480.** Twelve light buttons at three across assumes the
   tab bar leaves ~400 px; more bulbs than that and the grid needs to scroll.

## Verified against

Pin assignments and board settings are taken from Elecrow's own documentation
rather than guessed:

- [CrowPanel ESP32 HMI 7.0" wiki](https://www.elecrow.com/wiki/esp32-display-702727-intelligent-touch-screen-wi-fi26ble-800480-hmi-display.html) — backlight, SD, I2S, touch I2C pins
- [7.0-inch HMI Arduino tutorial](https://www.elecrow.com/wiki/ESP32_Display_7.0-inch_HMI_Arduino_Tutorial.html) — RGB data pin order, sync pins, board settings, library list
- [Elecrow-RD/CrowPanel-7.0-HMI-ESP32-Display-800x480](https://github.com/Elecrow-RD/CrowPanel-7.0-HMI-ESP32-Display-800x480) — official examples, PCA9557 library, hardware revision notes
- [Elecrow forum: DIS08070H needs USB drivers](https://forum.elecrow.com/discussion/1069/usb-drivers-for-the-esp32-7-inch-display) — identifies the bridge chip as CH340
- [esphome/esphome#15356](https://github.com/esphome/esphome/issues/15356) — I2C on GPIO19/20 vs USB_SERIAL_JTAG on this exact board

Two details are **not** independently confirmed and are the first things to
suspect if bring-up misbehaves: the exact PCA9557 IO0/IO1 reset sequence, and
whether this board wants 16 MHz or 24 MHz pixel clock (Elecrow's tutorial says
24, most community configs use 16, so the sketch defaults to 16).
