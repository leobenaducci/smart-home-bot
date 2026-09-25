#include "cameras.h"
#include "board_config.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <TJpg_Decoder.h>
#include <WiFiClient.h>

static Camera g_cams[CAMERAS_MAX];
static uint16_t g_count = 0;

// Where the decoder writes. TJpg_Decoder's callback carries no user pointer, so
// the target has to live at file scope — the alternative is decoding into a
// scratch buffer and copying, which on a 640x360 canvas is 460 KB of pointless
// memmove.
static lv_obj_t* g_canvas = nullptr;
static int16_t g_offX = 0, g_offY = 0;

static bool jpegOut(int16_t x, int16_t y, uint16_t w, uint16_t h, uint16_t* bitmap) {
  if (!g_canvas) return false;
  lv_area_t area;
  area.x1 = x + g_offX;
  area.y1 = y + g_offY;
  area.x2 = area.x1 + w - 1;
  area.y2 = area.y1 + h - 1;
  // Blocks that fall outside the canvas are dropped rather than clipped: TJpg
  // hands back whole MCUs, and a camera whose height rounds past the canvas
  // would otherwise scribble past the buffer.
  int32_t cw = lv_obj_get_width(g_canvas);
  int32_t ch = lv_obj_get_height(g_canvas);
  if (area.x1 < 0 || area.y1 < 0 || area.x2 >= cw || area.y2 >= ch) return true;

  lv_draw_buf_t* buf = lv_canvas_get_draw_buf(g_canvas);
  if (!buf) return false;
  uint16_t* px = (uint16_t*)buf->data;
  uint32_t stride = buf->header.stride / sizeof(uint16_t);
  for (uint16_t row = 0; row < h; row++) {
    memcpy(&px[(area.y1 + row) * stride + area.x1], &bitmap[row * w], w * sizeof(uint16_t));
  }
  return true;   // false would abort the decode
}

void camerasBegin() {
  TJpgDec.setCallback(jpegOut);
  // TJpg emits RGB565 in the byte order TFT_eSPI wants, which is the opposite
  // of what a raw LVGL RGB565 buffer expects. If the first snapshot comes out
  // in wrong colours — blue faces, orange sky — this line is the one to flip.
  TJpgDec.setSwapBytes(true);
}

bool camerasFetchList() {
  WiFiClient client;
  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(client, String(CAMERAS_API_URL) + "/api/cameras")) return false;

  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    Serial.printf("[cam] list failed: %d\n", code);
    http.end();
    return false;
  }

  // The response is an object keyed by camera id, not an array, and carries
  // stream paths and geometry this panel does not use.
  JsonDocument filter;
  filter["*"]["camera_id"] = true;
  filter["*"]["name"] = true;

  JsonDocument doc;
  DeserializationError err =
      deserializeJson(doc, http.getStream(), DeserializationOption::Filter(filter));
  http.end();
  if (err) {
    Serial.printf("[cam] list json: %s\n", err.c_str());
    return false;
  }

  g_count = 0;
  for (JsonPairConst kv : doc.as<JsonObjectConst>()) {
    if (g_count >= CAMERAS_MAX) break;
    g_cams[g_count].id = (const char*)(kv.value()["camera_id"] | kv.key().c_str());
    g_cams[g_count].name = (const char*)(kv.value()["name"] | "camara");
    g_count++;
  }
  Serial.printf("[cam] %u cameras\n", g_count);
  return g_count > 0;
}

uint16_t camerasCount() { return g_count; }

const Camera* camerasAt(uint16_t i) { return i < g_count ? &g_cams[i] : nullptr; }

bool camerasDrawSnapshot(uint16_t i, lv_obj_t* canvas) {
  if (i >= g_count || !canvas) return false;

  String url = String(CAMERAS_API_URL) + "/snapshot/" + g_cams[i].id +
               "?w=" + String(SNAPSHOT_W) + "&q=" + String(SNAPSHOT_Q);

  WiFiClient client;
  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(client, url)) return false;

  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    // 503 is normal and not an error worth shouting about: it means the server
    // has no frame for that camera yet, which happens for a few seconds after
    // it reconnects to an RTSP source.
    Serial.printf("[cam] snapshot %s: %d\n", g_cams[i].name.c_str(), code);
    http.end();
    return false;
  }

  int len = http.getSize();
  if (len <= 0 || len > 200 * 1024) {
    Serial.printf("[cam] implausible snapshot size %d\n", len);
    http.end();
    return false;
  }

  // Decoding needs the whole JPEG, so this one does get buffered — in PSRAM,
  // because 40 KB of internal RAM is not free on a board already holding two
  // LVGL render buffers.
  uint8_t* jpg = (uint8_t*)heap_caps_malloc(len, MALLOC_CAP_SPIRAM);
  if (!jpg) {
    http.end();
    return false;
  }

  WiFiClient* stream = http.getStreamPtr();
  int got = 0;
  uint32_t deadline = millis() + HTTP_TIMEOUT_MS;
  while (got < len && millis() < deadline) {
    int n = stream->read(jpg + got, len - got);
    if (n > 0) {
      got += n;
      deadline = millis() + HTTP_TIMEOUT_MS;   // progress resets the clock
    } else {
      delay(2);
    }
  }
  http.end();

  if (got != len) {
    Serial.printf("[cam] short read %d/%d\n", got, len);
    heap_caps_free(jpg);
    return false;
  }

  uint16_t jw = 0, jh = 0;
  if (TJpgDec.getJpgSize(&jw, &jh, jpg, len) != JDR_OK || jw == 0) {
    heap_caps_free(jpg);
    return false;
  }

  // Fit whatever actually arrived. The server resizes by width, so a portrait
  // camera comes back taller than the canvas; scale down by powers of two until
  // it fits, since that is the only scaling TJpg does.
  uint8_t scale = 1;
  while (scale < 8 && (jw / scale > lv_obj_get_width(canvas) ||
                       jh / scale > lv_obj_get_height(canvas))) {
    scale *= 2;
  }
  TJpgDec.setJpgScale(scale);

  g_canvas = canvas;
  g_offX = (lv_obj_get_width(canvas) - jw / scale) / 2;
  g_offY = (lv_obj_get_height(canvas) - jh / scale) / 2;
  if (g_offX < 0) g_offX = 0;
  if (g_offY < 0) g_offY = 0;

  lv_canvas_fill_bg(canvas, lv_color_hex(0x000000), LV_OPA_COVER);
  JRESULT r = TJpgDec.drawJpg(0, 0, jpg, len);
  g_canvas = nullptr;

  heap_caps_free(jpg);
  lv_obj_invalidate(canvas);
  return r == JDR_OK;
}
