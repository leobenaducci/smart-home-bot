// The LED ring: this device's entire user interface.
//
// There is no screen. Someone standing in front of it can only know what it is
// doing from these twelve pixels, so every state the firmware can be in has a
// distinct look — including the ones that mean "something is wrong", which are
// otherwise invisible until somebody notices Alfred never answers.
#pragma once

#include <Arduino.h>

enum LedState {
  LED_BOOTING,    // white chase — WiFi, gateway, broker not all up yet
  LED_IDLE,       // one dim pixel breathing — armed, listening for the wake word
  LED_LISTENING,  // blue ring — recording; speak now
  LED_THINKING,   // amber comet — uploaded, waiting on Alfred
  LED_SPEAKING,   // green ring, dimming as it goes
  LED_LEARNING,   // cyan ring — IR receiver is on, point the remote and press
  LED_PORTAL,     // purple pulse — access point is up, go configure it
  LED_ERROR,      // three red flashes, then back to whatever was before
};

void ledsBegin();

// Set the steady state. Cheap and idempotent; call it as often as you like.
void ledsSet(LedState state);

// Must be called from loop(): the animations advance here rather than on a
// timer, so nothing paints while an I2S read or an HTTP body is in progress.
void ledsTick();

// A transient error flash that returns to the current state on its own.
void ledsError();
