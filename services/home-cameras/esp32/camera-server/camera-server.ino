#include "esp_camera.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WebServer.h>
// Explicit rather than relying on what Arduino.h happens to drag in: the
// recovery lock below is a FreeRTOS mutex.
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

// ===========================
// Select camera model in board_config.h
// ===========================
#include "board_config.h"

// ===========================
// Standalone mode
// ===========================
// Set to 1 to run this camera on its own: no registration, no heartbeats, no
// dependency on cameras.home being up or even existing. The web server on :80
// and the stream on :81 come up exactly as they always do, so the camera is
// used by pointing a browser straight at its address.
//
// Worth having while working on a camera. Registration only makes the camera
// appear in HomeCameras; it has nothing to do with whether the camera itself
// works, and a board that cannot reach the registry otherwise spends every
// thirty seconds on an HTTP request that will not succeed — with each failed
// heartbeat clearing `cameraRegistered` so the next cycle retries the heavier
// registration instead. Turning it off removes the registry from the picture
// entirely, which is the point when the question is whether the *camera* is
// working.
#define SKIP_CAMERA_REGISTRY 0

// Camera Registry configuration
#if !SKIP_CAMERA_REGISTRY
const char *REGISTRY_BASE_URL = "http://cameras.home:21021";
const long REGISTRATION_INTERVAL = 30000; // Re-register every 30 seconds
unsigned long lastRegistration = 0;
bool cameraRegistered = false;
#endif

Preferences prefs;

// The camera configuration outlives setup() so the driver can be rebuilt with
// exactly the settings it was first given. It used to be a local, which is
// fine right up until the sensor stops producing frames and the only way back
// is esp_camera_init() with the same config — see cameraRecover() below.
static camera_config_t config;

// Serialises recovery. Two request handlers can be failing to grab a frame at
// the same moment; only one of them may deinit the driver.
static SemaphoreHandle_t cameraRecoverLock = NULL;

void startCameraServer();
void setupLedFlash();
void setupWifi();
void applySensorDefaults();
bool cameraRecover();
#if !SKIP_CAMERA_REGISTRY
void registerCamera();
void sendHeartbeat();
#endif

