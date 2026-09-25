// WiFi provisioning over a temporary access point.
//
// The panel has no touchscreen, no buttons of its own and no serial console
// once it's on a wall, so the only way in is to make it briefly become an
// access point and serve a form. Settings live in NVS, which survives deep
// sleep, power loss and reflashing.
//
// **AP mode is by far the most expensive state this device has.** The radio
// runs continuously at ~120 mA with no sleeping, so ten minutes of portal costs
// roughly what three days of normal hourly operation does. Everything here is
// shaped by that: the portal always times out, it is never entered
// speculatively, and it is never re-entered in a loop. See README.md.
#pragma once

#include <Arduino.h>

struct Settings {
  String ssid;
  String password;
  String url;
  String token;

  bool complete() const { return ssid.length() && url.length(); }
};

// Load from NVS, falling back to the compile-time defaults in secrets.h when a
// key has never been written. Returns false when there is nothing usable, which
// is the signal to open the portal.
bool settingsLoad(Settings& out);

// True when the user asked for the portal by tapping RESET twice.
//
// Costs a DRD_WINDOW_MS stall, so it must only be called on boots that a human
// caused. Calling it on a timer wake would add that delay to every wake of
// every day, for a button nobody pressed.
bool doubleResetDetected();

// Count of consecutive failed connect-or-fetch cycles, persisted across sleeps.
uint32_t failureCount();
void failureCountSet(uint32_t n);

struct PortalInfo {
  String ssid;        // AP name the phone should join
  String password;    // AP passphrase
  String ip;          // where the form lives, always 192.168.4.1
};

// Bring up the AP and the HTTP/DNS servers. Returns what to print on the panel.
PortalInfo portalBegin();

// Serve the form until settings are saved or *timeout_ms* elapses.
// Returns true when something was saved (the caller should then reboot).
bool portalRun(uint32_t timeout_ms);
