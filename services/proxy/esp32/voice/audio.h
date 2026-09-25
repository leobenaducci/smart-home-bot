// Microphone capture and speaker playback.
//
// Two separate I2S peripherals, not one reconfigured between phases: the mic
// runs at 16 kHz because that is what whisper wants, and the reply arrives at
// the piper voice's own 22050 Hz. The S3 has two I2S controllers, so this costs
// nothing but pins and removes a teardown/rebuild from the middle of every turn.
//
// There is no recording buffer and no voice-activity detection here. Both live
// on the gateway now, along with the wake word — this device streams its
// microphone and plays what it is told, and that is the whole of it.
#pragma once

#include <Arduino.h>
#include <ESP_I2S.h>

bool audioBegin();

// Read one frame of microphone audio, 16 kHz mono signed 16-bit. Returns bytes
// written, which may be 0 if nothing is ready yet.
size_t audioReadFrame(uint8_t* dst, size_t len);

// Throw away whatever the mic captured while we were not listening.
//
// Without this, the first thing sent after Alfred finishes speaking is the tail
// of Alfred speaking, buffered by the I2S driver — which the gateway would
// happily run its wake model over.
void audioFlushInput();

// Play raw signed 16-bit mono PCM at `sampleRate`, pulled in chunks by `next`,
// which fills `dst` with up to `len` bytes and returns how many it wrote, or 0
// when the stream is finished.
//
// Streaming rather than taking a buffer: the reply comes over HTTP and there is
// no reason to hold it. Chunks go from the socket into the I2S DMA and are
// forgotten.
typedef size_t (*AudioPullFn)(uint8_t* dst, size_t len, void* ctx);
void audioPlay(uint32_t sampleRate, AudioPullFn next, void* ctx);

// A short two-tone chirp, so a person knows they were heard even when the ring
// is behind them or they are not looking at it.
void audioChirp(bool rising);
