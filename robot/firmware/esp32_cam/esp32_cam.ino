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
 * Everything the camera must do arrives in the reply to its own live-frame
 * push (/camera/frame/) — it polls nothing else:
 *   * `commands`: snapshot / scan requests (from the dashboard or by voice,
 *     "امسح القطعة دي"), answered with the command_id so the robot can speak
 *     the result;
 *   * `enroll`: during a staff face-enrollment round, who is being called —
 *     the cam then sends captures to /enroll/capture/ until they're enrolled.
 * Motion (frame difference or the PIR on GPIO13) triggers a face check only;
 * part scans happen on request, not on every movement.
 * ------------------------------------------------------------------
 */

#include "esp_camera.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>

// A request body held in RAM, handed to HTTPClient as a stream. POST(buffer,
// size) writes the whole body in one call and fails with -3 (SEND_PAYLOAD_
// FAILED) when TLS accepts only part of it — which happens with any camera
// JPEG or voice clip over HTTPS. The stream path sends it in chunks and
// retries partial writes.
class BufStream : public Stream {
  const uint8_t* p_; size_t n_; size_t i_ = 0;
 public:
  BufStream(const uint8_t* p, size_t n) : p_(p), n_(n) {}
  int available() override { return (int)(n_ - i_); }
  int read() override { return i_ < n_ ? p_[i_++] : -1; }
  int peek() override { return i_ < n_ ? p_[i_] : -1; }
  size_t readBytes(char* b, size_t len) {
    size_t k = (n_ - i_ < len) ? n_ - i_ : len;
    memcpy(b, p_ + i_, k); i_ += k; return k;
  }
  size_t write(uint8_t) override { return 0; }
  void flush() override {}
};

// ONE connection to the backend, kept open and reused by every request.
// A fresh TLS handshake per frame cost 1-2 s on this chip (more over a 4G
// router) and was most of why the live view crawled; reusing the connection
// leaves only the upload itself. The certificate isn't pinned — the device
// token is what authenticates the robot.
WiFiClientSecure apiTls;
HTTPClient api;

bool beginApi(const String& url) {
  api.setReuse(true);
  api.setConnectTimeout(8000);
  apiTls.setHandshakeTimeout(15);   // the core's default is 120 s
  if (url.startsWith("https://")) { apiTls.setInsecure(); return api.begin(apiTls, url); }
  return api.begin(url);
}

// Finish a request; drop the connection when it ended in an unknown state so
// the next request reconnects cleanly.
void endApi(bool clean) {
  api.end();
  if (!clean) apiTls.stop();
}
#include <ArduinoJson.h>

// ---------------- Configuration ----------------
const char* WIFI_SSID   = "YOUR_WIFI";
const char* WIFI_PASS   = "YOUR_PASS";
const char* API_BASE    = "http://192.168.1.20:8000/api/robot/v1";
const char* ROBOT_TOKEN = "PASTE_DEVICE_TOKEN_FROM_ADMIN";

// PIR / IR presence sensor on GPIO13. Not fitted by default: an unconnected
// input floats and would report "someone's here" at random (endless face
// checks + false after-hours alerts), so it's only read when set to 1.
#define HAS_PRESENCE_SENSOR 0
#define PRESENCE_PIN 13

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
  Serial.printf("[CAM] WiFi \"%s\"", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  // Power save makes the radio doze between beacons, adding up to a few
  // hundred ms to every upload: the live view crawled. The camera is on
  // mains power, so it stays awake.
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    Serial.print(".");
    // Wrong password or no router: say so and start over instead of hanging.
    if (millis() - t0 > 30000) { Serial.println(" no WiFi, restarting"); ESP.restart(); }
  }
  Serial.printf(" %s (signal %d dBm)\n", WiFi.localIP().toString().c_str(), (int) WiFi.RSSI());
}

bool initCamera() {
  camera_config_t c = {};   // zero every field — unset ones must not be garbage
  c.ledc_channel = LEDC_CHANNEL_0; c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM; c.pin_d1 = Y3_GPIO_NUM; c.pin_d2 = Y4_GPIO_NUM;
  c.pin_d3 = Y5_GPIO_NUM; c.pin_d4 = Y6_GPIO_NUM; c.pin_d5 = Y7_GPIO_NUM;
  c.pin_d6 = Y8_GPIO_NUM; c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM; c.pin_pclk = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM; c.pin_href = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM; c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM; c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000; c.pixel_format = PIXFORMAT_JPEG;
  // Live frames are VGA (640x480, ~20 KB): half the upload of SVGA, which a
  // 4G link can push several times a second. Part scans switch to SVGA for
  // the detail a part number needs (captureScan).
  c.frame_size = FRAMESIZE_VGA;
  c.jpeg_quality = 14;
  // AI-Thinker has 4 MB PSRAM: two buffers + "latest" so every capture is a
  // fresh frame (a stale frame would enroll/recognize whoever stood there
  // a second ago).
  c.fb_count = psramFound() ? 2 : 1;
  c.fb_location = psramFound() ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
  c.grab_mode = CAMERA_GRAB_LATEST;
  return esp_camera_init(&c) == ESP_OK;
}

