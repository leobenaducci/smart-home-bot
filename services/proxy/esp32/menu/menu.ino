// Minuta - the kitchen weekly-menu panel.
//
// ESP32-C3 + WeAct 4.2" e-paper. The whole job:
//
//   wake -> WiFi -> GET the week as a 1-bit bitmap -> blit it -> deep sleep
//
// **This firmware does no layout.** The server hands back 15000 bytes of packed
// pixels already arranged, so changing how the panel looks is a HomeCore
// redeploy, not a reflash. That is the point of the split: the thing bolted to
// a wall behind a battery is the thing that should change least.
//
// Two consequences worth knowing before editing:
//
//   - The device has no clock and no timezone. It sleeps for exactly as long as
//     the server's X-Sleep-Seconds says, which is how the 23:00-06:00 quiet
//     window is enforced without any date handling here.
//   - The ETag is kept in RTC memory across deep sleep. A 304 means the pixels
//     are unchanged and we skip the refresh entirely - that saves the visible
//     flash and the panel's ghosting, though not the WiFi association, which
//     is where most of the energy actually goes.
//
// See README.md for wiring, board settings and libraries.

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <GxEPD2_BW.h>

#include "board_config.h"
#include "portal.h"

// Network settings come from NVS via portal.h, falling back to secrets.h at
// compile time when it exists. There is deliberately no #include "secrets.h"
// here - portal.cpp owns that, so this file works identically whether the panel
// was flashed pre-provisioned or configured over the air.
static Settings g_settings;

GxEPD2_BW<PANEL_CLASS, PANEL_CLASS::HEIGHT> display(
    PANEL_CLASS(EPD_CS, EPD_DC, EPD_RST, EPD_BUSY));

// ---------------------------------------------------------------------------
// State that survives deep sleep
// ---------------------------------------------------------------------------
// RTC memory keeps its contents through deep sleep but holds garbage after a
// cold boot, so a magic value tells the two apart. Getting this wrong would
// mean a random ETag on first power-up and a panel that never draws.
#define RTC_MAGIC 0x4D494E55   // 'MINU'

RTC_DATA_ATTR uint32_t g_magic = 0;
RTC_DATA_ATTR uint32_t g_wakes = 0;
RTC_DATA_ATTR char g_etag[80] = {0};

static void sleepFor(uint32_t seconds) {
  if (seconds < MIN_SLEEP_SECONDS) seconds = MIN_SLEEP_SECONDS;
  if (seconds > MAX_SLEEP_SECONDS) seconds = MAX_SLEEP_SECONDS;
  Serial.printf("[sleep] %u s (%.2f h)\n", seconds, seconds / 3600.0);
  Serial.flush();

  display.hibernate();          // panel into deep power-down, not just idle
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);

  esp_sleep_enable_timer_wakeup((uint64_t)seconds * 1000000ULL);
  esp_deep_sleep_start();       // never returns; setup() runs again on wake
}

static bool connectWiFi() {
  Serial.printf("[wifi] connecting to %s\n", g_settings.ssid.c_str());
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);

#ifdef USE_STATIC_IP
  // Skips DHCP, which is a second of radio time on every single wake.
  IPAddress ip(STATIC_IP), gw(STATIC_GATEWAY), sn(STATIC_SUBNET), dns(STATIC_DNS);
  if (!WiFi.config(ip, gw, sn, dns)) {
    Serial.println("[wifi] static config rejected, falling back to DHCP");
  }
#endif

  WiFi.begin(g_settings.ssid.c_str(), g_settings.password.c_str());
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - start > WIFI_TIMEOUT_MS) {
      Serial.println("[wifi] timeout");
      return false;
    }
    delay(100);
  }
  Serial.printf("[wifi] ok %s in %lu ms, rssi %d\n",
                WiFi.localIP().toString().c_str(), millis() - start, WiFi.RSSI());
  return true;
}

// Draw a packed 1-bit bitmap straight from the server.
//
// PIL's mode '1' and GxEPD2's internal buffer use the same convention - MSB
// first, 0 = black - so the bytes go across untouched. If the panel ever comes
// out photographically inverted, flip the `invert` argument below; that is the
// whole fix, and it means the server changed, not this.
static void drawBitmap(const uint8_t* buf) {
  display.setFullWindow();
  display.firstPage();
  do {
    display.drawImage(buf, 0, 0, PANEL_WIDTH, PANEL_HEIGHT,
                      /*invert=*/false, /*mirror_y=*/false, /*pgm=*/false);
  } while (display.nextPage());
}

