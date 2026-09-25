// Alfred's voice puck — the thing you talk to in a room.
//
// Say "Alfred", speak, and Alfred answers out loud. It has no screen: an LED
// ring is the entire interface, which is why every state has a distinct look
// including the failures.
//
// **The wake word does not run here.** This device streams its microphone to
// the voice gateway continuously and does what it is told:
//
//   stream mic ──► gateway ──► "listening"  ring blue
//                          ──► "thinking"   ring amber
//                          ──► "speak"      fetch the clip, play it, resume
//
// That is not a shortcut, it is what makes the word "Alfred" possible at all:
// no on-device engine ships one, and Espressif's custom route wants 20,000
// corpus entries or a purchase order. Training our own and running it on the
// server means the model is ours, retuning it never means reflashing this, and
// this firmware has no wake engine, no VAD and no recording buffer in it.
//
// It costs nothing in availability — a puck that heard you with no gateway to
// ask has nothing to say — and it costs one real thing: the microphone streams
// to the house server whenever this is powered. It never leaves the LAN.
//
// See ../../../home-voice/README.md for the protocol and wakeword/README.md for
// the model. Read README.md before flashing: the board settings are not
// optional, and this sketch has never run on hardware.

#include <Arduino.h>
#include <ArduinoJson.h>
#include <PubSubClient.h>
#include <WiFi.h>

#include "audio.h"
#include "board_config.h"
#include "gateway.h"
#include "ir.h"
#include "leds.h"
#include "portal.h"
#include "sensors.h"
#include "stream.h"

Settings g_settings;             // read by gateway.cpp
static String g_room;
static WiFiClient g_mqttNet;
static PubSubClient g_mqtt(g_mqttNet);
static uint32_t g_mqttRetryAt = 0;

// Work handed from a callback to loop(). Neither the websocket event nor the
// MQTT callback may spend ten seconds playing audio: both run inside the poll
// they were dispatched from.
static String g_pendingClip;
static bool g_speaking = false;

// Same rule for infrared: an AC frame is ~200 ms of blocking bit-banging, and a
// learn waits up to half a minute for somebody to find the remote.
static String g_pendingIr;
static String g_learnRequestId;

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

static void runConfigPortal(const char* why) {
  Serial.printf("[boot] config portal: %s\n", why);
  ledsSet(LED_PORTAL);
  PortalInfo info = portalBegin();
  Serial.printf("[boot] join %s / %s then open http://%s\n",
                info.ssid.c_str(), info.password.c_str(), info.ip.c_str());

  uint32_t start = millis();
  while (millis() - start < PORTAL_TIMEOUT_MS) {
    ledsTick();
    if (portalRun(200)) {
      Serial.println("[boot] saved, restarting");
      ESP.restart();
    }
  }
  Serial.println("[boot] portal timed out");
  failureCountSet(0);
  ESP.restart();
}

static bool wifiConnect() {
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);            // a continuous uplink; mains-powered anyway
  WiFi.setAutoReconnect(true);
  WiFi.begin(g_settings.ssid.c_str(), g_settings.password.c_str());

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_TIMEOUT_MS) {
    ledsTick();
    delay(10);
  }
  if (WiFi.status() != WL_CONNECTED) return false;
  Serial.printf("[boot] wifi %s, rssi %d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  return true;
}

// Back to the resting ring — unless a learn is running, in which case the ring
// is the instruction. Somebody is standing there holding a remote waiting to be
// told when to press, and an idle frame from the gateway or an announcement
// landing mid-learn would quietly take that away.
static void ledsIdleUnlessLearning() {
  ledsSet(g_learnRequestId.isEmpty() ? LED_IDLE : LED_LEARNING);
}

