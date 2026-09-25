// Display abstraction.
//
// LVGL stays the UI layer, and swapping the physical panel means adding one
// more DisplayDriver implementation and changing which one board_display()
// returns. Nothing in control.ino should reference LovyanGFX, the GT911, or any
// GPIO.
//
// This was written anticipating an e-ink panel as the second implementation.
// That is no longer where e-ink is going: the weekly-menu dashboard runs on an
// ESP32-C3 with no touch, no LVGL and server-rendered bitmaps, as its own
// sketch under ../menu/. The colour-format and render-mode hooks below stay
// because they are the right shape for any second panel, but do not treat them
// as a half-finished e-ink port waiting to be completed.
#pragma once

#include <lvgl.h>
#include <stdint.h>

class DisplayDriver {
 public:
  virtual ~DisplayDriver() = default;

  // Bring up the panel. Returns false if the panel did not initialise.
  virtual bool begin() = 0;

  virtual uint16_t width() const = 0;
  virtual uint16_t height() const = 0;

  // Push a rendered LVGL area to the panel. Called from LVGL's flush callback;
  // the caller signals lv_display_flush_ready(), not the driver.
  virtual void flush(const lv_area_t* area, uint8_t* pixels) = 0;

  // Pixel format LVGL should render in for this panel.
  virtual lv_color_format_t colorFormat() const = 0;

  // PARTIAL for framebuffer-backed colour panels, FULL for e-ink.
  virtual lv_display_render_mode_t renderMode() const = 0;

  // Current touch point in screen coordinates. Returns false when not touched.
  // Panels without a digitiser inherit this.
  virtual bool readTouch(int16_t& x, int16_t& y) {
    (void)x;
    (void)y;
    return false;
  }

  // Backlight level, 0-100. Panels without a backlight inherit this.
  virtual void setBacklight(uint8_t percent) { (void)percent; }

  // Human-readable panel name, for the boot screen and serial log.
  virtual const char* name() const = 0;
};

// The driver for the panel this firmware is built for. Selected at compile
// time; this is the seam to change when the e-ink panel arrives.
DisplayDriver& board_display();