void setupWifi() {
  prefs.begin("wifi", true);
  String savedSSID = prefs.getString("ssid", "");
  String savedPass  = prefs.getString("pass", "");
  prefs.end();

  String apError = "";

  if (savedSSID.length() > 0) {
    Serial.printf("Connecting to saved WiFi: %s(%s)\n", savedSSID.c_str(), savedPass.c_str());
    WiFi.begin(savedSSID.c_str(), savedPass.c_str());
    WiFi.setSleep(false);
    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts < 40) {
      delay(1000);
      Serial.print(".");
      attempts++;
    }
    if (WiFi.status() == WL_CONNECTED) {
      Serial.println("\nWiFi connected");
      return;
    }
    wl_status_t st = WiFi.status();
    const char* reasonStr =
      st == WL_NO_SSID_AVAIL  ? "network not found" :
      st == WL_CONNECT_FAILED ? "wrong password" : "connection failed";
    Serial.printf("\nWiFi failed (%s)\n", reasonStr);
    apError = String("Could not connect to <b>") + savedSSID + "</b>: " + reasonStr;
    prefs.begin("wifi", false);
    prefs.remove("ssid");
    prefs.remove("pass");
    prefs.end();
  }

  // AP fallback — AP_STA mode so WiFi scanning works while AP is active
  WiFi.disconnect(true);
  delay(100);
  WiFi.mode(WIFI_AP_STA);
  delay(100);
  bool apOk = WiFi.softAP("ESP32-Setup", "esp32setup");
  Serial.printf("AP %s — SSID: ESP32-Setup  Password: esp32setup  IP: %s\n",
    apOk ? "started" : "FAILED",
    WiFi.softAPIP().toString().c_str());

  WebServer server(80);

  // Scan endpoint — returns JSON array of visible networks
  server.on("/scan", HTTP_GET, [&]() {
    int n = WiFi.scanNetworks();
    String json = "[";
    for (int i = 0; i < n; i++) {
      String ssid = WiFi.SSID(i);
      ssid.replace("\\", "\\\\");
      ssid.replace("\"", "\\\"");
      if (i > 0) json += ",";
      json += "{\"s\":\"" + ssid + "\",\"r\":" + WiFi.RSSI(i) +
              ",\"e\":" + (WiFi.encryptionType(i) == WIFI_AUTH_OPEN ? "0" : "1") + "}";
    }
    json += "]";
    server.send(200, "application/json", json);
  });

  server.on("/", HTTP_GET, [&]() {
    String err = apError.length()
      ? "<p style='background:#fff0f0;color:#c00;padding:10px;border-radius:4px;margin-bottom:16px'>" + apError + "</p>"
      : "";
    String html =
      "<!DOCTYPE html><html><head>"
      "<meta name='viewport' content='width=device-width,initial-scale=1'>"
      "<style>"
      "body{font-family:sans-serif;max-width:420px;margin:32px auto;padding:0 16px}"
      "h2{margin-bottom:4px}p.sub{color:#666;font-size:13px;margin:0 0 16px}"
      "label{font-weight:600;display:block;margin-bottom:4px}"
      ".field{margin-bottom:14px}"
      "input[type=text],input[type=password]{width:100%;padding:9px 8px;box-sizing:border-box;"
        "border:1px solid #ccc;border-radius:4px;font-size:15px}"
      ".pw-wrap{position:relative}"
      ".pw-wrap input{padding-right:64px}"
      ".show-btn{position:absolute;right:8px;top:50%;transform:translateY(-50%);"
        "background:none;border:none;color:#0077cc;cursor:pointer;font-size:13px;padding:0}"
      ".nets{border:1px solid #ccc;border-radius:4px;max-height:160px;overflow-y:auto;margin-bottom:14px}"
      ".net{padding:9px 12px;cursor:pointer;display:flex;justify-content:space-between;align-items:center}"
      ".net:hover{background:#f0f8ff}"
      ".net+.net{border-top:1px solid #eee}"
      "button[type=submit]{width:100%;padding:11px;background:#0077cc;color:#fff;"
        "border:none;border-radius:4px;font-size:16px;cursor:pointer}"
      "button[type=submit]:hover{background:#005fa3}"
      "</style></head><body>"
      "<h2>ESP32 Camera Setup</h2>"
      "<p class='sub'>Connected to <b>ESP32-Setup</b> &bull; password: <b>esp32setup</b></p>" +
      err +
      "<div class='nets' id='nets'><div style='padding:10px;color:#888'>Scanning for networks...</div></div>"
      "<form method='POST' action='/save'>"
      "<div class='field'><label>SSID</label>"
      "<input type='text' id='ssid' name='ssid' required placeholder='Select above or type manually'></div>"
      "<div class='field'><label>Password</label>"
      "<div class='pw-wrap'>"
      "<input type='password' id='pass' name='pass' placeholder='leave blank for open networks'>"
      "<button type='button' class='show-btn' onclick=\""
        "var p=document.getElementById('pass');"
        "p.type=p.type=='password'?'text':'password';"
        "this.textContent=p.type=='password'?'Show':'Hide';"
        "\">Show</button></div></div>"
      "<div class='field'><label>Camera Name</label>"
      "<input type='text' name='name' placeholder='ESP32 Camera'></div>"
      "<button type='submit'>Save &amp; Connect</button>"
      "</form>"
      "<script>"
      "fetch('/scan').then(r=>r.json()).then(function(nets){"
        "var d=document.getElementById('nets');"
        "if(!nets.length){d.innerHTML='<div style=\"padding:10px;color:#888\">No networks found</div>';return;}"
        "d.innerHTML=nets.sort(function(a,b){return b.r-a.r;}).map(function(n){"
          "var bars=n.r>-60?'▂▄▆█':n.r>-70?'▂▄▆▁':n.r>-80?'▂▄▁▁':'▂▁▁▁';"
          "return '<div class=\"net\" onclick=\"document.getElementById(\\'ssid\\').value=\\''+n.s+'\\'\">'"
          "+'<span>'+n.s+(n.e?' 🔒':'')+'</span>'"
          "+'<span style=\"color:#888;font-size:12px\">'+bars+'</span></div>';"
        "}).join('');"
      "}).catch(function(){"
        "document.getElementById('nets').innerHTML='<div style=\"padding:10px;color:#c00\">Scan failed — type SSID manually</div>';"
      "});"
      "</script></body></html>";
    server.send(200, "text/html", html);
  });

  server.on("/save", HTTP_POST, [&]() {
    String newSSID = server.arg("ssid");
    String newPass = server.arg("pass");
    String newName = server.arg("name");
    if (newName.length() == 0) newName = "ESP32 Camera";

    prefs.begin("wifi", false);
    prefs.putString("ssid", newSSID);
    prefs.putString("pass", newPass);
    prefs.putString("name", newName);
    prefs.end();

    server.send(200, "text/html",
      "<!DOCTYPE html><html><body style='font-family:sans-serif;text-align:center;margin-top:60px'>"
      "<h2>Saved! Rebooting in 2 seconds...</h2>"
      "<p style='color:#666'>The camera will connect to <b>" + newSSID + "</b></p>"
      "</body></html>");
    delay(2000);
    ESP.restart();
  });

  server.begin();
  while (true) {
    server.handleClient();
    delay(10);
  }
}

