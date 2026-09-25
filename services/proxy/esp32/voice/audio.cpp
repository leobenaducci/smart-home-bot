#include "audio.h"
#include "board_config.h"

#include <math.h>

static I2SClass micBus;
static I2SClass spkBus;

static uint32_t g_spkRate = 0;      // what spkBus is currently configured for

bool audioBegin() {
  micBus.setPins(MIC_BCLK, MIC_WS, -1 /*dout*/, MIC_DIN);
  if (!micBus.begin(I2S_MODE_STD, MIC_SAMPLE_RATE, I2S_DATA_BIT_WIDTH_16BIT,
                    I2S_SLOT_MODE_MONO)) {
    Serial.println("[audio] mic I2S failed to start");
    return false;
  }

#if SPK_ENABLE >= 0
  pinMode(SPK_ENABLE, OUTPUT);
  digitalWrite(SPK_ENABLE, LOW);   // amp off until there is something to say
#endif

  Serial.println("[audio] ready");
  return true;
}

size_t audioReadFrame(uint8_t* dst, size_t len) {
  return micBus.readBytes((char*)dst, len);
}

void audioFlushInput() {
  // Read until the driver stops handing anything back, with a bound so a mic
  // that streams forever cannot trap us here.
  uint8_t scratch[512];
  for (int i = 0; i < 64; i++) {
    if (micBus.readBytes((char*)scratch, sizeof(scratch)) == 0) return;
  }
}

// The speaker bus is (re)started only when the rate changes. Piper's rate is
// fixed per voice, so in practice this configures once and then never again —
// but the gateway sends the rate with every clip precisely so that changing the
// voice does not mean reflashing every puck.
static bool spkEnsure(uint32_t sampleRate) {
  if (g_spkRate == sampleRate) return true;
  if (g_spkRate) spkBus.end();

  spkBus.setPins(SPK_BCLK, SPK_WS, SPK_DOUT, -1 /*din*/);
  if (!spkBus.begin(I2S_MODE_STD, sampleRate, I2S_DATA_BIT_WIDTH_16BIT,
                    I2S_SLOT_MODE_MONO)) {
    Serial.printf("[audio] speaker I2S failed at %u Hz\n", (unsigned)sampleRate);
    g_spkRate = 0;
    return false;
  }
  g_spkRate = sampleRate;
  return true;
}

void audioPlay(uint32_t sampleRate, AudioPullFn next, void* ctx) {
  if (!spkEnsure(sampleRate)) return;

#if SPK_ENABLE >= 0
  digitalWrite(SPK_ENABLE, HIGH);
  // The amp needs a moment before it will pass signal; without this the first
  // syllable is clipped, which reliably reads as Alfred mumbling.
  delay(5);
#endif

  uint8_t chunk[1024];
  for (;;) {
    size_t n = next(chunk, sizeof(chunk), ctx);
    if (n == 0) break;
    spkBus.write(chunk, n);
  }

#if SPK_ENABLE >= 0
  // Let the DMA drain before cutting the amp, or the tail of the last word is
  // swallowed along with the idle hiss this is here to kill.
  delay(60);
  digitalWrite(SPK_ENABLE, LOW);
#endif
}

struct ToneCtx { uint32_t left; float phase; float step; };

static size_t toneFill(uint8_t* dst, size_t len, void* ctx) {
  ToneCtx* t = (ToneCtx*)ctx;
  if (t->left == 0) return 0;
  size_t n = min((size_t)t->left, len / 2);
  int16_t* out = (int16_t*)dst;
  for (size_t i = 0; i < n; i++) {
    out[i] = (int16_t)(sinf(t->phase) * 6000);
    t->phase += t->step;
  }
  t->left -= n;
  return n * 2;
}

void audioChirp(bool rising) {
  const uint32_t rate = 16000;
  const uint32_t ms = 70;
  ToneCtx a{rate * ms / 1000, 0.0f, 2.0f * PI * (rising ? 660.0f : 990.0f) / rate};
  ToneCtx b{rate * ms / 1000, 0.0f, 2.0f * PI * (rising ? 990.0f : 660.0f) / rate};
  audioPlay(rate, toneFill, &a);
  audioPlay(rate, toneFill, &b);
}
