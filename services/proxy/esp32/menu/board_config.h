// Hardware pin map for the Minuta panel.
//
// MCU:     ESP32-C3 (SuperMini or equivalent) - RISC-V, no PSRAM, WiFi 4
// Display: WeAct Studio 4.2" e-paper, 400x300 black/white
//
// This is the ONLY file that should need editing for a different board or a
// different 4.2" panel. See README.md for the Arduino IDE board settings.
#pragma once

#define BOARD_NAME "ESP32-C3 + WeAct 4.2\" e-paper"

// ---------------------------------------------------------------------------
// Panel geometry
// ---------------------------------------------------------------------------
// Must match the server's MINUTA_W/MINUTA_H in HomeCore's app.py. The firmware
// refuses to draw a payload that isn't exactly PANEL_BYTES long rather than
// blitting a misaligned image, so a mismatch shows up in the serial log
// instead of as a scrambled screen.
#define PANEL_WIDTH  400
#define PANEL_HEIGHT 300
#define PANEL_BYTES  ((PANEL_WIDTH / 8) * PANEL_HEIGHT)   // 50 * 300 = 15000

// ---------------------------------------------------------------------------
// Panel controller
// ---------------------------------------------------------------------------
// WeAct has shipped the 4.2" module with more than one controller. The common
// current one is the SSD1683 (GxEPD2_420_GDEY042T81); older stock used the
// UC8176 (GxEPD2_420, GDEW042T2).
//
// **This is the single most likely thing to be wrong on first flash**, and the
// symptom is unambiguous: the panel stays blank or shows noise while the serial
// log reports a clean fetch and draw. Swap the two lines below if so.
#define PANEL_CLASS GxEPD2_420_GDEY042T81      // SSD1683 - current WeAct stock
// #define PANEL_CLASS GxEPD2_420              // UC8176 (GDEW042T2) - older

// ---------------------------------------------------------------------------
// Display wiring (the module's 8-pin header)
// ---------------------------------------------------------------------------
// Header order on the WeAct board: BUSY RES D/C CS SCL SDA GND VCC.
// The last two are power, so six GPIOs are needed.
//
// The C3's GPIO matrix means SPI can go on almost any pin, but three are worth
// avoiding and are left free here: GPIO2, GPIO8 and GPIO9 are strapping pins
// sampled at reset (GPIO9 is the BOOT button), and a display pulling one of
// them can stop the board from booting or from entering download mode.
// GPIO20/21 are the UART the serial monitor uses.
#define EPD_BUSY  3
#define EPD_RST  10
#define EPD_DC    5
#define EPD_CS    7
#define EPD_SCK   4    // SCL on the module silkscreen
#define EPD_MOSI  6    // SDA on the module silkscreen

// ---------------------------------------------------------------------------
// Timing
// ---------------------------------------------------------------------------
// How long to wait for the network before giving up on this wake. Every second
// here is spent with the radio on, which is the single biggest draw on the
// battery, so this is deliberately short - a missed hour is invisible on a
// weekly menu, and the next wake will catch up.
#define WIFI_TIMEOUT_MS  15000
#define HTTP_TIMEOUT_MS  15000

// Fallback sleep when the server can't be reached and therefore couldn't say
// how long to sleep. Shorter than a normal poll so a router reboot doesn't cost
// a whole day, long enough that a server outage doesn't flatten the cell.
#define RETRY_SLEEP_SECONDS  (15 * 60)

// Sleep used when the server's X-Sleep-Seconds is missing or unparseable.
#define DEFAULT_SLEEP_SECONDS (60 * 60)

// Clamp on whatever the server asks for, so a bad header can't strand the panel
// asleep for a week or spin it awake continuously.
#define MIN_SLEEP_SECONDS  60
#define MAX_SLEEP_SECONDS  (12 * 60 * 60)

// ---------------------------------------------------------------------------
// Static IP (optional)
// ---------------------------------------------------------------------------
// DHCP costs roughly a second of radio time on every single wake, and radio
// time is where the battery goes. Uncomment to skip it. The address must sit
// outside the router's DHCP pool.
//
// Lives here rather than in secrets.h because it is not a secret, and because
// secrets.h is optional while this file is always present.
//
// #define USE_STATIC_IP
// #define STATIC_IP       192, 168, 1, 60
// #define STATIC_GATEWAY  192, 168, 1, 1
// #define STATIC_SUBNET   255, 255, 255, 0
// #define STATIC_DNS      192, 168, 1, 1

// ---------------------------------------------------------------------------
// Config portal
// ---------------------------------------------------------------------------
// The panel has no touchscreen and no buttons besides the board's own RESET, so
// provisioning happens over a temporary access point. See portal.cpp.
//
// **AP mode costs ~120 mA with the radio never sleeping** - roughly three days
// of normal operation for ten minutes of portal. Every constant here exists to
// bound that, and none of them should be raised casually.

// WPA2 passphrase for the panel's own AP. Minimum 8 characters. Not a secret in
// any strong sense (it is printed on the panel's screen while the AP is up),
// but an open AP would let anyone in range read the device token off the form.
#define PORTAL_AP_PASSWORD "alfred1234"

// How long the portal stays up before giving up and sleeping. Ten minutes is
// plenty when you are standing in front of it, and it is the ceiling on what a
// stray double-tap of RESET can cost.
#define PORTAL_TIMEOUT_MS (10UL * 60 * 1000)

// Consecutive failed cycles before assuming the network changed and offering
// the portal unprompted. At RETRY_SLEEP_SECONDS this is about five hours of
// failure - long enough that a router reboot or a brief outage never triggers
// it, short enough to self-recover the day someone changes the WiFi password.
#define PORTAL_AFTER_FAILURES 20

// Window for the second tap of the double-RESET gesture. Only ever waited on
// boots a human caused; a timer wake never pays this.
#define DRD_WINDOW_MS 3000
