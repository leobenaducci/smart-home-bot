/**
 * ESP32 Camera Registration Example
 * Shows how to register with Camera Registry Server using Arduino IDE
 * 
 * Installation:
 * 1. Install Arduino IDE
 * 2. Add ESP32 board manager URL: https://esphome.io/package/addon-repositories#esp32
 * 3. Install Camera, WiFi, and HTTP client libraries
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <WebServer.h>
#include <esp_camera.h>

// ==== Configuration ====
const char* REGISTRY_URL = "http://compute.home:5001"; // Change to your registry server IP
const char* CAMERA_ID = "CAM-LIVING-001";
const char* CAMERA_NAME = "Living Room Camera";

// WiFi credentials
const char* ssid = "YOUR_WIFI_SSID";
const char* password = "YOUR_WIFI_PASSWORD";

// ==== Setup ====
void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println("Initializing Camera...");
  
  // Initialize camera (this part comes from your CameraWebServer code)
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = 26;
  config.pin_d1 = 27;
  config.pin_d2 = -1;
  config.pin_d3 = -1;
  config.pin_xclk = 16;
  config.pin_pclk = 17;
  config.pin_vsync = 18;
  config.pin_hsync = 19;
  config.pin_href = 23;
  config.pin_dc = 5;
  
  config.image_format = PIXFORMAT_JPEG; // Faster for streaming
  config.jpeg_quality = 10;
  config.fb_count = 1;
  config.grab_mode = CAMERA_GRAB_ALL; // Grab until push command
  
  if (!camera_init(&config, NULL)) {
    Serial.print("Camera init failed with error: ");
    Serial.println((int)esp_err_no());
    return;
  }
  
  Serial.println("Camera initialized successfully");
  Serial.printf("XCLK: %d, PCLK: %d, JPEG_Q: %d, FB_COUNT: %d\n", 
    config.pin_xclk, config.pin_pclk, config.jpeg_quality, config.fb_count);
  
  // ==== Connect to WiFi ====
  Serial.printf("Connecting to %s\n", ssid);
  
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);
  
  int waited = 0;
  while (WiFi.status() != WL_CONNECTED && waited < 20) {
    delay(500);
    Serial.print(".");
    waited++;
  }
  
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi connected");
    Serial.printf("IP Address: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("\nWiFi connection failed!");
    return;
  }
  
  // ==== Register with Registry ====
  registerWithRegistry();
  
  // ==== Setup Web Server for streaming ====
  setupCameraServer();
}

// ==== Registry API Functions ====

void registerWithRegistry() {
  Serial.println("\n=== Registering with Camera Registry ===");
  
  HTTPClient http;
  http.begin(String(REGISTRY_URL) + "/api/register");
  http.addHeader("Content-Type", "application/json");

  // `ip_address` at the TOP LEVEL. The server reads it from there and refuses
  // the request with 400 "ip_address is required" if it is missing — and this
  // sample used to bury it inside `metadata`, so every camera built from it
  // was rejected before it ever appeared in the registry.
  //
  // Built with String rather than adjacent literals for the same reason it has
  // to be: WiFi.localIP() is only known at run time, and the old
  //     "...\"ip_address\": \"" String(WiFi.localIP()) "\""
  // does not compile at all — a String object cannot be pasted between two
  // string literals.
  String jsonPayload = String("{") +
    "\"ip_address\": \"" + WiFi.localIP().toString() + "\"," +
    "\"camera_id\": \"" + CAMERA_ID + "\"," +
    "\"name\": \"" + CAMERA_NAME + "\"," +
    "\"port\": 80," +
    "\"stream_type\": \"mjpeg\"," +
    "\"stream_url\": \"/stream\"," +
    "\"capabilities\": [\"mjpeg\", \"web_server\"]," +
    "\"metadata\": {" +
      "\"resolution\": \"1920x1080\"," +
      "\"fps\": 30," +
      "\"sensor\": \"OV2640\"" +
    "}" +
  "}";

  Serial.println(jsonPayload);
  int code = http.POST(jsonPayload);
  
  if (code == 201) {
    Serial.println("Registration successful!");
    Serial.printf("Status code: %d\n", code);
    
    String response = http.getString();
    Serial.printf("Registry response:\n%s\n", response.c_str());
    
    http.end();
    
    // Start heartbeat thread
    startHeartbeatThread();
    
  } else {
    Serial.printf("Registration failed: %d\n", code);
    Serial.println("Check registry server logs");
    
    http.end();
  }
}

void startHeartbeatThread() {
  // ESP32 can't use threads easily in Arduino IDE, so use task (ESP-IDF)
  // or simply use loop() with delay
  Serial.println("Starting heartbeat...");
  
  // Send heartbeat every 30 seconds
  unsigned long lastHeartbeat = millis();
  
  while (true) {
    delay(100);
    
    if (millis() - lastHeartbeat > 30000) {
      lastHeartbeat = millis();
      sendHeartbeat();
    }
  }
}

void sendHeartbeat() {
  if (WiFi.status() != WL_CONNECTED) return;
  
  HTTPClient http;
  // By IP, not by CAMERA_ID: the registry keys every camera on its
  // address, so /api/<camera_id>/ping is a 404 for ever.
  http.begin(String(REGISTRY_URL) + "/api/" + WiFi.localIP().toString() + "/ping");
  int code = http.GET();
  
  if (code == 200) {
    Serial.printf("[HEARTBEAT] Status: %d - %s\n", code, http.getString().c_str());
  } else {
    Serial.printf("[HEARTBEAT] Error: %d\n", code);
  }
  
  http.end();
}

void setupCameraServer() {
  // Setup your camera HTTP server for streaming
  // This is your existing CameraWebServer setup code
}

void loop() {
  // Main camera server loop
  // Your existing streaming logic goes here
}

/*
 * ESP-IDF Integration Example
 * If using ESP-IDF instead of Arduino IDE:
 * 
 * void app_main(void) {
 *     // Initialize camera
 *     // Initialize WiFi
 *     
 *     // Register
 *     http_client_t client;
 *     http_client_init(&client);
 *     http_client_set_timeout(&client, 5000);
 *     
 *     http_client_post(&client, REGISTRY_URL "/api/register", json_data);
 *     
 *     // Start periodic heartbeat task
 *     xTaskCreate(heartbeat_task, "heartbeat", 8192, NULL, 5, NULL);
 * }
 * 
 * void heartbeat_task(void *pvParameters) {
 *     while (1) {
 *         sendHeartbeat();
 *         vTaskDelay(pdMS_TO_TICKS(30000)); // 30 seconds
 *     }
 * }
 */