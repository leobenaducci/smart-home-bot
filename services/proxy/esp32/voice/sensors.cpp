#include "sensors.h"

#include "board_config.h"

#include <Wire.h>

// Registers poked directly rather than through three Adafruit libraries.
//
// Not purity: each of these is one command and one read, the maths is four
// lines from the datasheet, and the alternative is three more libraries to
// install at the right versions before this sketch will build — on a project
// whose README already has to explain that the board settings are not
// optional. The cost is that the CRC bytes both climate parts send are
// ignored; a corrupt I2C read shows up as an absurd temperature rather than as
// a retry, and the gateway is a better place to notice that than a wall.

static const uint8_t ADDR_SHT3X = 0x44;
static const uint8_t ADDR_AHT20 = 0x38;
static const uint8_t ADDR_BH1750 = 0x23;

enum ClimatePart { CLIMATE_NONE, CLIMATE_SHT3X, CLIMATE_AHT20 };
static ClimatePart g_climate = CLIMATE_NONE;
static bool g_haveLux = false;

static uint32_t g_nextRead = 0;
static uint32_t g_nextPublish = 0;
static uint32_t g_lastMotionMs = 0;
static bool g_motion = false;
static SensorReading g_last;

static bool i2cPresent(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

static bool i2cWrite(uint8_t addr, const uint8_t* bytes, size_t n) {
  Wire.beginTransmission(addr);
  Wire.write(bytes, n);
  return Wire.endTransmission() == 0;
}

// ---------------------------------------------------------------------------

static bool readSht3x(float& c, float& rh) {
  const uint8_t cmd[2] = {0x2C, 0x06};      // single shot, high repeatability
  if (!i2cWrite(ADDR_SHT3X, cmd, 2)) return false;
  delay(20);
  if (Wire.requestFrom(ADDR_SHT3X, (uint8_t)6) != 6) return false;
  uint8_t b[6];
  for (uint8_t i = 0; i < 6; i++) b[i] = Wire.read();
  const uint16_t rawT = (b[0] << 8) | b[1];
  const uint16_t rawH = (b[3] << 8) | b[4];
  c = -45.0f + 175.0f * (rawT / 65535.0f);
  rh = 100.0f * (rawH / 65535.0f);
  return true;
}

static bool readAht20(float& c, float& rh) {
  const uint8_t cmd[3] = {0xAC, 0x33, 0x00};   // trigger measurement
  if (!i2cWrite(ADDR_AHT20, cmd, 3)) return false;
  delay(85);
  if (Wire.requestFrom(ADDR_AHT20, (uint8_t)7) != 7) return false;
  uint8_t b[7];
  for (uint8_t i = 0; i < 7; i++) b[i] = Wire.read();
  if (b[0] & 0x80) return false;               // still busy
  const uint32_t rawH = ((uint32_t)b[1] << 12) | ((uint32_t)b[2] << 4) | (b[3] >> 4);
  const uint32_t rawT = (((uint32_t)b[3] & 0x0F) << 16) | ((uint32_t)b[4] << 8) | b[5];
  rh = (rawH * 100.0f) / 1048576.0f;
  c = (rawT * 200.0f) / 1048576.0f - 50.0f;
  return true;
}

static bool readLux(float& lux) {
  if (Wire.requestFrom(ADDR_BH1750, (uint8_t)2) != 2) return false;
  const uint16_t raw = (Wire.read() << 8) | Wire.read();
  lux = raw / 1.2f;                            // datasheet's fixed constant
  return true;
}

// ---------------------------------------------------------------------------

void sensorsBegin() {
#if SENSOR_CLIMATE || SENSOR_LUX
  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(100000);                       // long dupont wire on a wall
#endif

#if SENSOR_CLIMATE
  if (i2cPresent(ADDR_SHT3X)) g_climate = CLIMATE_SHT3X;
  else if (i2cPresent(ADDR_AHT20)) {
    const uint8_t init[3] = {0xBE, 0x08, 0x00};
    i2cWrite(ADDR_AHT20, init, 3);
    delay(10);
    g_climate = CLIMATE_AHT20;
  }
  Serial.printf("[sensors] climate: %s\n",
                g_climate == CLIMATE_SHT3X ? "SHT3x" :
                g_climate == CLIMATE_AHT20 ? "AHT20" : "none");
#endif

#if SENSOR_LUX
  if (i2cPresent(ADDR_BH1750)) {
    const uint8_t mode = 0x10;                 // continuous, 1 lx resolution
    g_haveLux = i2cWrite(ADDR_BH1750, &mode, 1);
    delay(180);
  }
  Serial.printf("[sensors] light: %s\n", g_haveLux ? "BH1750" : "none");
#endif

#if PIR_PIN >= 0
  // No pull: a PIR module drives the line both ways. INPUT_PULLDOWN here would
  // fight the module's idle-low output and, on some AM312 clones, brown out the
  // reading entirely.
  pinMode(PIR_PIN, INPUT);
  Serial.printf("[sensors] presence: PIR on GPIO %d\n", PIR_PIN);
  // A PIR needs up to a minute after power-on to settle, during which it lies.
  // Motion is simply not believed until then; the alternative is Alfred
  // announcing somebody walked in every time the power blinks.
  g_lastMotionMs = millis();
#endif
}

bool sensorsPresent() {
  if (g_climate != CLIMATE_NONE || g_haveLux) return true;
#if PIR_PIN >= 0
  return true;
#else
  return false;
#endif
}

SensorReading sensorsRead() {
  SensorReading r;

  if (g_climate == CLIMATE_SHT3X) r.haveClimate = readSht3x(r.celsius, r.humidity);
  else if (g_climate == CLIMATE_AHT20) r.haveClimate = readAht20(r.celsius, r.humidity);

  if (g_haveLux) r.haveLux = readLux(r.lux);

#if PIR_PIN >= 0
  r.havePir = true;
  r.motion = g_motion;
  r.secondsSinceMotion = (millis() - g_lastMotionMs) / 1000;
#endif

  return r;
}

bool sensorsTick(SensorReading& out, bool& outIsEvent) {
  outIsEvent = false;
  if (!sensorsPresent()) return false;

  const uint32_t now = millis();

#if PIR_PIN >= 0
  // Read every pass, not on the sensor schedule: a PIR pulse is about two
  // seconds long and a five-second poll would miss half of them.
  const bool motion = digitalRead(PIR_PIN) == HIGH;
  if (motion) g_lastMotionMs = now;
  if (motion != g_motion) {
    g_motion = motion;
    // Somebody walking into a room is worth saying immediately; the numbers can
    // wait for the next read.
    out = sensorsRead();
    outIsEvent = true;
    g_last = out;
    return true;
  }
#endif

  if ((int32_t)(now - g_nextRead) < 0) return false;
  g_nextRead = now + SENSOR_READ_MS;

  SensorReading r = sensorsRead();

  bool worthIt = (int32_t)(now - g_nextPublish) >= 0;
  if (!worthIt && r.haveClimate && g_last.haveClimate) {
    worthIt = fabsf(r.celsius - g_last.celsius) >= SENSOR_TEMP_DELTA ||
              fabsf(r.humidity - g_last.humidity) >= SENSOR_HUM_DELTA;
  }
  if (!worthIt && r.haveLux && g_last.haveLux) {
    // A ratio, not a difference: the step from a dark room to a lit one is 300
    // lux and the step from an overcast noon to a sunny one is 20,000, and only
    // one of those is somebody turning on a light.
    // fminf/fmaxf rather than Arduino's min/max, which are macros on some
    // cores and evaluate their arguments twice.
    const float lo = fminf(r.lux, g_last.lux) + 1.0f;
    const float hi = fmaxf(r.lux, g_last.lux) + 1.0f;
    worthIt = (hi / lo) >= SENSOR_LUX_FACTOR;
  }
  if (!worthIt) return false;

  g_nextPublish = now + SENSOR_PUBLISH_MS;
  g_last = r;
  out = r;
  return true;
}
