// The house lights, over MQTT with an HTTP safety net.
//
// State arrives on the retained topic `home-lights/lights/state`, so a panel
// that has just connected gets every light immediately and then only deltas.
// That is why this is not a poller.
//
// Two facts from the lights server decide the whole design:
//
//   * There is no `toggle` over HTTP. /api/control takes on|off|brightness|rgb
//     and nothing else; toggle exists only on the MQTT `set` topic. A panel
//     whose main verb is "toggle" therefore wants the broker.
//   * The broker runs `persistence false`, so retained state does NOT survive a
//     broker restart (docs/mqtt-conventions.md). Subscribing alone can leave a panel
//     staring at an empty list forever, which is why boot also does one HTTP
//     GET /api/state and why a reconnect re-does it.
#pragma once

#include <Arduino.h>
#include <PubSubClient.h>

struct Light {
  String mac;
  String name;
  bool on = false;
  uint8_t brightness = 0;
};

// Twenty-four is far past what this house has and still only a few KB.
#define LIGHTS_MAX 24

void lightsBegin(PubSubClient* mqtt);

// Subscribe. Call after every (re)connect — subscriptions do not survive one.
void lightsSubscribe();

// One HTTP snapshot. The guaranteed path when retained state is missing.
bool lightsFetchHttp();

// Feed every MQTT message here; it ignores topics it does not own.
// Returns true if the light list changed and the UI should be rebuilt.
bool lightsOnMqtt(const char* topic, const uint8_t* payload, unsigned int len);

uint16_t lightsCount();
const Light* lightsAt(uint16_t i);

// Flip one light. Publishes to its `set` topic; the resulting state comes back
// as a normal retained update, so nothing here guesses at the new value.
void lightsToggle(uint16_t i);
void lightsSetBrightness(uint16_t i, uint8_t brightness);
