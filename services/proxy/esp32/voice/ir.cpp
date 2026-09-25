#include "ir.h"

#include "board_config.h"

#include <IRac.h>
#include <IRrecv.h>
#include <IRremoteESP8266.h>
#include <IRsend.h>
#include <IRutils.h>

// IRremoteESP8266 rather than Arduino-IRremote, which is what IR_Test_2 in
// IoT_projects reached for first. The deciding feature is air conditioners:
// Arduino-IRremote handles button-shaped remotes and treats an AC frame as an
// unknown blob, so "23 degrees" would mean capturing 23 degrees, and 24, and
// 25. This library encodes the state of ~60 AC protocols, so a temperature is
// a number in a struct. The name says ESP8266; ESP32 is a first-class target.

#if IR_TX_PIN >= 0
static IRsend g_send(IR_TX_PIN);
static IRac g_ac(IR_TX_PIN);
#endif

#if IR_RX_PIN >= 0
static IRrecv g_recv(IR_RX_PIN, IR_CAPTURE_BUFFER, IR_CAPTURE_TIMEOUT_MS, true);
static decode_results g_results;
static bool g_learning = false;
static uint32_t g_learnUntil = 0;
#endif

bool irCanSend() {
#if IR_TX_PIN >= 0
  return true;
#else
  return false;
#endif
}

bool irCanLearn() {
#if IR_RX_PIN >= 0
  return true;
#else
  return false;
#endif
}

void irBegin() {
#if IR_TX_PIN >= 0
  g_send.begin();
  g_ac.next.protocol = decode_type_t::UNKNOWN;
  Serial.printf("[ir] blaster on GPIO %d\n", IR_TX_PIN);
#endif
#if IR_RX_PIN >= 0
  // Deliberately not enableIRIn() here. The receiver only runs during a learn:
  // see the note in board_config.h.
  Serial.printf("[ir] receiver on GPIO %d (idle)\n", IR_RX_PIN);
#endif
}

// ---------------------------------------------------------------------------
// Sending
// ---------------------------------------------------------------------------

bool irSend(const IrCode& code) {
#if IR_TX_PIN < 0
  (void)code;
  return false;
#else
  if (!code.valid()) return false;

  if (code.isRaw()) {
    // sendRaw wants a mutable buffer.
    std::vector<uint16_t> buf(code.raw);
    g_send.sendRaw(buf.data(), buf.size(), code.khz);
    Serial.printf("[ir] sent %u raw marks at %u kHz\n", (unsigned)buf.size(), code.khz);
    return true;
  }

  const decode_type_t type = strToDecodeType(code.protocol.c_str());
  if (type == decode_type_t::UNKNOWN) {
    Serial.printf("[ir] unknown protocol '%s'\n", code.protocol.c_str());
    return false;
  }

  if (code.isState()) {
    // Long-form: an air conditioner's whole state as captured, replayed
    // byte-for-byte. Distinct from irSendAc, which *builds* a state.
    uint8_t bytes[kStateSizeMax] = {0};
    const size_t n = code.state.length() / 2;
    if (n == 0 || n > sizeof(bytes)) {
      Serial.printf("[ir] state of %u bytes is not sendable\n", (unsigned)n);
      return false;
    }
    for (size_t i = 0; i < n; i++) {
      bytes[i] = (uint8_t)strtoul(code.state.substring(i * 2, i * 2 + 2).c_str(), nullptr, 16);
    }
    const bool ok = g_send.send(type, bytes, n);
    Serial.printf("[ir] sent %s state (%u bytes): %s\n",
                  code.protocol.c_str(), (unsigned)n, ok ? "ok" : "unsupported");
    return ok;
  }

  const bool ok = g_send.send(type, code.value, code.bits, code.repeats);
  Serial.printf("[ir] sent %s 0x%llX/%u: %s\n",
                code.protocol.c_str(), (unsigned long long)code.value, code.bits,
                ok ? "ok" : "unsupported");
  return ok;
#endif
}

