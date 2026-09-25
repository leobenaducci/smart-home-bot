// The house control panel — lights and cameras on the wall.
//
// A CrowPanel 7" by the door: tap a light to toggle it, tap a camera to see it.
// It used to be planned as the voice-and-touch Alfred; voice moved to a
// separate screenless puck (../voice/), which suits both better — a microphone
// wants to be where people stand, a screen wants to be at eye level.
//
// It talks to two LAN services directly, both of which ask for no credential:
// the lights server on hub:5010 and the camera server on cameras.home:21020.
// Going through HomeCore's /luces/ and /camaras/ mounts instead would mean a
// session cookie, a CSRF token and a self-signed certificate, for nothing.
//
// Lights come over MQTT rather than polling: the state topic is retained, so
// the list is populated the moment the broker connects, and `toggle` exists
// only on MQTT anyway. See lights.h for the two facts that decide that.
//
// Panel-specific code lives in display_rgb.cpp behind the DisplayDriver
// interface in display.h. Pin numbers live in board_config.h.
//
// See README.md -- the Arduino IDE board settings are not optional.

#include <Arduino.h>
#include <PubSubClient.h>
#include <WiFi.h>
#include <lvgl.h>

#include "board_config.h"
#include "cameras.h"
#include "display.h"
#include "lights.h"
#include "portal.h"

// Height of each LVGL render buffer, in lines. Two of these are allocated in
// internal DMA-capable RAM (800 x 20 x 2 bytes = 32 KB each). Raising this
// costs internal RAM, which is the scarce resource here -- PSRAM is already
// taken by the panel framebuffer and is too slow to render into.
static const uint16_t kBufferLines = 20;

static DisplayDriver& g_display = board_display();
static Settings g_settings;
static WiFiClient g_net;
static PubSubClient g_mqtt(g_net);
static uint32_t g_mqttRetryAt = 0;

static lv_obj_t* g_tabview = nullptr;
static lv_obj_t* g_lightsPage = nullptr;
static lv_obj_t* g_camsPage = nullptr;
static lv_obj_t* g_canvas = nullptr;      // camera still
static lv_obj_t* g_camLabel = nullptr;
static lv_obj_t* g_statusBar = nullptr;
static int16_t g_openCam = -1;
static uint32_t g_camRefreshAt = 0;
static bool g_lightsDirty = true;

// ---------------------------------------------------------------------------
// LVGL glue (unchanged from the bring-up sketch)
// ---------------------------------------------------------------------------
static uint32_t tick_cb() { return millis(); }

static void flush_cb(lv_display_t* disp, const lv_area_t* area, uint8_t* pixels) {
  g_display.flush(area, pixels);
  lv_display_flush_ready(disp);
}

static void touch_cb(lv_indev_t* indev, lv_indev_data_t* data) {
  (void)indev;
  int16_t x, y;
  if (g_display.readTouch(x, y)) {
    data->point.x = x;
    data->point.y = y;
    data->state = LV_INDEV_STATE_PRESSED;
  } else {
    data->state = LV_INDEV_STATE_RELEASED;
  }
}

