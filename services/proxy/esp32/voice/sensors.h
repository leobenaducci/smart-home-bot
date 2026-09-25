// What the room is like: temperature, humidity, light, and whether anyone is in it.
//
// All four are optional and independent (see board_config.h). A puck built
// without any of them compiles to nothing here and behaves exactly as before.
//
// **Why on this device.** It is already mains-powered, already in the middle of
// the room and already holds an MQTT session. A separate sensor node would be a
// second thing to build, flash, name, and find a socket for — and it would sit
// in the same room reporting the same numbers.
//
// **Why it matters to Alfred specifically.** He has an IR blaster in the same
// enclosure. "Hace calor" is answerable, and so is doing something about it,
// without either half knowing about the other: the sensors publish, the blaster
// subscribes, and the decision happens where the language does.
//
// Presence is the one to be careful with. A PIR says "something moved", never
// who, and this house's rule is that a room device holds nothing personal — so
// what goes on the broker is a boolean and a timestamp, and it is a room's
// property, never a person's.
#pragma once

#include <Arduino.h>

struct SensorReading {
  bool haveClimate = false;
  float celsius = 0;
  float humidity = 0;

  bool haveLux = false;
  float lux = 0;

  bool havePir = false;
  bool motion = false;
  // Seconds since motion was last seen. Far more useful than the instantaneous
  // bit: a PIR is high for two seconds and low for the other fifty-eight of a
  // minute in which somebody is quietly reading in the room.
  uint32_t secondsSinceMotion = 0;
};

// Brings up I2C and whichever parts answer. Safe to call when none are fitted;
// a sensor that does not answer is simply absent for the rest of the run, not
// an error — a puck with a dead BH1750 should still be a working microphone.
void sensorsBegin();

// True if anything at all was detected at boot.
bool sensorsPresent();

// Call from loop(). Reads on its own schedule and does nothing in between.
// Returns true when there is something worth publishing: the periodic tick, a
// move worth reporting, or a number that changed by more than noise.
bool sensorsTick(SensorReading& out, bool& outIsEvent);

// The current values without deciding whether they are worth publishing — for
// a reply to somebody who just asked.
SensorReading sensorsRead();