// Instructions for joining the panel's AP, drawn on the panel itself.
//
// This is the one screen the firmware lays out rather than fetching, and it has
// to be: it's what you look at when the network is precisely what isn't
// working. It uses Adafruit_GFX's built-in font, which is ASCII only - hence
// "Configuracion" without the accent. Don't paste accented text in here; it
// renders as blanks, and this is the screen that most needs to be readable.
static void showConfigScreen(const PortalInfo& info) {
  display.setFullWindow();
  display.firstPage();
  do {
    display.fillScreen(GxEPD_WHITE);
    display.setTextColor(GxEPD_BLACK);

    display.setTextSize(3);
    display.setCursor(16, 24);
    display.print("MINUTA");

    display.setTextSize(2);
    display.setCursor(16, 62);
    display.print("Configuracion de red");
    display.drawLine(16, 88, PANEL_WIDTH - 16, 88, GxEPD_BLACK);

    display.setCursor(16, 104);
    display.print("1. Conecta el telefono a:");
    display.setTextSize(3);
    display.setCursor(34, 130);
    display.print(info.ssid);
    display.setTextSize(2);
    display.setCursor(34, 162);
    display.print("Clave: ");
    display.print(info.password);

    display.setCursor(16, 200);
    display.print("2. Abre en el navegador:");
    display.setTextSize(3);
    display.setCursor(34, 226);
    display.print(info.ip);

    display.setTextSize(1);
    display.setCursor(16, 278);
    display.print("El panel vuelve a dormir en 10 minutos.");
  } while (display.nextPage());
}

// Open the portal, show how to reach it, and act on the outcome. Never returns.
static void runConfigPortal(const char* why) {
  Serial.printf("[portal] entering config mode (%s)\n", why);
  PortalInfo info = portalBegin();
  showConfigScreen(info);

  if (portalRun(PORTAL_TIMEOUT_MS)) {
    Serial.println("[portal] restarting with new settings");
    delay(200);
    ESP.restart();
  }

  // Timed out with nothing saved. Clear the failure counter before sleeping:
  // without this, a panel whose network really is gone would reopen the portal
  // on every single wake and flatten the cell in an afternoon. Zeroing it means
  // the next portal is another PORTAL_AFTER_FAILURES cycles away, and the long
  // sleep bounds the worst case to two portals a day.
  failureCountSet(0);
  sleepFor(MAX_SLEEP_SECONDS);
}

