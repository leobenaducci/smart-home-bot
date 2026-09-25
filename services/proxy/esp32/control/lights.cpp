#include "lights.h"
#include "board_config.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFiClient.h>

static PubSubClient* g_mqtt = nullptr;
static Light g_lights[LIGHTS_MAX];
static uint16_t g_count = 0;

void lightsBegin(PubSubClient* mqtt) { g_mqtt = mqtt; }

void lightsSubscribe() {
  if (!g_mqtt) return;
  // The whole-array topic gives the initial picture; the per-light topic is
  // what actually arrives when somebody presses a wall button. Subscribing to
  // both means a single bulb changing does not republish the entire house.
  g_mqtt->subscribe("home-lights/lights/state", 1);
  g_mqtt->subscribe("home-lights/lights/+/state", 1);
}

static Light* findByMac(const String& mac) {
  for (uint16_t i = 0; i < g_count; i++) {
    if (g_lights[i].mac.equalsIgnoreCase(mac)) return &g_lights[i];
  }
  return nullptr;
}

// Merge one light object into the table. Returns true if anything is different
// from what was already there — the UI is rebuilt on that, and rebuilding an
// LVGL list on every 2-hour keepalive republish would be a visible flicker for
// no reason.
static bool upsert(JsonObjectConst o) {
  String mac = (const char*)(o["mac"] | "");
  if (!mac.length()) return false;

  Light* l = findByMac(mac);
  if (!l) {
    if (g_count >= LIGHTS_MAX) return false;
    l = &g_lights[g_count++];
    l->mac = mac;
    l->name = (const char*)(o["name"] | "");
    l->on = o["is_on"] | false;
    l->brightness = o["brightness"] | 0;
    return true;
  }

  bool changed = false;
  String name = (const char*)(o["name"] | "");
  if (name.length() && name != l->name) { l->name = name; changed = true; }
  if (!o["is_on"].isNull()) {
    bool on = o["is_on"] | false;
    if (on != l->on) { l->on = on; changed = true; }
  }
  if (!o["brightness"].isNull()) {
    uint8_t b = o["brightness"] | 0;
    if (b != l->brightness) { l->brightness = b; changed = true; }
  }
  return changed;
}

bool lightsOnMqtt(const char* topic, const uint8_t* payload, unsigned int len) {
  String t(topic);
  if (!t.startsWith("home-lights/lights/")) return false;

  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return false;

  if (t == "home-lights/lights/state") {
    // The full array. Not cleared first: a light that vanished from the array
    // is far more likely to be a transient publish than a bulb removed from the
    // house, and dropping it would make the panel flicker items away.
    bool changed = false;
    for (JsonObjectConst o : doc.as<JsonArrayConst>()) changed |= upsert(o);
    return changed;
  }

  // home-lights/lights/<MAC>/state — one bulb.
  return upsert(doc.as<JsonObjectConst>());
}

bool lightsFetchHttp() {
  WiFiClient client;
  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(client, String(LIGHTS_API_URL) + "/api/state")) return false;

  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    Serial.printf("[lights] http state failed: %d\n", code);
    http.end();
    return false;
  }

  // /api/state carries buttons and timestamps this panel has no use for.
  // A filter keeps the parsed document to the few fields below, which matters
  // when the alternative is several KB of JSON on a device already holding a
  // 750 KB framebuffer.
  JsonDocument filter;
  JsonObject f = filter["lights"].add<JsonObject>();
  f["mac"] = true;
  f["name"] = true;
  f["is_on"] = true;
  f["brightness"] = true;

  JsonDocument doc;
  DeserializationError err =
      deserializeJson(doc, http.getStream(), DeserializationOption::Filter(filter));
  http.end();
  if (err) {
    Serial.printf("[lights] state json: %s\n", err.c_str());
    return false;
  }

  for (JsonObjectConst o : doc["lights"].as<JsonArrayConst>()) upsert(o);
  Serial.printf("[lights] %u known\n", g_count);
  return true;
}

uint16_t lightsCount() { return g_count; }

const Light* lightsAt(uint16_t i) { return i < g_count ? &g_lights[i] : nullptr; }

void lightsToggle(uint16_t i) {
  if (i >= g_count || !g_mqtt) return;
  String topic = "home-lights/lights/" + g_lights[i].mac + "/set";
  // Raw "toggle", not JSON: the server accepts both, and this is the one verb
  // that has no HTTP equivalent at all.
  g_mqtt->publish(topic.c_str(), "toggle");
  Serial.printf("[lights] toggle %s\n", g_lights[i].name.c_str());
}

void lightsSetBrightness(uint16_t i, uint8_t brightness) {
  if (i >= g_count || !g_mqtt) return;
  String topic = "home-lights/lights/" + g_lights[i].mac + "/set";
  char body[64];
  snprintf(body, sizeof(body), "{\"action\":\"brightness\",\"brightness\":%u}", brightness);
  g_mqtt->publish(topic.c_str(), body);
}