static void onStream(StreamEvent event, const String& payload) {
  switch (event) {
    case STREAM_CONNECTED:   ledsIdleUnlessLearning(); break;
    case STREAM_DISCONNECTED:
      // The gateway is the whole function, so losing it is a fault worth
      // showing rather than a quiet degradation. On-device wake would not help
      // here: there would still be nobody to ask.
      ledsSet(LED_BOOTING);
      break;
    case STREAM_IDLE:        if (!g_speaking) ledsIdleUnlessLearning(); break;
    case STREAM_LISTENING:   ledsSet(LED_LISTENING); audioChirp(true); break;
    case STREAM_THINKING:    ledsSet(LED_THINKING); break;
    case STREAM_SPEAK:       g_pendingClip = payload; break;
    case STREAM_ERROR:       ledsError(); break;
  }
}

static void onMqtt(char* topic, byte* payload, unsigned int len) {
  const String t(topic);

  if (t.endsWith("/ir/set")) {
    // Kept as text and parsed in loop(): everything this leads to blocks, and
    // this runs inside g_mqtt.loop().
    g_pendingIr = String((const char*)payload, len);
    return;
  }

  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;
  const char* id = doc["audio_id"] | "";
  if (!*id) return;
  // Only the newest matters: if two announcements land while one is playing,
  // the second is the one worth hearing.
  g_pendingClip = id;
  Serial.printf("[mqtt] announce: %s\n", (const char*)(doc["text"] | ""));
}

static void mqttEnsure() {
  if (g_mqtt.connected() || g_room.isEmpty()) return;
  if (millis() < g_mqttRetryAt) return;
  g_mqttRetryAt = millis() + MQTT_RETRY_MS;

  // A unique client id per device: a fixed one makes the broker evict the other
  // session and both clients flap (docs/mqtt-conventions.md).
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char id[32];
  snprintf(id, sizeof(id), "voice-%02X%02X%02X", mac[3], mac[4], mac[5]);

  if (g_mqtt.connect(id)) {
    String topic = "home-voice/" + g_room + "/announce";
    g_mqtt.subscribe(topic.c_str(), 1);
    String irTopic = "home-voice/" + g_room + "/ir/set";
    g_mqtt.subscribe(irTopic.c_str(), 1);
    Serial.printf("[mqtt] connected, subscribed %s and %s\n", topic.c_str(), irTopic.c_str());
  }
}

// ---------------------------------------------------------------------------
// Infrared
// ---------------------------------------------------------------------------

// Every IR command is answered, including the ones that failed. The gateway is
// standing in an HTTP request waiting for this, and Alfred is standing behind
// that telling somebody whether their television turned on; "no reply" and "it
// did not work" have to look different from up there.
static void irReply(const String& requestId, bool ok, const char* detail, const IrCode* code) {
  if (!g_mqtt.connected()) return;

  JsonDocument doc;
  doc["request_id"] = requestId;
  doc["ok"] = ok;
  doc["room"] = g_room;
  if (detail && *detail) doc["detail"] = detail;
  if (code != nullptr) {
    if (!code->protocol.isEmpty()) doc["protocol"] = code->protocol;
    if (code->bits) doc["bits"] = code->bits;
    if (code->isState()) doc["state"] = code->state;
    else if (!code->isRaw()) doc["code"] = code->value;
    if (code->isRaw()) {
      JsonArray raw = doc["raw"].to<JsonArray>();
      for (uint16_t v : code->raw) raw.add(v);
      doc["khz"] = code->khz;
    }
  }

  String out;
  serializeJson(doc, out);
  // QoS 0: PubSubClient publishes at no other level. The gateway is already
  // waiting on this exact request id with a timeout, so a lost reply reads as
  // "the puck did not answer" rather than as silence — which is the same thing
  // an unplugged puck produces, and wants the same fix.
  String topic = "home-voice/" + g_room + "/ir/event";
  g_mqtt.publish(topic.c_str(), out.c_str());
}