static bool lvgl_begin() {
  lv_init();
  lv_tick_set_cb(tick_cb);

  const uint16_t w = g_display.width();
  const uint16_t h = g_display.height();
  const uint32_t px_size = lv_color_format_get_size(g_display.colorFormat());
  const uint32_t buf_bytes = w * kBufferLines * px_size;

  void* buf1 = heap_caps_malloc(buf_bytes, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
  void* buf2 = heap_caps_malloc(buf_bytes, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
  if (buf1 == nullptr || buf2 == nullptr) {
    Serial.println("[lvgl] failed to allocate render buffers");
    return false;
  }

  lv_display_t* disp = lv_display_create(w, h);
  lv_display_set_color_format(disp, g_display.colorFormat());
  lv_display_set_flush_cb(disp, flush_cb);
  lv_display_set_buffers(disp, buf1, buf2, buf_bytes, g_display.renderMode());

  lv_indev_t* indev = lv_indev_create();
  lv_indev_set_type(indev, LV_INDEV_TYPE_POINTER);
  lv_indev_set_read_cb(indev, touch_cb);
  return true;
}

// ---------------------------------------------------------------------------
// Lights tab
// ---------------------------------------------------------------------------
static void on_light_tap(lv_event_t* e) {
  uint16_t idx = (uint16_t)(uintptr_t)lv_event_get_user_data(e);
  lightsToggle(idx);
  // No optimistic repaint: the bulb's real state arrives on its retained topic
  // a moment later. Guessing here means the panel lies whenever a bulb is
  // unreachable, which is exactly when someone is standing there pressing it.
}

static void build_lights_page() {
  lv_obj_clean(g_lightsPage);
  g_lightsDirty = false;

  uint16_t n = lightsCount();
  if (n == 0) {
    lv_obj_t* msg = lv_label_create(g_lightsPage);
    lv_label_set_text(msg, WiFi.status() == WL_CONNECTED
                               ? "Buscando luces..."
                               : "Sin WiFi");
    lv_obj_set_style_text_color(msg, lv_color_hex(0x9AA0A6), LV_PART_MAIN);
    lv_obj_center(msg);
    return;
  }

  // Three across fits comfortably on 800 px with room for a name that is not
  // truncated, which matters because the names ARE the room names here.
  static int32_t cols[] = {LV_GRID_FR(1), LV_GRID_FR(1), LV_GRID_FR(1), LV_GRID_TEMPLATE_LAST};
  static int32_t rows[] = {90, 90, 90, 90, LV_GRID_TEMPLATE_LAST};
  lv_obj_set_grid_dsc_array(g_lightsPage, cols, rows);
  lv_obj_set_layout(g_lightsPage, LV_LAYOUT_GRID);

  for (uint16_t i = 0; i < n && i < 12; i++) {
    const Light* l = lightsAt(i);
    lv_obj_t* btn = lv_button_create(g_lightsPage);
    lv_obj_set_grid_cell(btn, LV_GRID_ALIGN_STRETCH, i % 3, 1,
                         LV_GRID_ALIGN_STRETCH, i / 3, 1);
    lv_obj_set_style_bg_color(btn,
                              l->on ? lv_color_hex(0xF5B301) : lv_color_hex(0x2A2F35),
                              LV_PART_MAIN);
    lv_obj_add_event_cb(btn, on_light_tap, LV_EVENT_CLICKED, (void*)(uintptr_t)i);

    lv_obj_t* lab = lv_label_create(btn);
    lv_label_set_text(lab, l->name.c_str());
    lv_label_set_long_mode(lab, LV_LABEL_LONG_DOT);
    lv_obj_set_width(lab, LV_PCT(100));
    lv_obj_set_style_text_color(lab,
                                l->on ? lv_color_hex(0x1A1A1A) : lv_color_hex(0xE8EAED),
                                LV_PART_MAIN);
    lv_obj_center(lab);
  }
}

// ---------------------------------------------------------------------------
// Cameras tab
// ---------------------------------------------------------------------------
static void on_camera_tap(lv_event_t* e) {
  g_openCam = (int16_t)(intptr_t)lv_event_get_user_data(e);
  const Camera* c = camerasAt(g_openCam);
  if (c) lv_label_set_text(g_camLabel, c->name.c_str());
  g_camRefreshAt = 0;         // fetch on the next loop rather than inside the event
}

static void build_cameras_page() {
  lv_obj_clean(g_camsPage);

  lv_obj_t* row = lv_obj_create(g_camsPage);
  lv_obj_set_size(row, LV_PCT(100), 60);
  lv_obj_align(row, LV_ALIGN_TOP_MID, 0, 0);
  lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
  lv_obj_set_style_bg_opa(row, LV_OPA_TRANSP, LV_PART_MAIN);
  lv_obj_set_style_border_width(row, 0, LV_PART_MAIN);

  for (uint16_t i = 0; i < camerasCount(); i++) {
    lv_obj_t* btn = lv_button_create(row);
    lv_obj_add_event_cb(btn, on_camera_tap, LV_EVENT_CLICKED, (void*)(intptr_t)i);
    lv_obj_t* lab = lv_label_create(btn);
    lv_label_set_text(lab, camerasAt(i)->name.c_str());
    lv_obj_center(lab);
  }

  g_camLabel = lv_label_create(g_camsPage);
  lv_obj_set_style_text_color(g_camLabel, lv_color_hex(0x9AA0A6), LV_PART_MAIN);
  lv_label_set_text(g_camLabel, camerasCount() ? "Elige una camara" : "Sin camaras");
  lv_obj_align(g_camLabel, LV_ALIGN_TOP_MID, 0, 66);

  // The canvas is PSRAM-backed: 640x360 RGB565 is 460 KB, which internal RAM
  // does not have and does not need to — it is written once per refresh and
  // then only read by the display flush.
  static uint8_t* canvasBuf = nullptr;
  const uint32_t need = SNAPSHOT_W * SNAPSHOT_H * 2;
  if (!canvasBuf) canvasBuf = (uint8_t*)heap_caps_malloc(need, MALLOC_CAP_SPIRAM);
  if (!canvasBuf) {
    Serial.println("[cam] no PSRAM for the canvas");
    return;
  }

  g_canvas = lv_canvas_create(g_camsPage);
  lv_canvas_set_buffer(g_canvas, canvasBuf, SNAPSHOT_W, SNAPSHOT_H, LV_COLOR_FORMAT_RGB565);
  lv_canvas_fill_bg(g_canvas, lv_color_hex(0x000000), LV_OPA_COVER);
  lv_obj_align(g_canvas, LV_ALIGN_BOTTOM_MID, 0, -6);
}

// ---------------------------------------------------------------------------
// Shell
// ---------------------------------------------------------------------------
static void build_ui() {
  lv_obj_t* scr = lv_screen_active();
  lv_obj_set_style_bg_color(scr, lv_color_hex(0x101418), LV_PART_MAIN);

  g_tabview = lv_tabview_create(scr);
  lv_tabview_set_tab_bar_size(g_tabview, 56);
  g_lightsPage = lv_tabview_add_tab(g_tabview, "Luces");
  g_camsPage = lv_tabview_add_tab(g_tabview, "Camaras");

  g_statusBar = lv_label_create(scr);
  lv_obj_set_style_text_color(g_statusBar, lv_color_hex(0x5F6368), LV_PART_MAIN);
  lv_obj_align(g_statusBar, LV_ALIGN_TOP_RIGHT, -10, 18);
  lv_label_set_text(g_statusBar, "");
}

static void refresh_status() {
  if (!g_statusBar) return;
  lv_label_set_text_fmt(g_statusBar, "%s  %s",
                        WiFi.status() == WL_CONNECTED ? "wifi" : "sin wifi",
                        g_mqtt.connected() ? "mqtt" : "sin mqtt");
}

static void onMqtt(char* topic, byte* payload, unsigned int len) {
  if (lightsOnMqtt(topic, payload, len)) g_lightsDirty = true;
}

static void mqttEnsure() {
  if (g_mqtt.connected()) return;
  if (millis() < g_mqttRetryAt) return;
  g_mqttRetryAt = millis() + MQTT_RETRY_MS;

  uint8_t mac[6];
  WiFi.macAddress(mac);
  char id[32];
  snprintf(id, sizeof(id), "panel-%02X%02X%02X", mac[3], mac[4], mac[5]);

  if (g_mqtt.connect(id)) {
    Serial.println("[mqtt] connected");
    lightsSubscribe();
    // Retained state does not survive a broker restart (persistence false), so
    // a reconnect cannot assume the retained message is still there. One HTTP
    // snapshot per connect is the guaranteed path.
    lightsFetchHttp();
    g_lightsDirty = true;
  }
}

static void runConfigPortal(const char* why) {
  Serial.printf("[boot] config portal: %s\n", why);
  PortalInfo info = portalBegin();
  Serial.printf("[boot] join %s / %s then open http://%s\n",
                info.ssid.c_str(), info.password.c_str(), info.ip.c_str());
  if (portalRun(PORTAL_TIMEOUT_MS)) ESP.restart();
  failureCountSet(0);
  ESP.restart();
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.printf("[boot] %s\n", BOARD_NAME);
  Serial.printf("[boot] PSRAM: %lu KB\n", (unsigned long)(ESP.getPsramSize() / 1024));

  if (ESP.getPsramSize() == 0) {
    // The panel framebuffer, the LVGL canvas and the JPEG buffer all come from
    // PSRAM. Without it nothing here works, so stop rather than crash later in
    // a way that looks like a display fault.
    Serial.println("[boot] FATAL: no PSRAM -- check board settings (OPI PSRAM)");
    return;
  }

  if (!g_display.begin()) {
    Serial.println("[boot] FATAL: display init failed");
    return;
  }
  if (!lvgl_begin()) return;
  build_ui();
  lv_timer_handler();          // paint the shell before the network stalls us

  bool configured = settingsLoad(g_settings);
  uint32_t fails = failureCount();
  if (!configured) runConfigPortal("no settings stored");
  else if (doubleResetDetected()) runConfigPortal("double reset");
  else if (fails >= PORTAL_AFTER_FAILURES) runConfigPortal("too many failures");

  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(g_settings.ssid.c_str(), g_settings.password.c_str());
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_TIMEOUT_MS) {
    lv_timer_handler();
    delay(10);
  }
  if (WiFi.status() != WL_CONNECTED) {
    failureCountSet(fails + 1);
    Serial.println("[boot] wifi failed");
  } else {
    failureCountSet(0);
    Serial.printf("[boot] wifi %s\n", WiFi.localIP().toString().c_str());
  }

  g_mqtt.setServer(MQTT_HOST, MQTT_PORT);
  g_mqtt.setCallback(onMqtt);
  // The retained whole-house state message runs to several KB and PubSubClient
  // drops anything past its buffer silently — the panel would subscribe and
  // then simply never receive.
  g_mqtt.setBufferSize(MQTT_BUFFER_BYTES);
  lightsBegin(&g_mqtt);
  camerasBegin();

  if (WiFi.status() == WL_CONNECTED) {
    lightsFetchHttp();
    camerasFetchList();
  }
  build_cameras_page();
}

void loop() {
  lv_timer_handler();

  if (WiFi.status() == WL_CONNECTED) {
    mqttEnsure();
    if (g_mqtt.connected()) g_mqtt.loop();
  }

  if (g_lightsDirty) build_lights_page();

  // The open camera refreshes on a slow timer. Snapshots, never the stream:
  // opening /stream/ would evict whoever is watching that camera in a browser.
  if (g_openCam >= 0 && g_canvas && millis() >= g_camRefreshAt) {
    g_camRefreshAt = millis() + SNAPSHOT_REFRESH_MS;
    camerasDrawSnapshot(g_openCam, g_canvas);
  }

  static uint32_t lastStatus = 0;
  if (millis() - lastStatus > 1000) {
    lastStatus = millis();
    refresh_status();
  }

  delay(5);
}
