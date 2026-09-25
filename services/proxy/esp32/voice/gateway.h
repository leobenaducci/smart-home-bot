// The voice gateway client.
//
// The HTTP half of talking to the gateway: which room am I, and fetch a clip.
// The microphone goes up a websocket instead — see stream.h.
//
// This device does not know that whisper, nanobot or piper exist, holds no
// credential but its own device token, and never parses a chat API — that is
// the whole point of the gateway sitting in front (see home-voice/README.md).
//
// Plain HTTP, no TLS: the gateway is LAN-only, like the lights API. No
// setInsecure, no certificate, nothing to expire.
#pragma once

#include <Arduino.h>

// GET /v1/whoami — which room am I in?
//
// Asked at boot rather than configured, so the room lives in exactly one place
// (the gateway's devices.json). Moving this puck to another room is an edit
// there and a reboot here, with no risk of the two disagreeing.
bool gatewayWhoAmI(String& roomOut);

// Fetch a clip and play it, streaming straight from the socket into I2S — the
// reply is never held whole. The id arrives either on the websocket ("speak")
// or over MQTT (an announcement); both are the same clip store.
//
// The gateway also still offers a one-shot POST /v1/turn for a device that
// would rather record than stream. This firmware does not use it.
bool gatewayPlay(const String& audioPath);
bool gatewayPlayById(const String& audioId);