static void handleIrCommand(const String& payload) {
  JsonDocument doc;
  if (deserializeJson(doc, payload)) {
    Serial.println("[ir] unparseable command");
    return;
  }

  const String action = doc["action"] | "send";
  const String requestId = doc["request_id"] | "";

  if (action == "cancel") {
    irLearnCancel();
    g_learnRequestId = "";
    ledsSet(LED_IDLE);
    irReply(requestId, true, "cancelled", nullptr);
    return;
  }

  if (action == "learn") {
    if (!irCanLearn()) {
      irReply(requestId, false, "this puck has no IR receiver", nullptr);
      return;
    }
    // A second learn while one is running: answer the first rather than
    // abandoning it, or its caller sits there until it times out and is told
    // the puck is unreachable — which is the one thing that did not happen.
    if (!g_learnRequestId.isEmpty()) {
      irReply(g_learnRequestId, false, "superseded by another learn", nullptr);
      irLearnCancel();
    }
    g_learnRequestId = requestId;
    ledsSet(LED_LEARNING);
    irLearnBegin((uint32_t)(doc["timeout_s"] | 0) * 1000UL);
    return;
  }

  if (action != "send") {
    irReply(requestId, false, "unknown action", nullptr);
    return;
  }

  if (!irCanSend()) {
    irReply(requestId, false, "this puck has no IR blaster", nullptr);
    return;
  }

  bool ok = false;
  if (doc["ac"].is<JsonObject>()) {
    // A described state, not a captured one — this is the path that can reach
    // a temperature nobody ever pressed on the original remote.
    JsonObject ac = doc["ac"];
    IrAcState state;
    state.protocol = ac["protocol"] | "";
    state.power = ac["power"] | true;
    state.mode = ac["mode"] | "cool";
    state.degrees = ac["degrees"] | 23.0f;
    state.fan = ac["fan"] | "auto";
    state.swingv = ac["swingv"] | false;
    state.quiet = ac["quiet"] | false;
    ok = irSendAc(state);
  } else {
    IrCode code;
    code.protocol = doc["protocol"] | "";
    code.value = doc["code"] | (uint64_t)0;
    code.bits = doc["bits"] | (uint16_t)0;
    code.state = doc["state"] | "";
    code.khz = doc["khz"] | (uint16_t)38;
    code.repeats = doc["repeats"] | (uint16_t)0;
    for (JsonVariant v : doc["raw"].as<JsonArray>()) code.raw.push_back(v.as<uint16_t>());
    ok = irSend(code);
  }

  // The speaker was silent, but the microphone was not: the send blocked the
  // loop for as long as the frame took, and what is sitting in the I2S buffer
  // is the room from before. Same reasoning as playClip.
  audioFlushInput();
  irReply(requestId, ok, ok ? nullptr : "the code was not sendable", nullptr);
}

// ---------------------------------------------------------------------------
// Sensors
// ---------------------------------------------------------------------------

static void publishSensors(const SensorReading& r, bool isEvent) {
  if (!g_mqtt.connected()) return;

  JsonDocument doc;
  doc["room"] = g_room;
  if (r.haveClimate) {
    // One decimal. A tenth of a degree is already past what these parts are
    // accurate to, and the extra digits are noise travelling to a dashboard.
    doc["celsius"] = roundf(r.celsius * 10) / 10.0f;
    doc["humidity"] = roundf(r.humidity * 10) / 10.0f;
  }
  if (r.haveLux) doc["lux"] = roundf(r.lux);
  if (r.havePir) {
    doc["motion"] = r.motion;
    doc["seconds_since_motion"] = r.secondsSinceMotion;
  }

  String out;
  serializeJson(doc, out);

  // Two topics, per docs/mqtt-conventions.md: `state` is retained so anything that
  // subscribes later — a dashboard, or Alfred being asked how warm it is —
  // gets an answer without waiting a minute for the next reading. `event` is
  // not retained, because "somebody moved" is only true when it arrives.
  String topic = "home-voice/" + g_room + "/sensors/" + (isEvent ? "event" : "state");
  g_mqtt.publish(topic.c_str(), out.c_str(), !isEvent);
}

static void sensorsPublishTick() {
  SensorReading r;
  bool isEvent = false;
  if (sensorsTick(r, isEvent)) publishSensors(r, isEvent);
}

