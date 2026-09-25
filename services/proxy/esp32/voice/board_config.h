// Pins and constants for the voice puck.
//
// Everything hardware-specific lives here so the rest of the sketch never names
// a GPIO. The exact mic and amp modules do not matter as long as they speak I2S;
// these defaults assume an INMP441-class MEMS mic and a MAX98357A-class class-D
// amp, which is the cheapest combination that works.
#pragma once

// ---------------------------------------------------------------------------
// I2S microphone (INMP441 / ICS-43434 / SPH0645)
// ---------------------------------------------------------------------------
// Standard I2S, not PDM. Wire the mic's L/R select to GND so it sits on the
// LEFT slot; ESP_SR is configured for a single mono channel below and will hear
// silence if the mic answers on the other slot.
#define MIC_BCLK 4
#define MIC_WS   5   // aka LRCL / WS
#define MIC_DIN  6   // the mic's DOUT, the ESP32's input

// ---------------------------------------------------------------------------
// I2S amplifier (MAX98357A) -> 4 ohm speaker
// ---------------------------------------------------------------------------
// A second I2S peripheral, not the mic's. The S3 has two, and the reply plays
// at the voice's own 22050 Hz while the mic runs at 16000 — one bus cannot be
// both without being torn down and rebuilt between every phase of a turn.
#define SPK_BCLK 15
#define SPK_WS   16  // aka LRC
#define SPK_DOUT 17  // aka DIN on the amp board

// The MAX98357A's SD pin: HIGH enables the amplifier. Pulled low between turns
// so the idle hiss of a class-D amp is not a permanent feature of the room.
// Set to -1 if the pin is strapped high in hardware.
#define SPK_ENABLE 18

// ---------------------------------------------------------------------------
// LED ring (WS2812 / NeoPixel)
// ---------------------------------------------------------------------------
#define LED_PIN    8
#define LED_COUNT  12
// Full brightness on a ring this size is unpleasant on a night table and pulls
// real current. 40/255 is bright enough to read across a room.
#define LED_BRIGHTNESS 40

// ---------------------------------------------------------------------------
// Infrared
// ---------------------------------------------------------------------------
// The puck is already the thing in the room you talk to, mains-powered and
// line-of-sight with the television — which is most of what an IR blaster
// needs to be. Both pins are optional: set either to -1 and that half compiles
// out, so a puck built without the parts still boots and still talks.
//
// TX is a LED, not a signal. An IR LED wants 100 mA or more to carry across a
// room and a GPIO can source 40, so this pin drives the base of a small NPN
// (2N2222 / BC337) through ~1k, with the LED and a ~100R resistor on the
// collector off the 5 V rail. Wired straight to the GPIO it will appear to
// work at 30 cm and fail at the sofa.
#define IR_TX_PIN 9

// RX is a demodulator (TSOP38238, VS1838B), not a bare photodiode: it strips
// the 38 kHz carrier and hands over clean logic levels. 3.3 V, and it wants
// 100R in series with VCC and 10uF to ground beside it — this house's LED ring
// puts real noise on that rail, and a hungry receiver reads it as IR.
//
// Only enabled while learning a code. Left running it would spend an interrupt
// on every remote in the room and every fluorescent tube, on a device whose
// actual job is a continuous 256 kbps audio uplink.
#define IR_RX_PIN 10

// Capture buffer, in state transitions. An air conditioner's remote sends its
// entire state in one frame — mode, temperature, fan, timer — which runs to
// several hundred marks and spaces; a television's power button is under 70.
// Sized for the worst case because that is the one worth capturing.
#define IR_CAPTURE_BUFFER 1024
// A frame is over when the air has been quiet this long. Below ~15 ms this
// splits one AC frame into two captures.
#define IR_CAPTURE_TIMEOUT_MS 50
// Nothing pointed at it, or a remote with a dead battery: give up rather than
// leave the receiver running and the ring stuck on cyan.
#define IR_LEARN_TIMEOUT_MS 30000

