# Minuta panel (ESP32-C3 + 4.2" e-paper)

A battery e-ink panel for the kitchen wall showing the week's *minuta* — lunch
and dinner for all seven days, with today's row inverted.

**The firmware draws nothing.** HomeCore renders the week to a 400×300 1-bit
bitmap and this sketch blits the bytes. Changing the layout is a HomeCore
redeploy, not a reflash — see [Changing how it looks](#changing-how-it-looks).

```
wake -> WiFi -> GET /menu/api/render.bin -> blit -> deep sleep
```

## Hardware

| | |
|---|---|
| MCU | ESP32-C3 (SuperMini or equivalent) — RISC-V, 400 KB SRAM, no PSRAM |
| Display | WeAct Studio 4.2" e-paper, 400×300 black/white |
| Payload | 15000 bytes (400/8 × 300), packed MSB-first, 0 = black |

### Wiring

The module's 8-pin header is `BUSY RES D/C CS SCL SDA GND VCC` — six signals
plus power. Pins are set in [`board_config.h`](board_config.h) and the C3's GPIO
matrix means almost any of them will do.

| Module pin | ESP32-C3 |
|---|---|
| BUSY | GPIO 3 |
| RES  | GPIO 10 |
| D/C  | GPIO 5 |
| CS   | GPIO 7 |
| SCL  | GPIO 4 |
| SDA  | GPIO 6 |
| GND  | GND |
| VCC  | 3V3 |

> **Don't move these onto GPIO 2, 8 or 9.** All three are strapping pins sampled
> at reset (GPIO 9 is the BOOT button), and a display holding one of them can
> stop the board booting or entering download mode — which reads as a bricked
> board. GPIO 20/21 are the serial UART.

## Arduino IDE setup

### 1. ESP32 core

Install **esp32 by Espressif Systems** (3.x) via Boards Manager.

### 2. Board settings

Select **ESP32C3 Dev Module**, then:

| Setting | Value |
|---|---|
| USB CDC On Boot | **Enabled** |
| Flash Size | 4MB (32Mb) |
| Partition Scheme | Default 4MB with spiffs |
| Upload Speed | 921600 |

**USB CDC On Boot must be Enabled** on a C3 SuperMini — unlike the CrowPanel in
[`../control/`](../control/), it has no USB-UART bridge chip and serial comes over
the C3's native USB. With CDC off you get a COM port that never prints anything,
which looks exactly like a dead sketch.

### 3. Libraries

| Library | Source |
|---|---|
| `GxEPD2` | Library Manager (Jean-Marc Zingg) |
| `Adafruit GFX Library` | Library Manager — pulled in by GxEPD2 |

No LVGL. The panel is static, has no touch, and redraws a few times a day; a
15 KB framebuffer and `drawImage()` cover the whole job.

The config portal uses `WebServer`, `DNSServer` and `Preferences`, all of which
ship with the ESP32 core — no WiFiManager or other external dependency. Rolling
it by hand is what makes the AP timeout and the entry conditions exact, and
those are the parts that decide whether the battery survives a router outage.

### 4. `secrets.h` — optional

```bash
cp secrets.h.example secrets.h
```

Fill in WiFi, the LAN URL of HomeCore, and `MENU_DEVICE_TOKEN`. Also worth
enabling the static-IP block in `board_config.h`: DHCP costs about a second of
radio on *every* wake, and radio time is the battery.

**This file is optional.** Without it the sketch still compiles, and the panel
opens its [config portal](#wifi-setup-the-config-portal) on first boot instead.
`secrets.h` just pre-provisions a panel, which is quicker on the bench and no
help at all once it's on a wall.

Note the precedence: these are **defaults only**. Anything saved through the
portal goes to NVS and wins, so editing and reflashing `secrets.h` will not
override a panel that's been configured over the air — erase flash for that.

## Server side

Generate a token and put it in HomeCore's `.env`:

```bash
openssl rand -hex 32
# -> MENU_DEVICE_TOKEN=<that value> in HomeCore/local/.env
cd HomeCore/local && docker compose up -d --force-recreate
```

Then confirm the endpoint works from a machine on the LAN, before involving the
hardware at all:

```bash
curl -sk -H "X-Device-Token: <token>" \
     -D- -o /tmp/menu.bin \
     https://hub.home:21001/menu/api/render.bin
# expect: 200, Content-Length: 15000, ETag, X-Sleep-Seconds
```

Optional tuning, all in HomeCore's environment — **no reflash needed**, the
device just obeys what it's told:

| Variable | Default | Meaning |
|---|---|---|
| `MENU_POLL_MINUTES` | `60` | How often to check while awake hours last |
| `MENU_QUIET_START` | `23` | Start of the overnight sleep, local time |
| `MENU_QUIET_END` | `6` | End of it |

## WiFi setup: the config portal

The panel has no touchscreen, no buttons of its own, and no reachable serial
console once it's mounted. So to configure it, it briefly becomes an access
point and serves a form, and it prints the instructions **on its own screen**:

```
MINUTA
Configuracion de red
────────────────────────────
1. Conecta el telefono a:
     Minuta-A4B2
     Clave: alfred1234

2. Abre en el navegador:
     192.168.4.1

El panel vuelve a dormir en 10 minutos.
```

Joining that AP and opening any URL lands on the form (it runs a captive-portal
DNS, so most phones pop it up by themselves). The form sets the WiFi network,
the server URL and the device token. Save, and the panel reboots into normal
operation.

Settings live in NVS — they survive deep sleep, power loss and reflashing.

### How it decides to open

| Trigger | When |
|---|---|
| Nothing stored | First boot of a panel with no `secrets.h` |
| **Double-tap RESET** | Any time you want it — the normal way in |
| 20 consecutive failures | ~5 hours of failing; recovers a changed WiFi password unattended |

Double-tapping RESET is the everyday gesture: press it twice within 3 seconds.
The 3-second window is only ever waited on boots a *human* caused — a timer wake
skips it entirely, because charging every hourly wake 3 seconds for a gesture
nobody made would be pure waste.

> **The portal is the most expensive thing this device does.** The radio runs
> flat out at ~120 mA and never sleeps: ten minutes of portal costs roughly
> three days of normal operation. That's why it always times out, why it's
> never entered speculatively, and why a timed-out portal clears the failure
> counter before sleeping — without that last part, a panel whose network had
> genuinely vanished would reopen the portal on every wake and flatten the cell
> in an afternoon.

### What counts as a failure

Only a *usable* answer resets the counter — HTTP 200 or 304. A wrong token
(401), a moved endpoint (404) and a vanished SSID all leave the same blank wall
and all three are portal-fixable, so all three count. Treating any HTTP response
as success would strand a panel with a mistyped token forever, and silently.

### The AP password

`alfred1234` by default, in `board_config.h`. It isn't much of a secret — it's
printed on the panel's screen while the AP is up — but an open AP would let
anyone in range read your device token straight off the form.

## Changing how it looks

Open `https://hub.home:21001/menu/api/render.png` in a browser while logged in.
It's the same pixels as the panel gets, as a PNG. Edit `_menu_render()` in
HomeCore's `app.py`, redeploy, refresh the tab. The panel picks the change up on
its next wake.

One rule when editing it: **never draw anything that varies with time.** The
ETag is a hash of the pixels, and a "last updated 14:32" stamp would change it
on every single wake, forcing a full e-ink refresh every hour forever. The
render timestamp is already in the `X-Rendered-At` response header.

## Build and flash

Open `menu.ino` and upload. Serial monitor at **115200**.

```
[boot] cold start
[boot] ESP32-C3 + WeAct 4.2" e-paper
[boot] wake #1, heap 268412
[wifi] connecting to <ssid>
[wifi] ok 192.168.1.60 in 2140 ms, rssi -58
[http] 200
[draw] 15000 bytes, etag "a3f1..."
[draw] done in 2870 ms
[sleep] 3600 s (1.00 h)
```

On the next wake, with the menu unchanged:

```
[http] 304
[draw] unchanged, skipping refresh
[sleep] 3600 s (1.00 h)
```

## Powering it

**Do not run the C3 off a boost converter's output.** This is the single
decision that decides whether the panel lasts weeks or days, and it is easy to
get wrong because the obvious wiring is the bad one.

The LX-LCBST is a TP4056 charger *and* a DC-DC step-up in one board. Its
charging half is exactly right. Its boost half is not:

- A cheap boost runs continuously and idles at ~1–10 mA. Against a deep-sleep
  load of ~50 µA that is 20–200× the entire rest of the budget — it turns 10
  weeks into 2 days.
- Boosting 3.7 V → 5 V only for the C3's onboard LDO to drop it back to 3.3 V
  burns a third of the pack in two needless conversions.
- Many such modules watch for a load and **switch off when they see none**. A
  sleeping C3 looks like no load, so the panel goes dark and never wakes — a
  failure that looks like a firmware bug and isn't.

Use it as a charger only, and take the battery voltage straight to the C3's
onboard regulator:

```
   USB-C ──> LX-LCBST  (charging half only)
                 B+ ──┬──> C3 SuperMini  5V pin
                 B- ──┼──> C3 SuperMini  GND
                      └──> LiPo cell
             OUT+/OUT- ──> leave unconnected
```

Feeding ~3.5–4.2 V into the pin labelled *5V* is correct here: it is the LDO's
input, and a low-dropout part regulates 3.3 V from anything above ~3.45 V —
which is most of a LiPo's usable curve. Below that the board browns out, which
lands above the ~3.0 V cell-damage threshold and so doubles as a crude
low-voltage cutoff. It is not a protection circuit, though: use a cell with a
protection PCB on the tab (most small pouch cells have one under the tape at
the tab — check before trusting it).

### Set the charge current before the first charge

> **A TP4056 board ships set to 1000 mA.** The cell here is a 902030 pouch —
> 9 × 20 × 30 mm, 500 mAh, 1.85 Wh — so 1 A is a 2C charge, roughly four times
> the 0.5C these pouches are specified for. Not the instant hazard that rate
> would be on a smaller cell, but it runs the pouch hot and takes cycle life
> off it for no benefit whatsoever on something that charges every few months.

Charge current is set by the programming resistor (`R3`/`Rprog` on most TP4056
boards, usually marked `122` = 1.2 kΩ):

```
I_charge(mA) = 1200 / R_prog(kΩ)
```

| Target | R_prog | Marking | Rate on a 500 mAh cell |
|---|---|---|---|
| **250 mA** | 4.7 kΩ | `472` | 0.5C — the datasheet figure, ~2.5 h charge |
| 500 mA | 2.4 kΩ | `242` | 1C — acceptable, warmer |
| 1000 mA (as shipped) | 1.2 kΩ | `122` | 2C — leave this behind |

Swap it for a 4.7 kΩ 0805/0603 before connecting the battery.

### Check the regulator before trusting any estimate

Look at the LDO on your SuperMini:

| Marking | Package | Quiescent | Verdict |
|---|---|---|---|
| `4A2D` | SOT-23-5 | ~50 µA | fine — this is what our board has |
| `ME6211`, `XC6206`, `6206` | tiny SOT-23 | ~40 µA | fine |
| `AMS1117` / `1117` | large SOT-223 | **~5 mA** | replace it or pick another board |

Ours is `4A2D` (Shenzhen Fuman Elec): 3.3 V, 500 mA, 50 µA Iq, 100 mV dropout
at 100 mA, **6 V absolute maximum input**. The low dropout is what makes the
battery-direct wiring above work — it holds 3.3 V until the cell reaches about
3.5 V under WiFi load. The 6 V ceiling is worth remembering if anyone is ever
tempted to feed the 5V pin from something unregulated.

An AMS1117 alone burns ~120 mAh/day. On a 500 mAh cell that is a flat battery
every three days no matter what the firmware does — the LED is not the problem on
those boards.

## Battery

Rough per-day budget at hourly polling, 06:00–23:00 (17 wakes):

| | |
|---|---|
| One wake, no change | ~0.12 mAh (dominated by WiFi association, not the fetch) |
| One wake with redraw | ~0.15 mAh |
| Active total | ~2.2 mAh/day |
| Deep sleep, bare C3 module @ ~10 µA | ~0.24 mAh/day |
| Deep sleep, SuperMini with the LED removed @ ~50 µA | ~1.2 mAh/day |
| Deep sleep, SuperMini as shipped (power LED lit) @ ~300 µA | ~7.2 mAh/day |
| Deep sleep, board using an AMS1117 LDO @ ~5 mA | ~120 mAh/day |

On a **902030 cell (500 mAh, ~425 mAh usable** once the LDO's ~3.45 V brownout
is accounted for):

| Board condition | Life at hourly polling |
|---|---|
| SuperMini as shipped (power LED lit) | ~6 weeks |
| **Power LED removed** | **~4 months** |
| Board using an AMS1117 | ~3 days |

Note the cell code is its *dimensions*, not its capacity — `902030` is
9 × 20 × 30 mm. Here the stated 500 mAh is consistent with the stated 1.85 Wh
(500 mAh × 3.7 V), so it's a real figure rather than a rounded-up listing.

Measure your own board before trusting any of this. Deep-sleep current varies
more between revisions than anything in this firmware does, and it is the only
number that decides the answer.

The changes that matter, in order — note the firmware knob is last, because it
is worth the least:

1. **Bypass the boost converter** (above). Worth more than everything else
   combined, and costs nothing but a different wire.
2. **Check the LDO** (above). An AMS1117 caps you at ~2 days regardless.
3. **Desolder the power LED.** ~300 µA → ~50 µA; roughly 6 weeks → 4 months.
4. **Poll less** — `MENU_POLL_MINUTES=240` is four checks a day on a menu
   that changes weekly. Once the LED is gone the radio is the larger half of
   the budget, so this is the remaining lever: ~4 months becomes **~9**.
   Server-side, no reflash.

Note that the 304 path saves the e-ink refresh but *not* the WiFi association,
which is the larger cost. Its real value is avoiding a visible flash and panel
ghosting every hour, not battery life.

## Mounting

**Magnets are fine.** E-ink is *electro*phoretic — charged white titanium-dioxide
and black carbon particles moved by an electric field from the driver
electrodes. Neither pigment is ferromagnetic, so a magnet does nothing to the
image whether the panel is powered, refreshing or hibernating. (The intuition
usually comes from Magna Doodle toys, which really are magnetic, or from
e-readers sleeping when a cover closes — that's a Hall sensor in the cover, and
this bare module has none.) Sticking it to the fridge is fine.

What actually needs care:

**Keep steel away from the antenna.** A fridge door is a large steel plane, and
the C3's antenna sits right at the end of the board. Mounted flat against it,
the antenna detunes and the signal drops — the classic "worked perfectly on the
bench" failure. Let the module's antenna end overhang the edge of the backing
plate, or space it off the metal. The firmware logs RSSI on every wake:

```
[wifi] ok 192.168.1.60 in 2140 ms, rssi -58
```

Check that number before and after mounting. Losing more than ~10 dB means the
antenna is too close to the steel, and worse than about −80 dB will start
costing failed wakes — each one a wasted radio-on.

**Spread the load on the glass.** The panel is a thin glass laminate and it is
the fragile part of the whole build. Magnets at the corners of a backing plate,
never one strong magnet clamping through the panel. Point pressure cracks it,
and a cracked e-ink panel is a permanent black blot.

**Never compress the LiPo.** Also mechanical, not magnetic. Give the 902030
pouch its own recess or tape it flat beside the board — don't let a magnet
sandwich squeeze it. A dented or punctured pouch is the one genuinely dangerous
failure mode here.

**Not above the oven, not in direct sun.** E-ink's rated operating range is
roughly 0–50 °C. Past that you get sluggish, ghost-prone refreshes and
eventually permanent damage.

### When the battery dies

E-ink holds its image with no power at all, so a flat cell leaves the last menu
on the wall indefinitely — the panel fails to a confident-looking but stale
display rather than going blank.

That's why the header draws the week range (`10 – 16 ago`) even though it costs
pixels: it is the only thing distinguishing "this week's menu" from "the menu
from whenever the battery died". Don't remove it when editing the renderer.

## Troubleshooting

| Symptom | Cause |
|---|---|
| COM port appears, serial prints nothing | **USB CDC On Boot** disabled — the C3 has no UART bridge |
| Panel blank/noise, serial shows a clean fetch and draw | Wrong controller — swap `PANEL_CLASS` in `board_config.h` (SSD1683 ↔ UC8176) |
| Image is photographically inverted | Flip the `invert` argument in `drawBitmap()` |
| `length N, expected 15000 - not drawing` | Server geometry and `board_config.h` disagree — check `MENU_W/H` in app.py |
| `[http] 401` | Token differs between the panel and HomeCore's env, or is unset server-side. Double-tap RESET and re-enter it |
| Reflashed `secrets.h`, panel ignores it | NVS wins over compile-time defaults — use the portal, or erase flash |
| Portal never appears on double-tap | Taps more than `DRD_WINDOW_MS` (3 s) apart, or the first press was a timer wake rather than a reset |
| `[wifi] timeout` every wake | 2.4 GHz only on a C3; check the AP isn't 5 GHz-only or using WPA3-only |
| Worked on the bench, flaky once mounted | Antenna too close to steel — see [Mounting](#mounting), compare the logged RSSI |
| Panel shows an old week and never updates | Flat battery; e-ink keeps the last image indefinitely |
| Board won't enter download mode | Something wired to GPIO 2/8/9 — see the wiring warning |
| Panel never updates but serial says 304 forever | Stale RTC ETag; power-cycle (not reset) to clear it |

## Verified against

- Payload format and headers: `_menu_response()` / `_menu_render()` in
  HomeCore's `local/app.py`, rendered and byte-checked at 15000 bytes.
- Sleep-window behaviour: `_menu_sleep_seconds()`, checked across the
  22:00–06:00 boundary including the skip-the-23:00-wake case.

- LDO: board marking reads `4A2D` — Shenzhen Fuman Elec, 3.3 V / 500 mA
  SOT-23-5, 50 µA Iq, 100 mV dropout, 6 V max in.

Not independently confirmed, and the first things to suspect during bring-up:

- the exact WeAct controller variant (`PANEL_CLASS`);
- the real deep-sleep current of your particular C3 board;
- **the config portal has not yet been run on hardware.** The logic is
  reviewed but unflashed. First run it on USB, not on battery — a portal that
  fails to time out is the one bug here that could empty the cell unattended.
