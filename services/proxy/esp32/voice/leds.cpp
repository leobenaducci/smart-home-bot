#include "leds.h"
#include "board_config.h"

#include <Adafruit_NeoPixel.h>

static Adafruit_NeoPixel g_ring(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);
static LedState g_state = LED_BOOTING;
static uint32_t g_phase = 0;         // animation step, advanced by ledsTick
static uint32_t g_lastTick = 0;
static uint32_t g_errorUntil = 0;

// One frame every 40 ms. Fast enough that a chase looks like motion, slow
// enough that it never competes with an I2S read for time.
static const uint32_t FRAME_MS = 40;

static void fill(uint8_t r, uint8_t g, uint8_t b) {
  for (uint16_t i = 0; i < LED_COUNT; i++) g_ring.setPixelColor(i, r, g, b);
}

// A single lit pixel walking around the ring, with a fading tail behind it.
static void comet(uint8_t r, uint8_t g, uint8_t b, uint8_t tail) {
  g_ring.clear();
  for (uint8_t i = 0; i <= tail; i++) {
    uint16_t idx = (g_phase - i + LED_COUNT * 4) % LED_COUNT;
    // Integer division rather than a float curve: the difference is invisible
    // at twelve pixels and this runs on every frame.
    uint8_t scale = 255 / (i + 1);
    g_ring.setPixelColor(idx, (r * scale) / 255, (g * scale) / 255, (b * scale) / 255);
  }
}

// 0..255..0 triangle over `period` frames — a breath, not a blink.
static uint8_t breathe(uint16_t period) {
  uint16_t p = g_phase % period;
  uint16_t half = period / 2;
  return p < half ? (p * 255) / half : ((period - p) * 255) / half;
}

void ledsBegin() {
  g_ring.begin();
  g_ring.setBrightness(LED_BRIGHTNESS);
  g_ring.clear();
  g_ring.show();
}

void ledsSet(LedState state) {
  if (g_state == state) return;
  g_state = state;
  g_phase = 0;
}

void ledsError() {
  g_errorUntil = millis() + 900;
}

void ledsTick() {
  uint32_t now = millis();
  if (now - g_lastTick < FRAME_MS) return;
  g_lastTick = now;
  g_phase++;

  // The error flash overrides whatever is underneath and then simply stops,
  // which is why it does not need to remember the previous state.
  if (now < g_errorUntil) {
    bool on = ((g_errorUntil - now) / 150) % 2;
    fill(on ? 255 : 0, 0, 0);
    g_ring.show();
    return;
  }

  switch (g_state) {
    case LED_BOOTING:
      comet(255, 255, 255, 3);
      break;

    case LED_IDLE: {
      // Deliberately almost nothing: this is the state the device is in for
      // 23 hours a day, in rooms people sleep in. One pixel, breathing.
      uint8_t v = breathe(120) / 6;
      g_ring.clear();
      g_ring.setPixelColor(0, v, v, v);
      break;
    }

    case LED_LISTENING:
      // Solid, not animated. Motion here reads as "working"; what it needs to
      // say is "your voice is going in right now", which is a steady light.
      fill(0, 80, 255);
      break;

    case LED_THINKING:
      comet(255, 150, 0, 5);
      break;

    case LED_SPEAKING:
      fill(0, breathe(60) / 2 + 60, 0);
      break;

    case LED_LEARNING:
      // Somebody is standing in front of this holding a remote, waiting to be
      // told when to press. A slow cyan breath is legible as "now", where the
      // steady blue of LISTENING would read as "it is already recording me".
      fill(0, breathe(50), breathe(50));
      break;

    case LED_PORTAL:
      fill(breathe(80) / 2, 0, breathe(80));
      break;

    case LED_ERROR:
      fill(255, 0, 0);
      break;
  }
  g_ring.show();
}