// ---------------------------------------------------------------------------
// Sensors
// ---------------------------------------------------------------------------
// All optional, all independent: set a pin to -1 or a define to 0 and that
// sensor compiles out. A puck with none of them is the puck as originally
// designed and behaves identically.
//
// The reason they belong here rather than on their own little board: this
// device is already mains-powered, already in the middle of the room, and
// already holds an MQTT connection. A separate sensor node in the same room
// would be a second thing to build, flash, name and lose.

// I2C, shared by the climate and light sensors. Two pins for both.
#define I2C_SDA 11
#define I2C_SCL 12

// Temperature and humidity. Set to 0 if not fitted.
//
// SHT31/SHT35 (0x44) or AHT20 (0x38) — both are I2C, both are cheap, and
// unlike a DHT22 neither needs bit-banged timing that a continuous audio
// uplink would trample on. The driver tries the addresses in that order.
#define SENSOR_CLIMATE 1

// Ambient light in lux: BH1750 (0x23). Set to 0 if not fitted.
//
// Preferred over an LDR on the ADC because the ESP32-S3's ADC is noisy while
// WiFi transmits, and this device transmits constantly. A number that swings
// 30% with the radio is not a light level.
#define SENSOR_LUX 1

// Presence: a PIR module (AM312 or HC-SR501), which is a plain digital high
// while it sees movement. -1 if not fitted.
//
// AM312 preferred: it runs at 3.3 V and has a fixed ~2 s pulse. The HC-SR501
// wants 5 V on VCC and its output is 3.3 V logic, so it works, but its two
// trimmers are one more thing set wrong on a wall.
#define PIR_PIN 13

// How often the room's numbers go on the broker even when nothing has changed.
// Retained, so this is not how Alfred finds out — it is how a dashboard's graph
// stays continuous and how a stale reading becomes obvious.
#define SENSOR_PUBLISH_MS   60000
// …and how often they are read. More often than they are published, so a
// change worth hearing about (below) does not wait a minute.
#define SENSOR_READ_MS      5000
// A change big enough to publish immediately rather than at the next interval.
// Below these the numbers are noise and the broker gets a message a second.
#define SENSOR_TEMP_DELTA   0.3f
#define SENSOR_HUM_DELTA    2.0f
#define SENSOR_LUX_FACTOR   1.5f   // ratio, not difference: lux is logarithmic

// ---------------------------------------------------------------------------
// Audio
// ---------------------------------------------------------------------------
#define MIC_SAMPLE_RATE 16000     // what whisper wants; the gateway assumes it

// One frame of microphone, 40 ms. Small enough that the wake word is not
// delayed by buffering, big enough that a websocket frame per 40 ms is not
// meaningful traffic — the whole uplink is 256 kbps.
#define MIC_FRAME_BYTES ((size_t)(MIC_SAMPLE_RATE * 2 * 40 / 1000))

// There is no recording buffer and no VAD here on purpose. The wake word and
// the end-of-speech detection both run on the gateway, which is what makes a
// custom "Alfred" possible: no on-device engine ships one. See wake handling in
// ../../../home-voice/wakeword/README.md.

// ---------------------------------------------------------------------------
// Network
// ---------------------------------------------------------------------------
#define HTTP_TIMEOUT_MS   20000   // fetching a reply clip
#define WIFI_TIMEOUT_MS   15000
#define MQTT_HOST         "mqtt.home"
#define MQTT_PORT         1883
#define MQTT_RETRY_MS     5000
// PubSubClient defaults to 256 bytes and drops anything larger without saying
// so. A learned code from a remote the library does not recognise is several
// hundred timings — comfortably over that, and the failure looks like the
// receiver never caught anything.
#define MQTT_BUFFER_BYTES 8192

// ---------------------------------------------------------------------------
// Config portal
// ---------------------------------------------------------------------------
// Unlike the menu panel this device is mains-powered and always awake, so the
// portal costs nothing but the time it is open. It still times out: an open AP
// with a form on it is not a thing to leave running on a wall.
#define PORTAL_AP_PASSWORD  "alfred1234"
#define PORTAL_TIMEOUT_MS   (10UL * 60UL * 1000UL)
#define DRD_WINDOW_MS       3000
#define PORTAL_AFTER_FAILURES 20
