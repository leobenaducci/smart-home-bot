// Hardware pin map for the Elecrow CrowPanel ESP32 HMI 7.0" (DIS08070H).
//
// Module:  ESP32-S3-WROOM-1-N4R8  (4 MB flash, 8 MB OPI PSRAM)
// Panel:   800x480 IPS, 16-bit RGB565 parallel, EK9716BD3 + EK73002ACGB
// Touch:   GT911 capacitive over I2C
//
// This is the ONLY file that should need editing when moving to a different
// panel. See README.md for the Arduino IDE board settings, which matter just
// as much as these pins.
#pragma once

#define BOARD_NAME "CrowPanel ESP32 HMI 7.0\" (DIS08070H)"

// ---------------------------------------------------------------------------
// Board revision
// ---------------------------------------------------------------------------
// Elecrow shipped V1.0 / V2.0 / V3.0 of this panel. V3.0 added a PCA9557 I/O
// expander that gates the GT911 reset line; without the reset sequence in
// display_rgb.cpp, touch silently fails to initialise on V3.0 boards while the
// display itself works fine. The revision is silkscreened on the back of the
// driver board.
//
// Set to 0 if you have a V1.0/V2.0 board and touch stops working.
#define BOARD_HAS_PCA9557 1

// ---------------------------------------------------------------------------
// Panel geometry
// ---------------------------------------------------------------------------
#define PANEL_WIDTH  800
#define PANEL_HEIGHT 480

// ---------------------------------------------------------------------------
// RGB parallel data bus
// ---------------------------------------------------------------------------
// LovyanGFX orders these d0..d15, which for RGB565 is B0-B4, G0-G5, R0-R4.
// Verified against Elecrow's wiki tutorial, which lists the same GPIOs in the
// same order as: dataPins[16] = {15,7,6,5,4,9,46,3,8,16,1,14,21,47,48,45}
#define LCD_B0 15
#define LCD_B1  7
#define LCD_B2  6
#define LCD_B3  5
#define LCD_B4  4
#define LCD_G0  9
#define LCD_G1 46
#define LCD_G2  3
#define LCD_G3  8
#define LCD_G4 16
#define LCD_G5  1
#define LCD_R0 14
#define LCD_R1 21
#define LCD_R2 47
#define LCD_R3 48
#define LCD_R4 45

// Sync signals
#define LCD_DE    41
#define LCD_VSYNC 40
#define LCD_HSYNC 39
#define LCD_PCLK   0

// Pixel clock. Elecrow's 7.0" tutorial specifies 24 MHz; 16 MHz is the safe
// fallback. If you see tearing, shimmering, or a horizontally torn image, drop
// this to 16000000 first -- it is the single most common cause of a bad image
// on these RGB panels.
#define LCD_PCLK_HZ 16000000

// Panel timing (800x480). These are the stock Elecrow values.
#define LCD_HSYNC_POLARITY    0
#define LCD_HSYNC_FRONT_PORCH 8
#define LCD_HSYNC_PULSE_WIDTH 4
#define LCD_HSYNC_BACK_PORCH  43
#define LCD_VSYNC_POLARITY    0
#define LCD_VSYNC_FRONT_PORCH 8
#define LCD_VSYNC_PULSE_WIDTH 4
#define LCD_VSYNC_BACK_PORCH  12
#define LCD_PCLK_ACTIVE_NEG   1

// ---------------------------------------------------------------------------
// Backlight
// ---------------------------------------------------------------------------
#define LCD_BACKLIGHT_PIN 2

// ---------------------------------------------------------------------------
// GT911 capacitive touch (I2C)
// ---------------------------------------------------------------------------
// GPIO19 and GPIO20 are also the ESP32-S3's native USB D-/D+ lines. This board
// wires them to the touch controller instead and provides USB serial through a
// separate CH340 bridge, so the native USB peripheral must stay off: build with
// "USB CDC On Boot: Disabled". Enabling it leaves USB_SERIAL_JTAG holding these
// two pins and touch dies while everything else keeps working.
#define TOUCH_SDA 19
#define TOUCH_SCL 20
#define TOUCH_INT -1   // not broken out
#define TOUCH_RST -1   // driven via the PCA9557 on V3.0 boards, not a GPIO

// Raw-to-screen mapping. Elecrow's examples invert both axes. If touch input
// lands mirrored, swap the pairs below -- that is the intended fix, no code
// change required elsewhere.
#define TOUCH_MAP_X1 800
#define TOUCH_MAP_X2 0
#define TOUCH_MAP_Y1 480
#define TOUCH_MAP_Y2 0

// ---------------------------------------------------------------------------
// TF / SD card (SPI) -- unused by this sketch, recorded so it is not lost
// ---------------------------------------------------------------------------
#define SD_MOSI 11
#define SD_MISO 13
#define SD_CLK  12
#define SD_CS   10

// ---------------------------------------------------------------------------
// I2S audio -- unused, and now unused on purpose
// ---------------------------------------------------------------------------
// This panel used to be planned as the voice-and-touch Alfred. Voice went to a
// separate, screenless puck instead (../voice/), which is a better fit: a
// microphone wants to be near where people stand, and this wants to be at eye
// level near a doorway. The pins stay recorded so they are not lost.
#define I2S_DOUT 17
#define I2S_BCLK 42
#define I2S_LRC  18

// ---------------------------------------------------------------------------
// Services this panel talks to
// ---------------------------------------------------------------------------
// All plain HTTP and LAN-only, so there is no TLS and nothing to expire. Going
// direct rather than through HomeCore's /luces/ and /camaras/ mounts is
// deliberate: those are @login_required, so a panel would need a session
// cookie, a CSRF token, and setInsecure for a self-signed certificate. The
// services themselves ask for none of that.
#define LIGHTS_API_URL   "http://hub.home:5010"
#define CAMERAS_API_URL  "http://cameras.home:21020"

#define MQTT_HOST      "mqtt.home"
#define MQTT_PORT      1883
#define MQTT_RETRY_MS  5000

// `home-lights/lights/state` carries every light in one retained message, which
// runs to a few KB. PubSubClient's default buffer is 256 bytes and it drops
// anything larger *silently* — the panel would simply never see the state it
// subscribed to.
#define MQTT_BUFFER_BYTES 8192

#define HTTP_TIMEOUT_MS 10000
#define WIFI_TIMEOUT_MS 15000

// Snapshot width requested from HomeCameras. The `?w=`/`?q=` parameters exist
// for this panel (HomeCameras/web_server/web_server.py); at w=640 q=60 a frame
// is about 40 KB instead of 175 KB, which is the difference between a tile that
// loads and one that exhausts the heap.
#define SNAPSHOT_W 640
#define SNAPSHOT_Q 60
#define SNAPSHOT_H 360          // canvas height; frames are letterboxed into it

// How often the open camera refreshes. Deliberately slow: this is a wall panel
// glanced at, not a monitoring station, and the MJPEG stream is off-limits
// because opening one evicts whoever is watching that camera in the browser.
#define SNAPSHOT_REFRESH_MS 5000

// ---------------------------------------------------------------------------
// Config portal
// ---------------------------------------------------------------------------
#define PORTAL_AP_PASSWORD   "alfred1234"
#define PORTAL_TIMEOUT_MS    (10UL * 60UL * 1000UL)
#define DRD_WINDOW_MS        3000
#define PORTAL_AFTER_FAILURES 20
