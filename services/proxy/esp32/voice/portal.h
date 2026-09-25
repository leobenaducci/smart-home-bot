// WiFi provisioning over a temporary access point.
//
// THIS FILE IS A COPY. The same provisioning exists, near-identically, in
// ../menu/portal.h and ../control/portal.h. The three panels share no library
// on purpose — different MCUs, different displays, different build settings —
// so a fix here is a fix that must be applied in all three. What differs
// between the copies: the NVS namespace, the secrets.h macro names, the AP name
// prefix, and the strings on the form.
//
// The puck has no screen and no keyboard, so the only way in is to make it
// briefly become an access point and serve a form. Settings live in NVS, which
// survives power loss and reflashing.
//
// Unlike the menu panel, this device is mains-powered, so the portal costs
// nothing but the time it is open. It still times out: an access point with a
// configuration form on it is not a thing to leave running on a wall.
#pragma once

#include <Arduino.h>

struct Settings {
  String ssid;
  String password;
  String url;      // the voice gateway, e.g. http://compute.home:8083
  String token;    // this device's entry in the gateway's devices.json

  bool complete() const { return ssid.length() && url.length() && token.length(); }
};

// Load from NVS, falling back to the compile-time defaults in secrets.h when a
// key has never been written. Returns false when there is nothing usable, which
// is the signal to open the portal.
bool settingsLoad(Settings& out);

// True when the user asked for the portal by tapping RESET twice.
//
// Costs a DRD_WINDOW_MS stall, so it is only called on a fresh power-up.
bool doubleResetDetected();

// Count of consecutive failed connect-or-fetch cycles, persisted across reboots.
uint32_t failureCount();
void failureCountSet(uint32_t n);

struct PortalInfo {
  String ssid;        // AP name the phone should join
  String password;    // AP passphrase
  String ip;          // where the form lives, always 192.168.4.1
};

// Bring up the AP and the HTTP/DNS servers.
PortalInfo portalBegin();

// Serve the form until settings are saved or *timeout_ms* elapses.
// Returns true when something was saved (the caller should then reboot).
bool portalRun(uint32_t timeout_ms);
