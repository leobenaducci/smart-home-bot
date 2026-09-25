// THIS FILE IS A COPY - see the note at the top of portal.h. The same code
// lives in ../menu/portal.cpp and ../voice/portal.cpp; a fix here is a fix
// that must be applied in all three.
#include "portal.h"
#include "board_config.h"

#include <Preferences.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>

// secrets.h is optional: with it you can flash a pre-provisioned puck, without
// it the portal is the only way in. Both paths are supported deliberately -
// the first is faster on the bench, the second is the only one that works for a
// panel already on a wall.
#if __has_include("secrets.h")
#include "secrets.h"
#endif
#ifndef WIFI_SSID
#define WIFI_SSID ""
#endif
#ifndef WIFI_PASSWORD
#define WIFI_PASSWORD ""
#endif
#ifndef CONTROL_URL
#define CONTROL_URL ""
#endif
#ifndef CONTROL_DEVICE_TOKEN
#define CONTROL_DEVICE_TOKEN ""
#endif

static const char* NVS_NS = "control";

static Preferences prefs;
static WebServer server(80);
static DNSServer dns;
static bool g_saved = false;

bool settingsLoad(Settings& out) {
  prefs.begin(NVS_NS, /*readOnly=*/true);
  out.ssid = prefs.getString("ssid", WIFI_SSID);
  out.password = prefs.getString("pass", WIFI_PASSWORD);
  out.url = prefs.getString("url", CONTROL_URL);
  out.token = prefs.getString("token", CONTROL_DEVICE_TOKEN);
  prefs.end();
  return out.complete();
}

static void settingsSave(const Settings& s) {
  prefs.begin(NVS_NS, /*readOnly=*/false);
  prefs.putString("ssid", s.ssid);
  prefs.putString("pass", s.password);
  prefs.putString("url", s.url);
  prefs.putString("token", s.token);
  prefs.end();
}

uint32_t failureCount() {
  prefs.begin(NVS_NS, true);
  uint32_t n = prefs.getUInt("fails", 0);
  prefs.end();
  return n;
}

void failureCountSet(uint32_t n) {
  // Only write when the value actually changes. The success path calls this
  // after every turn, and NVS is flash - rewriting a zero after every sentence
  // anyone speaks is wear for no reason.
  if (failureCount() == n) return;
  prefs.begin(NVS_NS, false);
  prefs.putUInt("fails", n);
  prefs.end();
}

bool doubleResetDetected() {
  prefs.begin(NVS_NS, false);
  bool armed = prefs.getBool("drd", false);
  if (armed) {
    prefs.putBool("drd", false);
    prefs.end();
    return true;
  }
  prefs.putBool("drd", true);
  prefs.end();

  delay(DRD_WINDOW_MS);        // a second RESET inside this window lands above

  prefs.begin(NVS_NS, false);
  prefs.putBool("drd", false);
  prefs.end();
  return false;
}

// ---------------------------------------------------------------------------
// The form
// ---------------------------------------------------------------------------
// Deliberately one self-contained page with no external anything: the phone
// joining this AP has no route to the internet, so a CDN stylesheet would
// simply hang. Spanish, to match the rest of the house.
static const char PAGE[] PROGMEM = R"HTML(<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Alfred</title><style>
body{font:16px system-ui,sans-serif;margin:0;padding:24px;background:#111;color:#eee}
h1{font-size:20px;margin:0 0 4px}p{color:#999;margin:0 0 20px;font-size:14px}
label{display:block;margin:14px 0 4px;font-size:13px;color:#bbb}
input{width:100%;padding:10px;font-size:16px;border:1px solid #444;border-radius:6px;
background:#1c1c1c;color:#eee;box-sizing:border-box}
button{width:100%;margin-top:22px;padding:13px;font-size:16px;border:0;border-radius:6px;
background:#2d6cdf;color:#fff}
</style>
<h1>Panel de la casa</h1><p>Configuraci&oacute;n de red</p>
<form method=post action=/save>
<label>Red WiFi (2,4 GHz)</label><input name=ssid value="%SSID%" required>
<label>Contrase&ntilde;a</label><input name=pass type=password value="">
<button type=submit>Guardar y reiniciar</button>
</form>)HTML";

static String escapeAttr(const String& v) {
  String o;
  for (size_t i = 0; i < v.length(); i++) {
    char c = v[i];
    if (c == '"') o += "&quot;";
    else if (c == '&') o += "&amp;";
    else if (c == '<') o += "&lt;";
    else o += c;
  }
  return o;
}

static void handleRoot() {
  Settings cur;
  settingsLoad(cur);
  String page = FPSTR(PAGE);
  page.replace("%SSID%", escapeAttr(cur.ssid));
  // The password is never sent back to the browser. Leaving it blank means
  // "keep what's stored", which handleSave() honours.
  server.send(200, "text/html; charset=utf-8", page);
}

static void handleSave() {
  Settings s;
  settingsLoad(s);                     // start from stored values

  if (server.hasArg("ssid")) s.ssid = server.arg("ssid");
  // Empty password field = unchanged, so re-saving the URL doesn't silently
  // wipe a working WiFi password.
  if (server.hasArg("pass") && server.arg("pass").length()) {
    s.password = server.arg("pass");
  }

  if (!s.complete()) {
    server.send(400, "text/html; charset=utf-8",
                "<meta charset=utf-8><p>Faltan datos. <a href=/>Volver</a>");
    return;
  }

  settingsSave(s);
  failureCountSet(0);
  g_saved = true;
  server.send(200, "text/html; charset=utf-8",
              "<meta charset=utf-8><p>Guardado. El panel se reinicia…");
  Serial.println("[portal] settings saved");
}

PortalInfo portalBegin() {
  WiFi.persistent(false);
  WiFi.mode(WIFI_AP);

  uint8_t mac[6];
  WiFi.softAPmacAddress(mac);
  char name[32];
  snprintf(name, sizeof(name), "AlfredPanel-%02X%02X", mac[4], mac[5]);

  WiFi.softAP(name, PORTAL_AP_PASSWORD);
  delay(100);                                   // let the AP settle before we read the IP

  IPAddress ip = WiFi.softAPIP();
  dns.start(53, "*", ip);                       // captive portal: everything -> us

  server.on("/", handleRoot);
  server.on("/save", HTTP_POST, handleSave);
  server.onNotFound(handleRoot);                // any probe URL lands on the form
  server.begin();

  Serial.printf("[portal] AP %s / %s at %s\n",
                name, PORTAL_AP_PASSWORD, ip.toString().c_str());

  PortalInfo info;
  info.ssid = name;
  info.password = PORTAL_AP_PASSWORD;
  info.ip = ip.toString();
  return info;
}

bool portalRun(uint32_t timeout_ms) {
  g_saved = false;
  uint32_t start = millis();
  while (millis() - start < timeout_ms) {
    dns.processNextRequest();
    server.handleClient();
    if (g_saved) {
      delay(800);                 // let the confirmation page reach the phone
      return true;
    }
    delay(2);
  }
  Serial.println("[portal] timeout");
  return false;
}
