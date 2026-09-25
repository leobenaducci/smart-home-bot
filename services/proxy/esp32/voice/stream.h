// The microphone stream, and the control channel that comes back down it.
//
// The puck sends binary frames of 16 kHz mono PCM, continuously, forever. The
// gateway runs the wake word and the end-of-speech detection on that stream and
// sends back small JSON messages telling this device what to do — which is the
// entire reason there is no wake engine, no VAD and no recording buffer in this
// firmware.
//
//   up    binary          raw int16 mono 16 kHz
//   up    {"type":"resume"}      finished playing, listen again
//
//   down  {"type":"state","state":"idle|listening|thinking"}
//   down  {"type":"speak","audio_id":…,"text":…,"audio":{…}}
//   down  {"type":"error","message":…}
//
// A device that hears "speak" stops sending, fetches the clip over HTTP, plays
// it, and then says "resume". Not sending while the speaker is on is what keeps
// Alfred from waking himself — there is no echo canceller anywhere in this.
#pragma once

#include <Arduino.h>

enum StreamEvent {
  STREAM_CONNECTED,
  STREAM_DISCONNECTED,
  STREAM_IDLE,
  STREAM_LISTENING,
  STREAM_THINKING,
  STREAM_SPEAK,       // payload = audio id
  STREAM_ERROR,       // payload = message
};

typedef void (*StreamHandler)(StreamEvent event, const String& payload);

// `url` is the gateway base, e.g. http://compute.home:8083
bool streamBegin(const String& url, const String& token, StreamHandler handler);

// Pump the socket. Call from loop(), often.
void streamLoop();

bool streamConnected();

// Send one frame of microphone audio.
void streamSendAudio(const uint8_t* pcm, size_t len);

// Tell the gateway we have finished playing and it should listen again.
void streamResume();
