// Infrared: the puck as a blaster, and as a remote-control learner.
//
// Two jobs that share one library. Sending is how "Alfred, prende el aire"
// reaches an appliance that has no network and never will. Learning is how a
// code gets into the house in the first place: point the original remote at
// the puck, press the button, and whatever came out of it is captured and
// handed back for Alfred to name.
//
// **A code has three possible shapes and all three are first-class.** Most
// remotes are a known protocol carrying a value that fits in 64 bits (NEC,
// Sony, Samsung) — those round-trip as protocol + code + bits. Air conditioner
// remotes send their whole state in one frame, hundreds of bits of it, because
// there is no "warmer" button on the wire, only "cool, 23 degrees, fan auto,
// power on" repeated in full; those round-trip as protocol + a state blob. And
// a remote the library does not recognise still has timings, so those
// round-trip as a raw array. Anything that can be captured can be replayed,
// which is the property that matters: the house never has to care whether a
// remote is famous.
//
// Sending an AC by *state* rather than by captured blob is what makes "subilo
// a 23" work without having captured 23 separately — see irSendAc.
//
// Both halves are optional at compile time (IR_TX_PIN / IR_RX_PIN set to -1),
// and the receiver is only powered up while a learn is actually in progress.
#pragma once

#include <Arduino.h>

#include <vector>

// What a capture or a send is carrying. Mirrors the JSON the gateway speaks,
// so translating between them stays a field-by-field copy with no cleverness.
struct IrCode {
  // "NEC", "COOLIX", "SAMSUNG_AC"… empty for a raw capture. Names are
  // IRremoteESP8266's own, so a code learned here can be sent by any other
  // tool that uses the same library.
  String protocol;
  uint64_t value = 0;      // protocol codes that fit in 64 bits
  uint16_t bits = 0;
  // Long-form state, for the air conditioners. Hex, no 0x, two chars a byte —
  // the same text IRremoteESP8266's own tools print, so it can be pasted.
  String state;
  // Raw timings in microseconds, mark/space alternating, when nothing else
  // recognised it.
  std::vector<uint16_t> raw;
  uint16_t khz = 38;       // carrier for a raw send; ignored for the rest
  uint16_t repeats = 0;

  bool isRaw() const { return !raw.empty(); }
  bool isState() const { return !state.isEmpty(); }
  bool valid() const { return isRaw() || isState() || (!protocol.isEmpty() && bits > 0); }
};

// A full air-conditioner state. Sent as one frame, because that is the only
// thing an AC remote knows how to say.
struct IrAcState {
  String protocol;         // "COOLIX", "DAIKIN", "MITSUBISHI_AC"…
  bool power = true;
  String mode = "cool";    // cool | heat | dry | fan | auto
  float degrees = 23;
  String fan = "auto";     // auto | min | low | medium | high | max
  bool swingv = false;
  bool quiet = false;
};

// Sets up the sender. Cheap, and safe to call when IR_TX_PIN is -1.
void irBegin();

// True when this puck was built with the part. The gateway is told at boot, so
// a room with no blaster can be reported as such instead of silently
// swallowing every command.
bool irCanSend();
bool irCanLearn();

// Blocks for as long as the frame takes — tens of milliseconds, up to ~200 for
// a chatty air conditioner. That is a real gap in the microphone uplink, and
// it is why the caller drops the frames either side rather than sending them:
// half a word arriving at the wake model is worse than no word.
bool irSend(const IrCode& code);

// Build and send an AC frame from a described state rather than a captured
// blob, so a temperature nobody ever captured is still sendable. Returns false
// when the protocol name is not one the library can encode.
bool irSendAc(const IrAcState& state);

// Start listening for one frame. Powers up the receiver; irLearnPoll must be
// called until it stops returning IR_LEARN_WAITING or the receiver stays on.
void irLearnBegin(uint32_t timeoutMs);

enum IrLearnResult {
  IR_LEARN_IDLE,       // not learning
  IR_LEARN_WAITING,    // receiver on, nothing yet
  IR_LEARN_GOT,        // a frame arrived; `out` is filled
  IR_LEARN_TIMEOUT,    // nobody pressed anything
};

// Call from loop(). Cheap when idle.
IrLearnResult irLearnPoll(IrCode& out);

// Give up early — a second learn request, or a cancel.
void irLearnCancel();