// POST a JPEG as multipart/form-data with extra text fields ("k=v&k2=v2").
// Returns the HTTP code; the response body is written to `out`.
int postJpeg(const String& endpoint, camera_fb_t* fb, const String& fields, String& out) {
  const String boundary = "----MoussTecCam";
  String head = "";
  int start = 0;
  while (start < (int)fields.length()) {
    int amp = fields.indexOf('&', start);
    if (amp < 0) amp = fields.length();
    String kv = fields.substring(start, amp);
    int eq = kv.indexOf('=');
    if (eq > 0) {
      head += "--" + boundary + "\r\nContent-Disposition: form-data; name=\"" +
              kv.substring(0, eq) + "\"\r\n\r\n" + kv.substring(eq + 1) + "\r\n";
    }
    start = amp + 1;
  }
  head += "--" + boundary + "\r\n"
          "Content-Disposition: form-data; name=\"image\"; filename=\"cam.jpg\"\r\n"
          "Content-Type: image/jpeg\r\n\r\n";
  String tail = "\r\n--" + boundary + "--\r\n";

  size_t total = head.length() + fb->len + tail.length();
  uint8_t* body = (uint8_t*) malloc(total);
  if (!body) return -2;
  size_t o = 0;
  memcpy(body + o, head.c_str(), head.length()); o += head.length();
  memcpy(body + o, fb->buf, fb->len);            o += fb->len;
  memcpy(body + o, tail.c_str(), tail.length());

  beginApi(String(API_BASE) + endpoint);
  api.addHeader("X-Robot-Token", ROBOT_TOKEN);
  api.addHeader("Content-Type", "multipart/form-data; boundary=" + boundary);
  api.setTimeout(8000);
  BufStream bs(body, total);
  int code = api.sendRequest("POST", &bs, total);
  out = (code > 0) ? api.getString() : "";
  endApi(code > 0);
  free(body);
  return code;
}

// Grab a fresh frame and POST it. Returns the HTTP code.
int captureAndPost(const String& endpoint, const String& fields) {
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) return -1;
  String resp;
  int code = postJpeg(endpoint, fb, fields, resp);
  esp_camera_fb_return(fb);
  Serial.printf("[CAM] %s → %d %s\n", endpoint.c_str(), code, resp.substring(0, 120).c_str());
  return code;
}

// ---------------------------------------------------------------------------
// 24/7 live push + motion + head-tracking
// ---------------------------------------------------------------------------
// The camera NEVER sleeps. Every ~1.5s it pushes the current frame to
// /camera/frame/ (dashboard live view). A cheap frame-difference (or the PIR)
// flags motion; the backend decides if it's after-hours and raises an alert.

unsigned long lastFramePush = 0;
unsigned long framePushMs = 1500;   // idle cadence; the backend sets it
bool enrollActive = false;          // a staff face-enrollment round is running

// Act on what the server put in the /camera/frame/ reply.
void handleFrameReply(const String& resp) {
  DynamicJsonDocument doc(3072);
  if (deserializeJson(doc, resp)) return;

  long v = doc["push_interval_ms"] | 0;
  if (v >= 100 && v <= 5000) framePushMs = (unsigned long) v;

  enrollActive = !doc["enroll"].isNull() && (doc["enroll"]["active"] | false);

  for (JsonObject c : doc["commands"].as<JsonArray>()) {
    long id = c["command_id"] | 0;
    String kind = c["kind"] | "";
    if (kind == "snapshot") {
      String reason = c["payload"]["reason"] | "manual";
      captureAndPost("/snapshot/", "reason=" + reason + "&command_id=" + String(id));
    } else if (kind == "scan") {
      String purpose = c["payload"]["purpose"] | "lookup";
      delay(1200);  // give them a moment to hold the part up after the prompt
      captureScan("purpose=" + purpose + "&command_id=" + String(id));
    }
  }
}

