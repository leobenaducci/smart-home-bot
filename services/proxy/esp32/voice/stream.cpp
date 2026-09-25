#include "stream.h"
#include "board_config.h"

#include <ArduinoJson.h>
#include <WebSocketsClient.h>

static WebSocketsClient g_ws;
static StreamHandler g_handler = nullptr;
static bool g_connected = false;

// Split "http://compute.home:8083" into its parts. Deliberately tiny rather
// than a URL library: the gateway address is the only URL this device ever
// sees, and it is typed into the config portal by a person.
static bool parseUrl(const String& url, String& host, uint16_t& port) {
  String u = url;
  int scheme = u.indexOf("://");
  if (scheme >= 0) u = u.substring(scheme + 3);
  while (u.endsWith("/")) u.remove(u.length() - 1);

  int colon = u.indexOf(':');
  if (colon < 0) {
    host = u;
    port = 80;
  } else {
    host = u.substring(0, colon);
    port = (uint16_t)u.substring(colon + 1).toInt();
  }
  return host.length() > 0 && port > 0;
}

static void onEvent(WStype_t type, uint8_t* payload, size_t len) {
  switch (type) {
    case WStype_CONNECTED:
      g_connected = true;
      Serial.println("[stream] connected");
      if (g_handler) g_handler(STREAM_CONNECTED, "");
      break;

    case WStype_DISCONNECTED:
      if (g_connected) Serial.println("[stream] disconnected");
      g_connected = false;
      if (g_handler) g_handler(STREAM_DISCONNECTED, "");
      break;

    case WStype_TEXT: {
      JsonDocument doc;
      if (deserializeJson(doc, payload, len)) return;
      String type_ = (const char*)(doc["type"] | "");

      if (type_ == "state") {
        String s = (const char*)(doc["state"] | "");
        if (s == "listening") g_handler(STREAM_LISTENING, "");
        else if (s == "thinking") g_handler(STREAM_THINKING, "");
        else g_handler(STREAM_IDLE, "");
      } else if (type_ == "speak") {
        Serial.printf("[stream] reply: %s\n", (const char*)(doc["text"] | ""));
        g_handler(STREAM_SPEAK, String((const char*)(doc["audio_id"] | "")));
      } else if (type_ == "error") {
        String m = (const char*)(doc["message"] | "");
        Serial.printf("[stream] gateway error: %s\n", m.c_str());
        g_handler(STREAM_ERROR, m);
      }
      break;
    }

    default:
      break;
  }
}

bool streamBegin(const String& url, const String& token, StreamHandler handler) {
  String host;
  uint16_t port;
  if (!parseUrl(url, host, port)) {
    Serial.printf("[stream] cannot parse gateway url: %s\n", url.c_str());
    return false;
  }
  g_handler = handler;

  // The token goes in the query string rather than a header: it is what the
  // gateway reads, and it matches how nanobot's own websocket is gated.
  String path = "/v1/stream?token=" + token;
  Serial.printf("[stream] connecting to ws://%s:%u%s\n", host.c_str(), port,
                "/v1/stream?token=***");

  g_ws.begin(host, port, path);
  g_ws.onEvent(onEvent);
  // The library reconnects on its own, which is the whole reason to use it:
  // a puck that loses the gateway must come back without anyone power-cycling
  // it. Ten seconds is slow enough not to hammer a server that is down.
  g_ws.setReconnectInterval(10000);
  return true;
}

void streamLoop() { g_ws.loop(); }

bool streamConnected() { return g_connected; }

void streamSendAudio(const uint8_t* pcm, size_t len) {
  if (!g_connected || len == 0) return;
  g_ws.sendBIN((uint8_t*)pcm, len);
}

void streamResume() {
  if (!g_connected) return;
  g_ws.sendTXT("{\"type\":\"resume\"}");
}