bool irSendAc(const IrAcState& state) {
#if IR_TX_PIN < 0
  (void)state;
  return false;
#else
  const decode_type_t type = strToDecodeType(state.protocol.c_str());
  if (type == decode_type_t::UNKNOWN || !IRac::isProtocolSupported(type)) {
    Serial.printf("[ir] no AC encoder for '%s'\n", state.protocol.c_str());
    return false;
  }

  g_ac.next.protocol = type;
  g_ac.next.model = -1;                 // the library's default variant
  g_ac.next.power = state.power;
  g_ac.next.mode = IRac::strToOpmode(state.mode.c_str(), stdAc::opmode_t::kCool);
  g_ac.next.celsius = true;             // this house is not in Fahrenheit
  g_ac.next.degrees = state.degrees;
  g_ac.next.fanspeed = IRac::strToFanspeed(state.fan.c_str(), stdAc::fanspeed_t::kAuto);
  g_ac.next.swingv = state.swingv ? stdAc::swingv_t::kAuto : stdAc::swingv_t::kOff;
  g_ac.next.swingh = stdAc::swingh_t::kOff;
  g_ac.next.quiet = state.quiet;
  g_ac.next.turbo = false;
  g_ac.next.econo = false;
  g_ac.next.light = true;
  g_ac.next.filter = false;
  g_ac.next.clean = false;
  g_ac.next.beep = false;
  g_ac.next.sleep = -1;                 // off; a positive value is minutes
  g_ac.next.clock = -1;

  const bool ok = g_ac.sendAc();
  Serial.printf("[ir] AC %s: %s %.0fC fan %s -> %s\n",
                state.protocol.c_str(), state.power ? state.mode.c_str() : "off",
                state.degrees, state.fan.c_str(), ok ? "ok" : "failed");
  return ok;
#endif
}

// ---------------------------------------------------------------------------
// Learning
// ---------------------------------------------------------------------------

void irLearnBegin(uint32_t timeoutMs) {
#if IR_RX_PIN < 0
  (void)timeoutMs;
#else
  g_recv.enableIRIn();
  g_learning = true;
  g_learnUntil = millis() + (timeoutMs ? timeoutMs : IR_LEARN_TIMEOUT_MS);
  Serial.printf("[ir] learning for %u ms — point the remote and press once\n",
                (unsigned)(timeoutMs ? timeoutMs : IR_LEARN_TIMEOUT_MS));
#endif
}

void irLearnCancel() {
#if IR_RX_PIN >= 0
  if (!g_learning) return;
  g_recv.disableIRIn();
  g_learning = false;
  Serial.println("[ir] learning cancelled");
#endif
}

IrLearnResult irLearnPoll(IrCode& out) {
#if IR_RX_PIN < 0
  (void)out;
  return IR_LEARN_IDLE;
#else
  if (!g_learning) return IR_LEARN_IDLE;

  if ((int32_t)(millis() - g_learnUntil) >= 0) {
    irLearnCancel();
    return IR_LEARN_TIMEOUT;
  }

  if (!g_recv.decode(&g_results)) return IR_LEARN_WAITING;

  out = IrCode();
  const decode_type_t type = g_results.decode_type;

  if (type == decode_type_t::UNKNOWN) {
    // Nothing recognised it, but the timings are still a perfectly good code —
    // this is the path that makes an off-brand remote work at all. The first
    // entry resultToRawArray produces is the gap *before* the frame, which is
    // however long the receiver happened to be idle and is not part of the
    // signal; replaying it would just be a pause.
    const uint16_t len = getCorrectedRawLength(&g_results);
    uint16_t* raw = resultToRawArray(&g_results);
    if (raw != nullptr) {
      for (uint16_t i = 1; i < len; i++) out.raw.push_back(raw[i]);
      delete[] raw;
    }
    out.khz = 38;
    Serial.printf("[ir] learned an unrecognised frame, %u marks\n", (unsigned)out.raw.size());
  } else {
    out.protocol = typeToString(type, g_results.repeat);
    out.bits = g_results.bits;
    if (hasACState(type)) {
      out.state = resultToHexidecimal(&g_results);
      Serial.printf("[ir] learned %s state %s\n", out.protocol.c_str(), out.state.c_str());
    } else {
      out.value = g_results.value;
      Serial.printf("[ir] learned %s 0x%llX/%u\n", out.protocol.c_str(),
                    (unsigned long long)out.value, out.bits);
    }
  }

  // One press, one code. Staying on would capture the auto-repeat frame most
  // remotes send while a finger is still down, and overwrite the real one.
  irLearnCancel();
  return IR_LEARN_GOT;
#endif
}
