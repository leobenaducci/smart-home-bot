# ESP32 Camera Firmware - AI Agents Documentation & Change Log

This file tracks all changes made by AI agents specifically to the ESP32 camera firmware project.

### [2026-08-10] A camera that could not tell anyone it had stopped seeing

cam2 sat frozen for four hours. `/status` answered instantly and read sensor
registers back correctly, so SCCB was fine and the chip was alive and
addressable — but no frame ever arrived. `/capture` returned 500 after ~4 s,
and `/stream` accepted the TCP connection and then sent **nothing at all**, for
ever. A power cycle did not clear it.

Three separate things were wrong, none of them the sensor:

- **`/stream` hung instead of failing.** `stream_handler` sets the content type
  up front, then on a failed grab sets `res = ESP_FAIL` and falls into a run of
  `res == ESP_OK` guards that are now all false — so it sent no body, no
  status, nothing, and returned. `curl` sat with no headers until it timed out,
  and `web_server`'s reconnect loop had no failure to react to. That is why a
  dead sensor presented as a frozen picture rather than an error. It answers
  500 now, if and only if nothing has reached the wire yet.
- **Nothing counted the failures.** Every `esp_camera_fb_get()` in
  `app_httpd.cpp` now goes through `camera_fb_get_watched()`. Ten consecutive
  failures rebuild the driver (`esp_camera_deinit` / `esp_camera_init` with the
  original config, then re-apply the sensor settings); forty and it reboots.
  Consecutive, never cumulative — a dropped frame under load is normal and must
  not accumulate into a reboot.
- **There was no way to restart it remotely.** The only `ESP.restart()` is in
  the WiFi-config `/save` handler, unreachable once the camera is on the
  network, so recovery meant walking to the camera and pulling its power.

The camera configuration was a local in `setup()`, which is fine until the one
thing you need is `esp_camera_init()` with exactly those settings again; it is
file-scope now, and the sensor tweaks moved into `applySensorDefaults()` so
recovery re-applies them rather than coming back silently upside down.
Recovery takes a non-blocking FreeRTOS mutex: two handlers can be failing at
the same moment and only one may deinit the driver.

`SKIP_CAMERA_REGISTRY` was added alongside it. Set to 1, the camera does not
register and sends no heartbeats — the web server on :80 and the stream on :81
come up exactly as they always do, and the camera is used by pointing a browser
at its address. Registration only decides whether the camera appears in
HomeCameras; it has nothing to do with whether the camera works, and a board
that cannot reach the registry otherwise spends every thirty seconds on a
request that will not succeed, each failed heartbeat clearing `cameraRegistered`
so the next cycle retries the heavier registration instead. Taking the registry
out of the picture is worth having when the question is whether the *camera*
is working. It prints where to point a browser on boot, because a camera that
never appears in HomeCameras looks broken from the other end.

**Not compile-verified** — there is no Arduino toolchain on the build boxes.
Reviewed by hand only, and it needs a flash. Both settings of
`SKIP_CAMERA_REGISTRY` were checked by resolving the conditionals and
confirming that no registry symbol survives when it is on, that the camera path
survives in both, and that the directive nesting balances either way.

Worth saying plainly: this makes the camera able to notice and recover from a
wedged driver, and it would not have saved cam2. Register reads working while
the pixel path delivers nothing, surviving a power cycle, points at the ribbon
cable or the sensor module itself — which no firmware can fix.

## Recent Changes

### [Date: 2026-04-08] - ESP32 Camera Web Server Setup
**Agent**: Cline (Anthropic)
**Task**: Set up ESP32 camera web server firmware for MJPEG streaming

**Changes Made**:
1. Created `esp32/CameraWebServer/CameraWebServer.ino`:
   - ESP32 camera initialization and setup
   - WiFi connection to access point
   - Camera sensor configuration (OV2640 or OV3660)
   - MJPEG streaming server implementation using ESP-camera library
   - Frame size and quality optimization for streaming
   - PSRAM handling for larger frame buffers
   - Sensor corrections (vflip, brightness, saturation)

2. Created `esp32/CameraWebServer/board_config.h`:
   - Camera model detection macros
   - GPIO pin definitions for different ESP32 boards
   - Hardware-specific configuration

3. Created `esp32/CameraWebServer/camera_pins.h`:
   - Pin definitions for camera interface (XCLK, PCLK, VSYNC, HREF)
   - Sensor control pins (PWDN, RESET)
   - GPIO mappings for I2C SDA/SCL

4. Created `esp32/CameraWebServer/partitions.csv`:
   - SPIFFS partition layout for web server assets

5. Created `esp32/CameraWebServer/ci.yml`:
   - Arduino CI/CD workflow for automated builds

6. Created `esp32/CameraWebServer/camera_index.h`:
   - Web page header/footer templates

**Key Features**:
- MJPEG streaming at configurable resolutions
- Automatic PSRAM detection and utilization
- Multiple camera model support (ESP32-CAM, ESP-EYE, M5Stack, etc.)
- Sensor-specific calibration settings
- Low memory footprint for resource-constrained devices

**Compilation Instructions**:
```bash
cd esp32/CameraWebServer
arduino-cli compile --fqbn esp32:esp32:esp32cam:PSRAM=enabled
```

**API Reference**:
- CameraWebServer.ino - Main entry point and setup
- board_config.h - Board-specific configurations
- camera_pins.h - GPIO pin definitions
- camera_index.h - Web page templates

## ESP32 Project Structure

```
esp32/
└── CameraWebServer/
    ├── CameraWebServer.ino      # Main Arduino sketch
    ├── board_config.h           # Board configuration macros
    ├── camera_pins.h            # GPIO pin definitions
    ├── camera_index.h           # Web page templates
    ├── partitions.csv           # SPIFFS partition layout
    ├── ci.yml                   # CI/CD workflow
    └── AGENTS.md                # This file
```

## Technical Details

**Supported Camera Models**:
- ESP32-CAM (OV2640)
- ESP-EYE (OV2640)
- M5Stack StickCAM
- M5Stack StickCAMP3
- ESP32S3-EYE

**Stream Specifications**:
- Default resolution: QVGA (320x240) at 30fps
- Quality: JPEG quality 10-12 (lower = higher quality)
- Frame buffer: 2 buffers for smooth streaming

**Memory Optimization**:
- Uses PSRAM when available for larger frame buffers
- Falls back to DRAM when PSRAM not detected
- Optimizes frame size when PSRAM unavailable

## Contact & Support

For questions about ESP32 firmware contributions or to report issues, please refer to the project maintainers.