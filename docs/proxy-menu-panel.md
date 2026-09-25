# Minuta panel — kitchen weekly-menu dashboard

A battery e-ink panel on the kitchen wall showing the week's *minuta* (lunch and
dinner, seven days, today's row inverted). It is the first of the two ESP32
projects in this repo; the second is the full voice/touch Alfred panel in
[`esp32/control/`](../services/proxy/esp32/control/), which shares no code with it.

This document is the cross-repo view and the reasoning. The how-to lives in
[`esp32/menu/README.md`](../services/proxy/esp32/menu/README.md) — wiring, board settings,
flashing, power, mounting, troubleshooting.

## Shape of it

```
HomeCore (Pi, local/app.py)                     ESP32-C3 on the wall
┌────────────────────────────────┐             ┌──────────────────────┐
│ menu.db  (Minuta semanal)      │             │  wake                │
│   ↓                            │             │   ↓                  │
│ _menu_render()  PIL          │  15000 B    │  GET render.bin      │
│   → 400×300 1-bit bitmap       │ ──────────► │   ↓                  │
│ ETag = sha256(pixels)          │  ◄────────  │  If-None-Match       │
│ X-Sleep-Seconds                │   304 or    │   ↓                  │
└────────────────────────────────┘   200       │  blit → deep sleep   │
                                               └──────────────────────┘
```

The device does **no layout and owns no clock**. It fetches finished pixels and
sleeps for exactly as long as the server tells it to.

## The four decisions

### 1. Its own sketch, not a second `DisplayDriver` behind alfred

`esp32/control/display.h` was written expecting e-ink to arrive as a second
implementation behind its LVGL abstraction. It didn't, and the interface's
header comment now says so.

