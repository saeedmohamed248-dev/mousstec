/*
 * esp32_bridge.ino
 * ------------------------------------------------------------------
 * Mouss Tec Robot — ESP32 "brain-stem": Wi-Fi + API + Audio + serial bridge.
 *
 * Responsibilities:
 *   1. Connect to Wi-Fi and the Mouss Tec backend (device-token auth).
 *   2. Heartbeat + poll /api/robot/v1/motor/pending/ and forward each frame
 *      to the Arduino Mega over Serial2.
 *   3. Capture audio from an INMP441 I2S microphone, (optionally) do on-device
 *      wake-word/VAD, and POST the transcript to /voice/ — then play the spoken
 *      reply through a MAX98357A I2S amplifier.
 *   4. Relay motor commands the backend queues (issued by a face-authorized
 *      employee) down to the Mega.
 *
 * NOTE: The ESP32-CAM (vision/faces) is a SEPARATE board (esp32_cam.ino) — the
 * classic ESP32-CAM has no free pins for I2S audio + UART + relays, so audio +
 * bridge live here and vision lives there. Both share the same device token or
 * use one token each; here we use one bridge token.
 *
 * Speech-to-text: the INMP441 gives raw PCM. Full on-device STT is heavy, so
 * this sketch streams/ög uploads the PCM to the backend `/voice/` which can run
 * STT server-side (or you set `transcript` directly if you do wake-word + a
 * cloud STT on-device). Text-to-speech: the backend returns `reply` text; here
 * we call a TTS endpoint or a local synth — kept as a hook (`speak()`).
 * ------------------------------------------------------------------
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <driver/i2s.h>

// ---------------- Configuration ----------------
const char* WIFI_SSID   = "YOUR_WIFI";
const char* WIFI_PASS   = "YOUR_PASS";
const char* API_BASE    = "http://192.168.1.20:8000/api/robot/v1";  // laptop/server
const char* ROBOT_TOKEN = "PASTE_DEVICE_TOKEN_FROM_ADMIN";           // RobotDevice.api_token
const char* FIRMWARE_VERSION = "1.0.0";

// ---- Serial link to Arduino Mega (UART2) ----
// ESP32 GPIO17 = TX2 → Mega RX1(19);  ESP32 GPIO16 = RX2 → Mega TX1(18)
#define MEGA_TX 17
#define MEGA_RX 16

// ---- INMP441 I2S microphone (I2S port 0) ----
#define I2S_MIC_SCK   14   // BCLK
#define I2S_MIC_WS    15   // LRCLK / WS
#define I2S_MIC_SD    32   // DATA out of mic

// ---- MAX98357A I2S amplifier (I2S port 1) ----
#define I2S_AMP_BCLK  26
#define I2S_AMP_LRC   25
#define I2S_AMP_DIN   22

unsigned long lastHeartbeat = 0;
unsigned long lastMotorPoll = 0;

// ---------------- Wi-Fi ----------------
void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("WiFi");
  while (WiFi.status() != WL_CONNECTED) { delay(400); Serial.print("."); }
  Serial.printf(" connected: %s\n", WiFi.localIP().toString().c_str());
}

// ---------------- HTTP helpers ----------------
int httpPostJson(const String& path, const String& body, String& out) {
  HTTPClient http;
  http.begin(String(API_BASE) + path);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Robot-Token", ROBOT_TOKEN);
  int code = http.POST(body);
  out = http.getString();
  http.end();
  return code;
}

int httpGet(const String& path, String& out) {
  HTTPClient http;
  http.begin(String(API_BASE) + path);
  http.addHeader("X-Robot-Token", ROBOT_TOKEN);
  int code = http.GET();
  out = http.getString();
  http.end();
  return code;
}

// ---------------- Heartbeat ----------------
void sendHeartbeat() {
  StaticJsonDocument<128> doc;
  doc["firmware_version"] = FIRMWARE_VERSION;
  String body; serializeJson(doc, body);
  String resp;
  httpPostJson("/heartbeat/", body, resp);
}

// ---------------- Motor bridge ----------------
void pollAndForwardMotorCommands() {
  String resp;
  if (httpGet("/motor/pending/", resp) != 200) return;
  StaticJsonDocument<1024> doc;
  if (deserializeJson(doc, resp)) return;
  for (JsonObject c : doc["commands"].as<JsonArray>()) {
    const char* frame = c["frame"];      // e.g. "<head:left:800>"
    if (frame) { Serial2.print(frame); Serial.printf("→Mega %s\n", frame); }
  }
}

// ---------------- Audio: INMP441 mic ----------------
void setupMic() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = 16000,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = 0,
    .dma_buf_count = 4,
    .dma_buf_len = 1024,
    .use_apll = false,
  };
  i2s_pin_config_t pins = {
    .bck_io_num = I2S_MIC_SCK, .ws_io_num = I2S_MIC_WS,
    .data_out_num = I2S_PIN_NO_CHANGE, .data_in_num = I2S_MIC_SD,
  };
  i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL);
  i2s_set_pin(I2S_NUM_0, &pins);
}

// Capture ~2s of PCM into a buffer (very small demo capture). In production,
// stream this to the backend or run VAD/wake-word first to avoid dead air.
size_t captureAudio(uint8_t* buffer, size_t maxBytes) {
  size_t total = 0, bytesRead = 0;
  while (total < maxBytes) {
    i2s_read(I2S_NUM_0, buffer + total, maxBytes - total, &bytesRead, portMAX_DELAY);
    if (bytesRead == 0) break;
    total += bytesRead;
    if (total >= maxBytes) break;
  }
  return total;
}

// ---------------- Audio: MAX98357A amp (TTS playback hook) ----------------
void setupAmp() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate = 16000,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = 0,
    .dma_buf_count = 8,
    .dma_buf_len = 256,
    .use_apll = false,
  };
  i2s_pin_config_t pins = {
    .bck_io_num = I2S_AMP_BCLK, .ws_io_num = I2S_AMP_LRC,
    .data_out_num = I2S_AMP_DIN, .data_in_num = I2S_PIN_NO_CHANGE,
  };
  i2s_driver_install(I2S_NUM_1, &cfg, 0, NULL);
  i2s_set_pin(I2S_NUM_1, &pins);
}

// Play 16-bit PCM through the amp. Feed it a WAV/PCM stream returned by a TTS
// service, or synthesize on-device. Hook left minimal on purpose.
void playPcm(const uint8_t* pcm, size_t len) {
  size_t written = 0;
  i2s_write(I2S_NUM_1, pcm, len, &written, portMAX_DELAY);
}

// Ask the backend to handle a spoken turn. `transcript` here would come from an
// on-device STT or from streaming the mic PCM; kept as a parameter so the flow
// is testable without a full STT stack.
void handleVoiceTurn(const String& transcript) {
  StaticJsonDocument<256> doc;
  doc["transcript"] = transcript;
  String body; serializeJson(doc, body);
  String resp;
  if (httpPostJson("/voice/", body, resp) == 200) {
    StaticJsonDocument<1024> r;
    if (!deserializeJson(r, resp)) {
      const char* reply = r["reply"];
      Serial.printf("🔊 %s\n", reply ? reply : "");
      // speak(reply);  // route `reply` through your TTS → playPcm()
    }
  }
}

void setup() {
  Serial.begin(115200);
  Serial2.begin(115200, SERIAL_8N1, MEGA_RX, MEGA_TX);
  connectWifi();
  setupMic();
  setupAmp();
  sendHeartbeat();
  Serial.println("[ESP32] bridge ready");
}

void loop() {
  unsigned long now = millis();

  if (now - lastHeartbeat > 30000) { sendHeartbeat(); lastHeartbeat = now; }
  if (now - lastMotorPoll > 500)   { pollAndForwardMotorCommands(); lastMotorPoll = now; }

  // Voice loop would live here: detect wake-word, capture, STT, handleVoiceTurn().
  // handleVoiceTurn("هل يوجد طرمبة مياه لـ BMW E90؟");

  delay(20);
}