// Everything the sensor needs told to it after a successful esp_camera_init().
// Factored out of setup() because cameraRecover() has to apply exactly the
// same settings — a re-initialised driver comes back with sensor defaults, and
// a camera that silently returns to being upside down after recovering is its
// own kind of broken.
void applySensorDefaults() {
  sensor_t *s = esp_camera_sensor_get();
  if (!s) {
    Serial.println("applySensorDefaults: no sensor handle");
    return;
  }

  // initial sensors are flipped vertically and colors are a bit saturated
  if (s->id.PID == OV3660_PID) {
    s->set_vflip(s, 1);        // flip it back
    s->set_brightness(s, 1);   // up the brightness just a bit
    s->set_saturation(s, -2);  // lower the saturation
  }
  // drop down frame size for higher initial frame rate
  if (config.pixel_format == PIXFORMAT_JPEG) {
    s->set_framesize(s, FRAMESIZE_QVGA);
  }

#if defined(CAMERA_MODEL_M5STACK_WIDE) || defined(CAMERA_MODEL_M5STACK_ESP32CAM)
  s->set_vflip(s, 1);
  s->set_hmirror(s, 1);
#endif

#if defined(CAMERA_MODEL_ESP32S3_EYE)
  s->set_vflip(s, 1);
#endif
}

// Tear the camera driver down and build it again.
//
// Called from the frame-grab watchdog in app_httpd.cpp when grabs have been
// failing for a while. The failure this exists for looks like this: /status
// answers instantly and reads sensor registers correctly, so SCCB is fine and
// the sensor is alive and addressable, but no frame ever arrives — /capture
// 500s after ~4 s and /stream sends nothing at all. The driver is waiting on a
// VSYNC that never comes.
//
// Returns true if the driver came back and can produce a frame. Being able to
// re-init is not the same as working, so we prove it with an actual grab
// rather than trusting the return code.
bool cameraRecover() {
  // Non-blocking: if another handler is already recovering, this one should
  // just report failure and let its own retry come round again. Waiting would
  // pile up httpd tasks on a camera that by definition is not producing
  // anything for them anyway.
  if (cameraRecoverLock == NULL ||
      xSemaphoreTake(cameraRecoverLock, 0) != pdTRUE) {
    return false;
  }

  Serial.println("Camera recovery: re-initialising driver...");
  esp_camera_deinit();
  delay(200);  // let the sensor's clocks settle before driving it again

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Camera recovery: re-init failed 0x%x\n", err);
    xSemaphoreGive(cameraRecoverLock);
    return false;
  }

  applySensorDefaults();

  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("Camera recovery: re-init succeeded but still no frames");
    xSemaphoreGive(cameraRecoverLock);
    return false;
  }
  esp_camera_fb_return(fb);

  Serial.println("Camera recovery: back, and producing frames");
  xSemaphoreGive(cameraRecoverLock);
  return true;
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println("setup");
  
  // WiFi must be initialised before the camera driver allocates DRAM frame
  // buffers. On boards with no PSRAM (512 KB internal only) the camera grabs
  // most of the heap; if WiFi tries to init afterwards it can't allocate its
  // NVS config and fails silently.
  setupWifi();

  cameraRecoverLock = xSemaphoreCreateMutex();

  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 10000000;
  config.frame_size = FRAMESIZE_SVGA;
  config.pixel_format = PIXFORMAT_JPEG;  // for streaming
  //config.pixel_format = PIXFORMAT_RGB565; // for face detection/recognition
  config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.jpeg_quality = 12;
  config.fb_count = 1;

  if (config.pixel_format == PIXFORMAT_JPEG) {
    if (psramFound()) {
      config.frame_size = FRAMESIZE_HD;
      config.jpeg_quality = 10;
      config.fb_count = 2;
      config.grab_mode = CAMERA_GRAB_LATEST;
    } else {
      config.frame_size = FRAMESIZE_SVGA;
      config.fb_location = CAMERA_FB_IN_DRAM;
      config.fb_count = 1;
    }
  } else {
    // Best option for face detection/recognition
    config.frame_size = FRAMESIZE_240X240;
#if CONFIG_IDF_TARGET_ESP32S3
    config.fb_count = 2;
#endif
  }