// Part scan at SVGA: the sensor switches size, the first frames after a
// switch are stale or half-exposed, so two are dropped before the capture.
void captureScan(const String& fields) {
  sensor_t* s = esp_camera_sensor_get();
  bool big = psramFound() && s;
  if (big) {
    s->set_framesize(s, FRAMESIZE_SVGA);
    for (int i = 0; i < 2; i++) { camera_fb_t* f = esp_camera_fb_get(); if (f) esp_camera_fb_return(f); }
  }
  captureAndPost("/scan/", fields);
  if (big) {
    s->set_framesize(s, FRAMESIZE_VGA);
    camera_fb_t* f = esp_camera_fb_get();
    if (f) esp_camera_fb_return(f);
  }
}

// Push the current frame to /camera/frame/ with a motion flag. Every 20
// frames the log shows the average round trip and size, so a slow link is
// visible without flooding the monitor.
void pushLiveFrame(bool motion) {
  static unsigned long sumMs = 0, sumBytes = 0;
  static int frames = 0, failed = 0;
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) return;
  String resp;
  unsigned long t0 = millis();
  size_t len = fb->len;
  int code = postJpeg("/camera/frame/", fb, String("motion=") + (motion ? "1" : "0"), resp);
  esp_camera_fb_return(fb);
  sumMs += millis() - t0; sumBytes += len; frames++;
  if (code != 200) failed++;
  if (frames == 20) {
    Serial.printf("[CAM] 20 frames: %lu ms each, %lu KB each, %d failed (last %d)\n",
                  sumMs / 20, sumBytes / 20 / 1024, failed, code);
    sumMs = sumBytes = 0; frames = failed = 0;
  }
  if (code == 200) handleFrameReply(resp);
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
#if HAS_PRESENCE_SENSOR
  if (digitalRead(PRESENCE_PIN) == HIGH) return true;
#endif
  return moved;
}

// If a face is detected off-center, tell the backend to turn the head.
// (Face box → offset in [-1,1]; feed it from a face detector if you add one.)
void trackFaceHead(float faceCenterX /* 0..1, 0.5 = centered */) {
  float offset = (faceCenterX - 0.5f) * 2.0f;   // → [-1,1]
  if (fabs(offset) < 0.12f) return;             // already looking at them
  beginApi(String(API_BASE) + "/look/");
  api.addHeader("X-Robot-Token", ROBOT_TOKEN);
  api.addHeader("Content-Type", "application/x-www-form-urlencoded");
  int code = api.POST("offset=" + String(offset, 3));
  if (code > 0) api.getString();
  endApi(code > 0);
}

void setup() {
  Serial.begin(115200);
#if HAS_PRESENCE_SENSOR
  pinMode(PRESENCE_PIN, INPUT);
#endif
  Serial.println("\n[CAM] Mouss Tec camera");
  connectWifi();
  if (!initCamera()) { Serial.println("[CAM] init failed: check the ribbon cable and the 5V supply"); }
  else               { Serial.printf("[CAM] ready — 24/7 (PSRAM %s)\n", psramFound() ? "yes" : "NO: enable it in Tools"); }
}

unsigned long lastFaceCheck = 0;
unsigned long lastEnrollShot = 0;

void loop() {
  // If Wi-Fi dropped, keep trying to reconnect but DON'T stop the camera —
  // the bridge ESP32 buffers offline work; this node just resumes pushing when
  // the link is back.
  // Reconnect every 5 s: restarting the attempt every 0.5 s never let it finish.
  static unsigned long lastRetry = 0;
  if (WiFi.status() != WL_CONNECTED) {
    if (millis() - lastRetry > 5000) { WiFi.reconnect(); lastRetry = millis(); }
    delay(100);
    return;
  }

  bool motion = detectMotion();

  // Live frame push (throttled by the server-driven cadence) — never closes.
  // Its reply also carries snapshot/scan commands and the enrollment state.
  if (millis() - lastFramePush > framePushMs) {
    pushLiveFrame(motion);
    lastFramePush = millis();
  }

  // Staff face enrollment: keep sending captures of whoever was just called
  // until the server says they're enrolled (it moves on by itself).
  if (enrollActive) {
    if (millis() - lastEnrollShot > 900) {
      captureAndPost("/enroll/capture/", "");
      lastEnrollShot = millis();
    }
    delay(50);
    return;  // no attendance checks while enrolling
  }

  // Someone in front of the robot: passive face check (clock-in on first
  // sighting, authorizes the next few minutes of actions). Throttled.
  if (motion && millis() - lastFaceCheck > 4000) {
    captureAndPost("/face/", "purpose=authorize");
    lastFaceCheck = millis();
  }
  delay(50);
}