static void irLearnTick() {
  if (g_learnRequestId.isEmpty()) return;

  IrCode learned;
  switch (irLearnPoll(learned)) {
    case IR_LEARN_GOT:
      irReply(g_learnRequestId, true, nullptr, &learned);
      g_learnRequestId = "";
      ledsSet(LED_IDLE);
      audioChirp(true);            // it caught something; say so in the room
      break;
    case IR_LEARN_TIMEOUT:
      irReply(g_learnRequestId, false, "nothing arrived", nullptr);
      g_learnRequestId = "";
      ledsSet(LED_IDLE);
      break;
    default:
      break;                       // still waiting, or not learning at all
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[boot] Alfred voice");

  ledsBegin();
  ledsSet(LED_BOOTING);

  bool configured = settingsLoad(g_settings);
  uint32_t fails = failureCount();
  if (!configured) runConfigPortal("no settings stored");
  else if (doubleResetDetected()) runConfigPortal("double reset");
  else if (fails >= PORTAL_AFTER_FAILURES) runConfigPortal("too many failures");

  if (!audioBegin()) {
    Serial.println("[boot] FATAL: audio unavailable");
    for (;;) { ledsSet(LED_ERROR); ledsTick(); delay(10); }
  }

  if (!wifiConnect()) {
    failureCountSet(fails + 1);
    Serial.println("[boot] wifi failed, restarting");
    delay(2000);
    ESP.restart();
  }

  if (!gatewayWhoAmI(g_room)) {
    // Reachable and refused is a configuration problem a reboot will not fix,
    // so count it: enough of these and the portal opens by itself.
    failureCountSet(fails + 1);
    Serial.println("[boot] could not learn my room, restarting");
    delay(5000);
    ESP.restart();
  }
  Serial.printf("[boot] I am in: %s\n", g_room.c_str());
  failureCountSet(0);

  irBegin();
  sensorsBegin();

  g_mqtt.setServer(MQTT_HOST, MQTT_PORT);
  g_mqtt.setCallback(onMqtt);
  // PubSubClient's stock buffer is 256 bytes, which is fine for an announce and
  // nowhere near a learned code: an unrecognised remote comes back as several
  // hundred timings, and a message over the buffer is dropped silently at both
  // ends. This is the whole reason a learn would "work" and report nothing.
  g_mqtt.setBufferSize(MQTT_BUFFER_BYTES);
  mqttEnsure();

  if (!streamBegin(g_settings.url, g_settings.token, onStream)) {
    Serial.println("[boot] FATAL: bad gateway url");
    for (;;) { ledsSet(LED_ERROR); ledsTick(); delay(10); }
  }

  Serial.println("[boot] ready — streaming");
}

// ---------------------------------------------------------------------------
// Playing a reply
// ---------------------------------------------------------------------------

static void playClip(const String& audioId) {
  g_speaking = true;
  ledsSet(LED_SPEAKING);
  ledsTick();

  if (!gatewayPlayById(audioId)) ledsError();

  // Everything the microphone picked up while the speaker was on is Alfred, and
  // the gateway would run its wake model over it. Throw it away before sending
  // another byte.
  audioFlushInput();

  g_speaking = false;
  ledsIdleUnlessLearning();
  streamResume();
}

void loop() {
  ledsTick();
  streamLoop();

  if (WiFi.status() != WL_CONNECTED) {
    ledsSet(LED_BOOTING);
    delay(100);
    return;
  }

  mqttEnsure();
  if (g_mqtt.connected()) g_mqtt.loop();

  if (!g_pendingIr.isEmpty()) {
    String cmd = g_pendingIr;
    g_pendingIr = "";
    handleIrCommand(cmd);
    return;
  }
  irLearnTick();
  sensorsPublishTick();

  if (!g_pendingClip.isEmpty()) {
    String id = g_pendingClip;
    g_pendingClip = "";
    playClip(id);
    return;
  }

  // The steady state: 40 ms of microphone, up the socket, forever.
  if (streamConnected() && !g_speaking) {
    static uint8_t frame[MIC_FRAME_BYTES];
    size_t n = audioReadFrame(frame, sizeof(frame));
    if (n > 0) streamSendAudio(frame, n);
  } else {
    delay(5);
  }
}
