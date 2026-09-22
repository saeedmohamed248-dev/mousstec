/*
 * esp32_cam.ino
 * ------------------------------------------------------------------
 * Mouss Tec Robot — ESP32-CAM (AI-Thinker) vision node.
 *
 * Two jobs, both POST a JPEG to the Mouss Tec backend:
 *   1. Part scan  → POST /api/robot/v1/scan/  (multipart 'image', 'purpose')
 *      Backend identifies the part and returns stock + RETAIL price (never
 *      wholesale). For used parts (purpose=scrap) it returns a condition-based
 *      suggested RETAIL price.
 *   2. Face scan  → POST /api/robot/v1/face/  (embedding or image)
 *      Backend matches the face to an authorized employee, clocks attendance,
 *      and authorizes privileged actions (sales).
 *
 * Real face embeddings are best produced by a model; on the plain ESP32-CAM
 * that's heavy, so the common pattern is: send the JPEG to the backend and let
 * a server-side model extract the embedding. This sketch sends the image and
 * lets the backend do recognition (the /face/ endpoint accepts an image).
 *
 * Trigger: a PIR/ultrasonic on GPIO13 (presence) or a button starts a scan.
 * ------------------------------------------------------------------
 */

#include "esp_camera.h"
#include <WiFi.h>
#include <HTTPClient.h>

// ---------------- Configuration ----------------
const char* WIFI_SSID   = "YOUR_WIFI";
const char* WIFI_PASS   = "YOUR_PASS";
const char* API_BASE    = "http://192.168.1.20:8000/api/robot/v1";
const char* ROBOT_TOKEN = "PASTE_DEVICE_TOKEN_FROM_ADMIN";

#define PRESENCE_PIN 13   // PIR / IR presence sensor (optional)

// ---- AI-Thinker ESP32-CAM pin map (standard) ----
#define PWDN_GPIO_NUM 32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 0
#define SIOD_GPIO_NUM 26
#define SIOC_GPIO_NUM 27
#define Y9_GPIO_NUM 35
#define Y8_GPIO_NUM 34
#define Y7_GPIO_NUM 39
#define Y6_GPIO_NUM 36
#define Y5_GPIO_NUM 21
#define Y4_GPIO_NUM 19
#define Y3_GPIO_NUM 18
#define Y2_GPIO_NUM 5
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM 23
#define PCLK_GPIO_NUM 22

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) delay(400);
}

bool initCamera() {
  camera_config_t c;
  c.ledc_channel = LEDC_CHANNEL_0; c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM; c.pin_d1 = Y3_GPIO_NUM; c.pin_d2 = Y4_GPIO_NUM;
  c.pin_d3 = Y5_GPIO_NUM; c.pin_d4 = Y6_GPIO_NUM; c.pin_d5 = Y7_GPIO_NUM;
  c.pin_d6 = Y8_GPIO_NUM; c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM; c.pin_pclk = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM; c.pin_href = HREF_GPIO_NUM;
  c.pin_sscb_sda = SIOD_GPIO_NUM; c.pin_sscb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM; c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000; c.pixel_format = PIXFORMAT_JPEG;
  c.frame_size = FRAMESIZE_SVGA;   // 800x600 — good balance for part detail
  c.jpeg_quality = 12; c.fb_count = 1;
  return esp_camera_init(&c) == ESP_OK;
}

// POST the current frame as multipart/form-data with a `purpose` field.
// endpoint = "/scan/" or "/face/".
int postFrame(const char* endpoint, const char* purpose) {
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) return -1;

  const String boundary = "----MoussTecCam";
  String head = "--" + boundary + "\r\n"
    "Content-Disposition: form-data; name=\"purpose\"\r\n\r\n" + String(purpose) + "\r\n"
    "--" + boundary + "\r\n"
    "Content-Disposition: form-data; name=\"image\"; filename=\"scan.jpg\"\r\n"
    "Content-Type: image/jpeg\r\n\r\n";
  String tail = "\r\n--" + boundary + "--\r\n";

  HTTPClient http;
  http.begin(String(API_BASE) + endpoint);
  http.addHeader("X-Robot-Token", ROBOT_TOKEN);
  http.addHeader("Content-Type", "multipart/form-data; boundary=" + boundary);

  size_t total = head.length() + fb->len + tail.length();
  uint8_t* body = (uint8_t*) malloc(total);
  if (!body) { esp_camera_fb_return(fb); http.end(); return -2; }

  size_t o = 0;
  memcpy(body + o, head.c_str(), head.length()); o += head.length();
  memcpy(body + o, fb->buf, fb->len);            o += fb->len;
  memcpy(body + o, tail.c_str(), tail.length()); o += tail.length();

  int code = http.POST(body, total);
  String resp = http.getString();
  Serial.printf("[CAM] %s → %d: %s\n", endpoint, code, resp.c_str());

  free(body);
  esp_camera_fb_return(fb);
  http.end();
  return code;
}

