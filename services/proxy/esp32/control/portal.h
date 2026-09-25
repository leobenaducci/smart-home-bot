// WiFi provisioning over a temporary access point.
//
// THIS FILE IS A COPY. The same provisioning exists, near-identically, in
// ../menu/portal.h and ../voice/portal.h. The three panels share no library
// on purpose — different MCUs, different displays, different build settings —
// so a fix here is a fix that must be applied in all three. What differs
// between the copies: the NVS namespace, the secrets.h macro names, the AP name
// prefix, and the strings on the form.
//
// This panel has a touchscreen, so in principle it could ask for a password on
// its own screen. It does not: an on-screen keyboard is a lot of LVGL for
// something used twice in a device's life, and the phone-joins-an-AP flow is
// already written and already understood in this house.
//
// Mains-powered, so the portal costs nothing but the time it is open. It still
// times out: an access point with a configuration form on it is not a thing to
// leave running on a wall.
#pragma once

#include <Arduino.h>

struct Settings {
  String ssid;
  String password;
  String url;      // unused by this panel; service URLs are compiled in
  String token;    // unused by this panel; the lights and camera APIs have no auth

  // Only WiFi matters here. The panel talks to two LAN services that ask for no
  // credential at all, so there is nothing else to provision.
  bool complete() const { return ssid.length(); }
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
