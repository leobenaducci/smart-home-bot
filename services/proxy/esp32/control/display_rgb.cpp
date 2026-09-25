// DisplayDriver implementation for the CrowPanel ESP32 HMI 7.0" RGB panel.
//
// Wraps LovyanGFX (panel + backlight) and TAMC_GT911 (touch). All GPIO numbers
// come from board_config.h -- there should be no bare pin numbers in this file.

#include <Arduino.h>

#define LGFX_USE_V1
#include <LovyanGFX.hpp>
#include <TAMC_GT911.h>
#include <Wire.h>

#include "board_config.h"
#include "display.h"

#if BOARD_HAS_PCA9557
#include <PCA9557.h>
#endif

// ---------------------------------------------------------------------------
// LovyanGFX panel definition
// ---------------------------------------------------------------------------
class LGFX_CrowPanel7 : public lgfx::LGFX_Device {
  lgfx::Panel_RGB _panel;
  lgfx::Bus_RGB _bus;
  lgfx::Light_PWM _light;

 public:
  LGFX_CrowPanel7() {
    {
      auto cfg = _panel.config();
      cfg.memory_width = PANEL_WIDTH;
      cfg.memory_height = PANEL_HEIGHT;
      cfg.panel_width = PANEL_WIDTH;
      cfg.panel_height = PANEL_HEIGHT;
      cfg.offset_x = 0;
      cfg.offset_y = 0;
      _panel.config(cfg);
    }
    {
      // The 800x480 framebuffer is 750 KB, so it has to live in PSRAM. This is
      // why the board must be built with OPI PSRAM enabled.
      auto cfg = _panel.config_detail();
      cfg.use_psram = 1;
      _panel.config_detail(cfg);
    }
    {
      auto cfg = _bus.config();
      cfg.panel = &_panel;

      cfg.pin_d0 = LCD_B0;
      cfg.pin_d1 = LCD_B1;
      cfg.pin_d2 = LCD_B2;
      cfg.pin_d3 = LCD_B3;
      cfg.pin_d4 = LCD_B4;
      cfg.pin_d5 = LCD_G0;
      cfg.pin_d6 = LCD_G1;
      cfg.pin_d7 = LCD_G2;
      cfg.pin_d8 = LCD_G3;
      cfg.pin_d9 = LCD_G4;
      cfg.pin_d10 = LCD_G5;
      cfg.pin_d11 = LCD_R0;
      cfg.pin_d12 = LCD_R1;
      cfg.pin_d13 = LCD_R2;
      cfg.pin_d14 = LCD_R3;
      cfg.pin_d15 = LCD_R4;

      cfg.pin_henable = LCD_DE;
      cfg.pin_vsync = LCD_VSYNC;
      cfg.pin_hsync = LCD_HSYNC;
      cfg.pin_pclk = LCD_PCLK;
      cfg.freq_write = LCD_PCLK_HZ;

      cfg.hsync_polarity = LCD_HSYNC_POLARITY;
      cfg.hsync_front_porch = LCD_HSYNC_FRONT_PORCH;
      cfg.hsync_pulse_width = LCD_HSYNC_PULSE_WIDTH;
      cfg.hsync_back_porch = LCD_HSYNC_BACK_PORCH;
      cfg.vsync_polarity = LCD_VSYNC_POLARITY;
      cfg.vsync_front_porch = LCD_VSYNC_FRONT_PORCH;
      cfg.vsync_pulse_width = LCD_VSYNC_PULSE_WIDTH;
      cfg.vsync_back_porch = LCD_VSYNC_BACK_PORCH;
      cfg.pclk_active_neg = LCD_PCLK_ACTIVE_NEG;
      cfg.de_idle_high = 0;
      cfg.pclk_idle_high = 0;

      _bus.config(cfg);
      _panel.setBus(&_bus);
    }
    {
      auto cfg = _light.config();
      cfg.pin_bl = LCD_BACKLIGHT_PIN;
      _light.config(cfg);
      _panel.light(&_light);
    }
    setPanel(&_panel);
  }
};

// ---------------------------------------------------------------------------
// Driver
// ---------------------------------------------------------------------------
namespace {

class RgbPanelDriver : public DisplayDriver {
 public:
  bool begin() override {
    if (!_lcd.init()) return false;
    _lcd.setColorDepth(16);
    _lcd.fillScreen(TFT_BLACK);
    setBacklight(100);
    beginTouch();
    return true;
  }

  uint16_t width() const override { return PANEL_WIDTH; }
  uint16_t height() const override { return PANEL_HEIGHT; }

  lv_color_format_t colorFormat() const override { return LV_COLOR_FORMAT_RGB565; }

  lv_display_render_mode_t renderMode() const override {
    return LV_DISPLAY_RENDER_MODE_PARTIAL;
  }

  void flush(const lv_area_t* area, uint8_t* pixels) override {
    const int32_t w = area->x2 - area->x1 + 1;
    const int32_t h = area->y2 - area->y1 + 1;
    _lcd.pushImageDMA(area->x1, area->y1, w, h,
                      reinterpret_cast<lgfx::rgb565_t*>(pixels));
  }

  bool readTouch(int16_t& x, int16_t& y) override {
    if (!_touchReady) return false;
    _touch.read();
    if (!_touch.isTouched || _touch.touches == 0) return false;

    x = map(_touch.points[0].x, TOUCH_MAP_X1, TOUCH_MAP_X2, 0, PANEL_WIDTH - 1);
    y = map(_touch.points[0].y, TOUCH_MAP_Y1, TOUCH_MAP_Y2, 0, PANEL_HEIGHT - 1);
    x = constrain(x, 0, PANEL_WIDTH - 1);
    y = constrain(y, 0, PANEL_HEIGHT - 1);
    return true;
  }

  void setBacklight(uint8_t percent) override {
    _lcd.setBrightness(map(constrain(percent, 0, 100), 0, 100, 0, 255));
  }

  const char* name() const override { return BOARD_NAME; }

  bool touchReady() const { return _touchReady; }

 private:
  void beginTouch() {
    Wire.begin(TOUCH_SDA, TOUCH_SCL);

#if BOARD_HAS_PCA9557
    // V3.0 boards gate the GT911 reset line behind the PCA9557. The GT911
    // samples its I2C address from the INT pin state as reset is released, so
    // this sequence has to run, with these delays, before touch.begin().
    PCA9557 expander;
    expander.reset();
    expander.setMode(IO_OUTPUT);
    expander.setState(IO0, IO_LOW);
    expander.setState(IO1, IO_LOW);
    delay(20);
    expander.setState(IO0, IO_HIGH);
    delay(100);
    expander.setMode(IO1, IO_INPUT);
#endif

    _touch.begin();
    _touch.setRotation(ROTATION_INVERTED);
    _touchReady = true;
  }

  LGFX_CrowPanel7 _lcd;
  TAMC_GT911 _touch{TOUCH_SDA, TOUCH_SCL, TOUCH_INT, TOUCH_RST, PANEL_WIDTH,
                    PANEL_HEIGHT};
  bool _touchReady = false;
};

RgbPanelDriver g_driver;

}  // namespace

DisplayDriver& board_display() { return g_driver; }