void setup() {
  Serial.begin(115200);
  delay(50);

  if (g_magic != RTC_MAGIC) {     // cold boot: RTC memory is undefined
    g_magic = RTC_MAGIC;
    g_wakes = 0;
    g_etag[0] = '\0';
    Serial.println("\n[boot] cold start");
  }
  g_wakes++;
  Serial.printf("[boot] %s\n[boot] wake #%u, heap %u\n",
                BOARD_NAME, g_wakes, ESP.getFreeHeap());

  SPI.end();
  SPI.begin(EPD_SCK, -1 /* no MISO */, EPD_MOSI, EPD_CS);
  display.init(115200, /*initial=*/g_wakes == 1, /*reset_ms=*/2, /*pulldown=*/false);

  bool configured = settingsLoad(g_settings);
  uint32_t fails = failureCount();

  // A timer wake is the device's own doing; anything else means a human pressed
  // RESET or plugged it in. Only the latter is allowed to cost the double-reset
  // window, because charging every hourly wake DRD_WINDOW_MS for a gesture
  // nobody made would be pure waste.
  bool human = esp_reset_reason() != ESP_RST_DEEPSLEEP;

  if (!configured) {
    runConfigPortal("no settings stored");
  } else if (human && doubleResetDetected()) {
    runConfigPortal("double reset");
  } else if (fails >= PORTAL_AFTER_FAILURES) {
    runConfigPortal("too many failures");
  }

  if (!connectWiFi()) {
    failureCountSet(fails + 1);
    sleepFor(RETRY_SLEEP_SECONDS);
  }

  WiFiClientSecure client;
  // HomeCore's cert is self-signed (CN=localhost), so there is no chain to
  // validate and no hostname that would match. On the home LAN this still
  // encrypts the token against passive sniffing; it does not defend against an
  // active man-in-the-middle already on the network. The endpoint is read-only
  // and returns a picture of dinner, which is the trade being made here.
  client.setInsecure();

  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.setReuse(false);
  if (!http.begin(client, g_settings.url)) {
    Serial.println("[http] begin failed");
    failureCountSet(fails + 1);
    sleepFor(RETRY_SLEEP_SECONDS);
  }

  const char* wanted[] = {"X-Sleep-Seconds", "ETag"};
  http.collectHeaders(wanted, 2);
  http.addHeader("X-Device-Token", g_settings.token);
  if (g_etag[0]) http.addHeader("If-None-Match", g_etag);

  int code = http.GET();
  Serial.printf("[http] %d\n", code);

  // Only a usable answer counts as success. A wrong token (401), a moved
  // endpoint (404) and a vanished SSID all leave the same blank wall, and all
  // three are things the portal can fix - so all three must accumulate toward
  // reopening it. Counting any HTTP response as success would strand a panel
  // with a mistyped token forever, silently.
  bool usable = (code == HTTP_CODE_OK || code == HTTP_CODE_NOT_MODIFIED);
  failureCountSet(usable ? 0 : fails + 1);

  // Read the sleep hint before any early exit - even a 304 carries it, and
  // honouring it is what keeps the panel off the air overnight.
  uint32_t sleep_s = DEFAULT_SLEEP_SECONDS;
  if (http.hasHeader("X-Sleep-Seconds")) {
    long v = http.header("X-Sleep-Seconds").toInt();
    if (v > 0) sleep_s = (uint32_t)v;
  }

  if (code == HTTP_CODE_NOT_MODIFIED) {
    Serial.println("[draw] unchanged, skipping refresh");
    http.end();
    sleepFor(sleep_s);
  }

  if (code != HTTP_CODE_OK) {
    Serial.printf("[http] unexpected status, retrying later\n");
    http.end();
    sleepFor(RETRY_SLEEP_SECONDS);
  }

  int len = http.getSize();
  if (len != PANEL_BYTES) {
    // Refuse rather than blit a misaligned image: a wrong length means the
    // server's geometry and board_config.h have diverged, and a scrambled
    // screen is a much worse way to find that out than this line.
    Serial.printf("[http] length %d, expected %d - not drawing\n", len, PANEL_BYTES);
    http.end();
    sleepFor(RETRY_SLEEP_SECONDS);
  }

  uint8_t* buf = (uint8_t*)malloc(PANEL_BYTES);
  if (!buf) {
    Serial.printf("[mem] malloc(%d) failed, heap %u\n", PANEL_BYTES, ESP.getFreeHeap());
    http.end();
    sleepFor(RETRY_SLEEP_SECONDS);
  }

  WiFiClient* stream = http.getStreamPtr();
  int got = 0;
  uint32_t deadline = millis() + HTTP_TIMEOUT_MS;
  while (got < PANEL_BYTES && millis() < deadline) {
    int n = stream->read(buf + got, PANEL_BYTES - got);
    if (n > 0) {
      got += n;
      deadline = millis() + HTTP_TIMEOUT_MS;   // progress resets the clock
    } else {
      delay(5);
    }
  }

  // Capture the ETag only after a complete body: storing it early would mean a
  // truncated download is remembered as successfully drawn, and the panel would
  // sit on a half-image until the menu next changed.
  if (got == PANEL_BYTES) {
    String tag = http.header("ETag");
    strncpy(g_etag, tag.c_str(), sizeof(g_etag) - 1);
    g_etag[sizeof(g_etag) - 1] = '\0';
  }
  http.end();

  if (got != PANEL_BYTES) {
    Serial.printf("[http] short read %d/%d\n", got, PANEL_BYTES);
    free(buf);
    sleepFor(RETRY_SLEEP_SECONDS);
  }

  Serial.printf("[draw] %d bytes, etag %s\n", got, g_etag);
  uint32_t t0 = millis();
  drawBitmap(buf);
  free(buf);
  Serial.printf("[draw] done in %lu ms\n", millis() - t0);

  sleepFor(sleep_s);
}

void loop() {
  // Unreachable: setup() always ends in deep sleep, which restarts the sketch
  // from the top on the next wake.
}
