#include "gateway.h"
#include "audio.h"
#include "board_config.h"
#include "portal.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFiClient.h>

extern Settings g_settings;   // owned by voice.ino

static String base() {
  String u = g_settings.url;
  while (u.endsWith("/")) u.remove(u.length() - 1);
  return u;
}

bool gatewayWhoAmI(String& roomOut) {
  WiFiClient client;
  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(client, base() + "/v1/whoami")) return false;
  http.addHeader("X-Device-Token", g_settings.token);

  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    // 403 here is the single most likely first-boot failure: the token in this
    // device does not exist in the gateway's devices.json. Say which it was.
    Serial.printf("[gw] whoami failed: %d%s\n", code,
                  code == 403 ? " (token not in devices.json)"
                              : (code == 401 ? " (no token configured)" : ""));
    http.end();
    return false;
  }

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, http.getStream());
  http.end();
  if (err) {
    Serial.printf("[gw] whoami json: %s\n", err.c_str());
    return false;
  }
  roomOut = (const char*)(doc["room"] | "");
  return roomOut.length() > 0;
}

// Pull callback for audioPlay: hands it bytes straight off the socket.
struct StreamCtx {
  WiFiClient* stream;
  uint32_t remaining;
  uint32_t deadline;
};

static size_t streamPull(uint8_t* dst, size_t len, void* ctx) {
  StreamCtx* s = (StreamCtx*)ctx;
  while (s->remaining > 0 && millis() < s->deadline) {
    size_t want = min((size_t)s->remaining, len);
    int n = s->stream->read(dst, want);
    if (n > 0) {
      s->remaining -= n;
      // Progress resets the clock, so a slow network stretches the reply rather
      // than truncating it mid-word.
      s->deadline = millis() + HTTP_TIMEOUT_MS;
      return (size_t)n;
    }
    delay(2);
  }
  return 0;
}

bool gatewayPlay(const String& audioPath) {
  WiFiClient client;
  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(client, base() + audioPath)) return false;

  const char* wanted[] = {"X-Sample-Rate"};
  http.collectHeaders(wanted, 1);

  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    // 404 means the clip expired — the gateway holds them for two minutes and
    // at most 32 at once. Reaching this means the device took too long between
    // being told about a clip and asking for it.
    Serial.printf("[gw] audio failed: %d%s\n", code,
                  code == 404 ? " (clip expired)" : "");
    http.end();
    return false;
  }

  uint32_t rate = http.header("X-Sample-Rate").toInt();
  if (rate == 0) rate = 22050;      // the current piper voice, if the header is lost
  int len = http.getSize();

  StreamCtx ctx{http.getStreamPtr(),
                len > 0 ? (uint32_t)len : 0xFFFFFFFF,
                millis() + HTTP_TIMEOUT_MS};
  audioPlay(rate, streamPull, &ctx);
  http.end();
  return true;
}

bool gatewayPlayById(const String& audioId) {
  return gatewayPlay("/v1/audio/" + audioId);
}