The dashboard wanted a different MCU (C3: single-core RISC-V, no PSRAM, against
alfred's assumption of 8 MB PSRAM for a 750 KB framebuffer), no touch, no
animation and a handful of redraws a day. Sharing the abstraction would have
meant carrying LVGL onto the C3 for no benefit. Two small sketches beat one that
has to be two things.

### 2. The server renders, the device blits

HomeCore produces the finished 400×300 1-bit bitmap; the firmware calls
`drawImage()` on it. Layout, fonts, wrapping, Spanish accents and the
today-highlight are all Python.

This means **changing how the panel looks is a redeploy, not a reflash** — which
matters a great deal for a thing bolted to a wall behind a battery. Iteration
happens in a browser tab against `/menu/api/render.png`, the same pixels as PNG.

The cost is a server dependency: the panel is useless without HomeCore. Given
the panel's entire content *is* HomeCore data, that dependency already existed.

### 3. A device token, not proxy-auth

`/menu/api/*` already accepts the trusted-proxy headers used by Alfred, and the
codebase has a convention for derived per-identity tokens. Neither fits: proxy
auth runs through `find_user()`, so the panel would have needed a real
`users.json` entry, and would then have surfaced as a person in task-assignment
lists and share pickers.

`MENU_DEVICE_TOKEN` is scoped to the two read-only render routes and keeps the
wall panel out of the people-shaped parts of the app.

### 4. Sleep policy on the server

`X-Sleep-Seconds` is computed in `_menu_sleep_seconds()` from `TASKS_TZ` and
returned on every response, including 304s. The device has no RTC, no timezone
and no date handling at all — it obeys the number.

This is what enforces the 23:00–06:00 quiet window, and it means cadence is
retunable from HomeCore's environment with no reflash. It also lets the server
skip a pointless wake: if the next ordinary poll would land inside the quiet
window, it returns the *morning* instead of waking the panel at 23:15 only to
tell it to go back to sleep.

## Server side (HomeCore)

All in `local/app.py`, in the Minuta section after the existing `/menu/api/*`
routes.

| | |
|---|---|
| `GET /menu/api/render.bin` | Packed 1-bit bitmap, exactly 15000 bytes |
| `GET /menu/api/render.png` | Same pixels as PNG, for browser iteration |
| Auth | `X-Device-Token`, or an ordinary session (so preview works logged in) |
| `ETag` | sha256 of the *pixels* — stable across restarts and redeploys |
| `X-Sleep-Seconds` | How long to sleep; sent on 200 and 304 alike |
| `X-Rendered-At` | Debug only. Deliberately a header, never drawn |

`_menu_week_days()` was extracted so the renderer and `menu_api_list()` build
the week from one place and cannot drift.

### The rule that is easy to break

**Nothing time-varying may be drawn.** The ETag hashes the pixels, and a 304 is
what lets the device skip its refresh. An "actualizado 14:32" stamp would change
the hash every wake and force a full e-ink redraw every hour, forever. That is
why the render time is a response header.

### Environment

```
MENU_DEVICE_TOKEN=<openssl rand -hex 32>   # unset = session-only, preview still works
MENU_POLL_MINUTES=60
MENU_QUIET_START=23
MENU_QUIET_END=6
```

Pillow ≥10.1 and DejaVu were already present for `/chat/map`; the renderer
reuses that font resolution via `_map_font()`, adding only a bold-face lookup.

## Firmware (`esp32/menu/`)

```
menu.ino        boot flow, config screen, fetch, blit, sleep
board_config.h    pins, geometry, timing, portal limits, static IP
portal.h/.cpp     AP provisioning + NVS settings
secrets.h.example optional compile-time defaults
```

Wake → WiFi → conditional GET → blit if changed → deep sleep. The ETag lives in
RTC memory across sleeps, behind a magic value because RTC memory holds garbage
after a cold boot.

The payload format needs no negotiation: PIL mode `1` and GxEPD2's internal
buffer share a convention (MSB-first, 0 = black), and 400/8 = 50 bytes per row
exactly, so there is no row padding. The bytes go straight from socket to panel.
A length that isn't 15000 is refused rather than blitted — a scrambled screen is
a bad way to discover the geometry has diverged.

### Provisioning

No touchscreen, no buttons but RESET, no reachable serial once mounted. So the
panel becomes an AP and prints the join instructions **on its own screen**.
Settings go to NVS, which outlives sleep, power loss and reflashing.

Opens on: nothing stored, double-tapped RESET, or 20 consecutive failures.

AP mode is the most expensive state this device has — ~120 mA with the radio
never sleeping, so ten minutes of portal costs about three days of normal
operation. Hence: it always times out; the double-reset window is only waited on
boots a human caused (a timer wake never pays it); and a timed-out portal clears
the failure counter before a long sleep, without which a panel whose network had
truly vanished would reopen the portal every wake and flatten the cell in an
afternoon.

Only 200/304 count as success. A wrong token, a moved endpoint and a vanished
SSID all leave the same blank wall and all three are portal-fixable, so all
three accumulate.

## Hardware

| | |
|---|---|
| MCU | ESP32-C3 SuperMini |
| LDO | `4A2D` — 3.3 V/500 mA SOT-23-5, 50 µA Iq, 100 mV dropout, 6 V max |
| Display | WeAct 4.2" e-paper, 400×300 b/w, 8-pin SPI header (no touch) |
| Battery | 902030 LiPo, 3.7 V 500 mAh 1.85 Wh |
| Charger | LX-LCBST (TP4056 + boost) — **charging half only** |

### Power, in order of what matters

1. **Bypass the LX-LCBST's boost.** B+/B− straight to the C3's 5V pin,
   `OUT+/OUT−` unconnected. A boost idles at 1–10 mA against a ~55 µA sleeping
   device, and many auto-shut-off when they see no load — which looks exactly
   like a firmware bug. The `4A2D`'s 100 mV dropout is what makes feeding a
   4.2 V cell into the "5V" pin work well rather than merely work.
2. **Charge current.** TP4056 ships at 1000 mA = 2C on this cell. Swap `R3` to
   4.7 kΩ for 250 mA (0.5C). `I(mA) = 1200 / R(kΩ)`.
3. **Remove the power LED.** ~300 µA → ~55 µA.
4. **Poll less.** `MENU_POLL_MINUTES`, server-side.

Budget at hourly polling, 06:00–23:00 (17 wakes), ~425 mAh usable:

| | |
|---|---|
| Per wake | ~0.12 mAh — dominated by WiFi association, not the fetch |
| Active | ~2.2 mAh/day |
| Sleep, LED removed (~55 µA) | ~1.3 mAh/day |
| **Life** | **~4 months**; ~9 at four polls/day |

Note the 304 saves the e-ink refresh but *not* the association, which is the
larger cost. Its value is no visible flash and no ghosting every hour.

## Status

Working and verified:

- Renderer — 15000 bytes exactly, accents intact, today-row inverted, ellipsis
  truncation, checked by rendering the real `_menu_render()` against stub data.
- `_menu_sleep_seconds()` across the 22:00–06:00 boundary, including the
  skip-the-23:00-wake case.

Not yet run on hardware:

- The whole firmware. Nothing has been flashed.
- `PANEL_CLASS` — WeAct has shipped the 4.2" with both SSD1683 and UC8176.
  Defaults to SSD1683; the alternative is one commented line away. Symptom of a
  wrong guess is a blank panel with a clean serial log.
- The config portal. **Run it on USB first** — a portal that fails to time out
  is the one bug here that could empty the cell unattended.
- Real deep-sleep current of this particular board.