#if defined(CAMERA_MODEL_ESP_EYE)
  pinMode(13, INPUT_PULLUP);
  pinMode(14, INPUT_PULLUP);
#endif

  // camera init
  Serial.println("Camera init...");

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Camera init failed with error 0x%x\n", err);
    return;
  }
  Serial.printf("Camera init success 0x%x\n", err);

  Serial.printf("PSRAM: %d bytes\n", ESP.getPsramSize());

  applySensorDefaults();

// Setup LED FLash if LED pin is defined in camera_pins.h
#if defined(LED_GPIO_NUM)
  setupLedFlash();
#endif

  startCameraServer();

  Serial.print("Camera Ready! Use 'http://");
  Serial.print(WiFi.localIP());
  Serial.println("' to connect");

#if SKIP_CAMERA_REGISTRY
  // Say it out loud. A camera that never appears in HomeCameras looks broken
  // from the other end, and this is the one line that explains why it is not.
  Serial.println("Standalone mode: not registering with the camera registry.");
  Serial.print("  stream:  http://");
  Serial.print(WiFi.localIP());
  Serial.println(":81/stream");
  Serial.print("  still:   http://");
  Serial.print(WiFi.localIP());
  Serial.println("/capture");
#else
  // Register camera with registry on startup
  registerCamera();
  lastRegistration = millis();
#endif
}

void loop() {
#if !SKIP_CAMERA_REGISTRY
  // Re-register or heartbeat periodically to keep camera marked as active
  if (millis() - lastRegistration >= REGISTRATION_INTERVAL) {
    if (WiFi.status() == WL_CONNECTED) {
      if (!cameraRegistered) {
        registerCamera();
      } else {
        sendHeartbeat();
      }
    }
    lastRegistration = millis();
  }
#endif

  delay(1000);
}

#if !SKIP_CAMERA_REGISTRY

void registerCamera() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Cannot register camera: WiFi not connected");
    return;
  }

  HTTPClient http;
  String registryUrl = String(REGISTRY_BASE_URL) + "/api/register";

  Serial.printf("Registering camera with registry at %s...\n", registryUrl.c_str());

  if (http.begin(registryUrl)) {
    http.addHeader("Content-Type", "application/json");

    String ip = WiFi.localIP().toString();

    prefs.begin("wifi", true);
    String camName = prefs.getString("name", "ESP32 Camera");
    prefs.end();
    if (camName == "ESP32 Camera") camName = "ESP32 Camera " + ip;

    String payload = "{";
    payload += "\"ip_address\":\"" + ip + "\",";
    payload += "\"name\":\"" + camName + "\",";
    payload += "\"port\":81,";
    payload += "\"stream_url\":\"/stream\",";
    payload += "\"stream_type\":\"mjpeg\",";
    payload += "\"status\":\"online\"";
    payload += "}";

    int httpResponseCode = http.POST(payload);

    if (httpResponseCode == 200 || httpResponseCode == 201) {
      Serial.printf("Camera registered successfully! Response: %d\n", httpResponseCode);
      cameraRegistered = true;
    } else {
      Serial.printf("Failed to register camera. HTTP code: %d\n", httpResponseCode);
      if (httpResponseCode > 0) {
        Serial.printf("Response: %s\n", http.getString().c_str());
      }
    }

    http.end();
  } else {
    Serial.println("Failed to connect to camera registry");
  }
}

void sendHeartbeat() {
  if (WiFi.status() != WL_CONNECTED) return;

  HTTPClient http;
  String url = String(REGISTRY_BASE_URL) + "/api/" + WiFi.localIP().toString() + "/ping";

  if (http.begin(url)) {
    int code = http.GET();
    if (code == 200) {
      Serial.printf("Heartbeat OK (%d)\n", code);
    } else {
      // Registry may have lost the record (e.g. restarted); re-register next cycle
      Serial.printf("Heartbeat failed (%d), will re-register\n", code);
      cameraRegistered = false;
    }
    http.end();
  }
}

#endif  // !SKIP_CAMERA_REGISTRY