// ---------------------------------------------------------------------------
// 24/7 live push + motion + head-tracking
// ---------------------------------------------------------------------------
// The camera NEVER sleeps. Every ~1.5s it pushes the current frame to
// /camera/frame/ (dashboard live view). A cheap frame-difference (or the PIR)
// flags motion; the backend decides if it's after-hours and raises an alert.
// When a face is detected, its horizontal position drives /look/ so the head
// turns to face whoever is being spoken to.

unsigned long lastFramePush = 0;
unsigned long framePushMs = 1500;   // idle cadence; the backend bumps it while
                                    // a supervisor is watching (smooth MJPEG).

// Push the current frame to /camera/frame/ with an optional motion flag.
// Reads `push_interval_ms` from the reply to speed up/slow down on demand.
void pushLiveFrame(bool motion) {
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) return;

  const String boundary = "----MoussTecLive";
  String head = "--" + boundary + "\r\n"
    "Content-Disposition: form-data; name=\"motion\"\r\n\r\n" + String(motion ? "1" : "0") + "\r\n"
    "--" + boundary + "\r\n"
    "Content-Disposition: form-data; name=\"image\"; filename=\"live.jpg\"\r\n"
    "Content-Type: image/jpeg\r\n\r\n";
  String tail = "\r\n--" + boundary + "--\r\n";

  HTTPClient http;
  http.begin(String(API_BASE) + "/camera/frame/");
  http.addHeader("X-Robot-Token", ROBOT_TOKEN);
  http.addHeader("Content-Type", "multipart/form-data; boundary=" + boundary);
  size_t total = head.length() + fb->len + tail.length();
  uint8_t* body = (uint8_t*) malloc(total);
  if (body) {
    size_t o = 0;
    memcpy(body + o, head.c_str(), head.length()); o += head.length();
    memcpy(body + o, fb->buf, fb->len);            o += fb->len;
    memcpy(body + o, tail.c_str(), tail.length());
    int code = http.POST(body, total);
    free(body);
    // Honor the server's requested cadence (fast while someone is watching).
    if (code == 200) {
      String resp = http.getString();
      int i = resp.indexOf("push_interval_ms");
      if (i >= 0) {
        long v = resp.substring(resp.indexOf(':', i) + 1).toInt();
        if (v >= 100 && v <= 5000) framePushMs = (unsigned long) v;
      }
    }
  }
  esp_camera_fb_return(fb);
  http.end();
}

// Very cheap motion detector: average-luma difference between frames.
// Replace with a real motion/vision routine or rely on the PIR on GPIO13.
bool detectMotion() {
  static long prevAvg = -1;
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) return false;
  long sum = 0; for (size_t i = 0; i < fb->len; i += 64) sum += fb->buf[i];
  long avg = sum / (fb->len / 64 + 1);
  bool moved = (prevAvg >= 0) && (labs(avg - prevAvg) > 6);
  prevAvg = avg;
  esp_camera_fb_return(fb);
  return moved || digitalRead(PRESENCE_PIN) == HIGH;
}

// If a face is detected off-center, tell the backend to turn the head.
// (Face box → offset in [-1,1]; here a stub reads it from a detector you add.)
void trackFaceHead(float faceCenterX /* 0..1, 0.5 = centered */) {
  float offset = (faceCenterX - 0.5f) * 2.0f;   // → [-1,1]
  if (fabs(offset) < 0.12f) return;             // already looking at them
  HTTPClient http;
  http.begin(String(API_BASE) + "/look/");
  http.addHeader("X-Robot-Token", ROBOT_TOKEN);
  http.addHeader("Content-Type", "application/x-www-form-urlencoded");
  http.POST("offset=" + String(offset, 3));
  http.end();
}

void setup() {
  Serial.begin(115200);
  pinMode(PRESENCE_PIN, INPUT);
  connectWifi();
  if (!initCamera()) { Serial.println("[CAM] init failed"); }
  else               { Serial.println("[CAM] ready — 24/7"); }
}

void loop() {
  // If Wi-Fi dropped, keep trying to reconnect but DON'T stop the camera —
  // the bridge ESP32 buffers offline work; this node just resumes pushing when
  // the link is back.
  if (WiFi.status() != WL_CONNECTED) { WiFi.reconnect(); delay(500); return; }

  bool motion = detectMotion();

  // Live frame push (throttled by the server-driven cadence) — never closes.
  if (millis() - lastFramePush > framePushMs) {
    pushLiveFrame(motion);
    lastFramePush = millis();
  }

  // On presence/motion, run access-control face check + a part scan, and turn
  // the head toward the detected face (feed a real face-center from a detector).
  if (motion) {
    postFrame("/face/", "authorize");
    // trackFaceHead(faceCenterXFromDetector);   // wire to your face detector
    delay(300);
    postFrame("/scan/", "pos");
    delay(2500);   // debounce
  }
  delay(50);
}
