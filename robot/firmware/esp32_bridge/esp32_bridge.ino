/*
 * esp32_bridge.ino
 * ------------------------------------------------------------------
 * Mouss Tec Robot — ESP32 "brain-stem": Wi-Fi + API + Audio + serial bridge.
 *
 * Responsibilities:
 *   1. Connect to Wi-Fi and the Mouss Tec backend (device-token auth).
 *   2. Poll /motor/pending/ and forward each frame to the Arduino Mega, and
 *      keep the Mega's 3s watchdog fed with a 1s <ping:0:0> (its own task,
 *      so long HTTP calls or speech never starve it).
 *   3. Voice: listen on the INMP441 mic with a simple energy VAD (or a push-
 *      to-talk button), send the utterance as WAV to /voice/, and speak the
 *      reply through the MAX98357A (the backend returns 16 kHz WAV).
 *   4. Say whatever the backend queues (dashboard "say", owner paging,
 *      staff face-enrollment name calls, voice-requested scan results).
 *   5. Telemetry every 15s: battery, CPU temp, SD free space, Wi-Fi, and the
 *      per-motor currents the Mega measures (predictive maintenance).
 *   6. Offline: cache the retail catalog on SD, queue events while offline,
 *      replay them to /sync/push/ on reconnect (idempotent by client_uid).
 *
 * The ESP32-CAM (vision/faces) is a SEPARATE board (esp32_cam.ino); the two
 * coordinate only through the backend.
 *
 * Its name: the robot answers only when called by name ("يا موس، …"). The
 * classic ESP32 can't run a wake-word model, so the VAD below uploads each
 * utterance and the SERVER checks for the name (robot/wakename.py): speech
 * not addressed to it gets an empty reply and nothing is spoken or stored.
 * Follow-ups within 20 s don't need the name; holding the push-to-talk
 * button skips the name check. Set the name on the dashboard device profile.
 *
 * Libraries: ArduinoJson (v6), built-in WiFi/HTTPClient/SD/SPI.
 * ------------------------------------------------------------------
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <driver/i2s.h>
#include <SPI.h>
#include <SD.h>

// ---------------- Configuration ----------------
const char* WIFI_SSID   = "YOUR_WIFI";
const char* WIFI_PASS   = "YOUR_PASS";
const char* API_BASE    = "http://192.168.1.20:8000/api/robot/v1";  // laptop/server
const char* ROBOT_TOKEN = "PASTE_DEVICE_TOKEN_FROM_ADMIN";           // printed once by create_robot_device
const char* FIRMWARE_VERSION = "2.0.0";

// ---- Fitted hardware (match what's on YOUR robot) ----
#define HAS_MIC            1   // INMP441 — needed for voice. Set 0 until it's
                               // wired: an unconnected data pin reads noise
                               // that the VAD would keep uploading.
#define HAS_SD             0   // microSD module (offline catalog/queue/clip)
#define HAS_BATTERY_SENSE  0   // 12V divider on GPIO34 (else battery isn't reported)

// ---- Serial link to Arduino Mega (UART2) ----
// ESP32 GPIO17 = TX2 → Mega RX1(19);  ESP32 GPIO16 = RX2 ← Mega TX1(18)
// ⚠️ Mega TX is 5V: put a divider (1k/2k) before ESP32 GPIO16.
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

// ---- microSD (SPI / VSPI) ----
#define SD_CS    5          // SCK 18, MISO 19, MOSI 23

// ---- Misc ----
#define PTT_BUTTON  4       // push-to-talk to GND (optional, INPUT_PULLUP)
#define BATTERY_ADC 34      // 12V battery via 100k/22k divider (input-only pin)
const float BATTERY_DIVIDER = (100.0 + 22.0) / 22.0;
const float BATTERY_EMPTY_V = 11.6, BATTERY_FULL_V = 12.7;   // lead-acid rest volts

// ---- Voice capture ----
const int SAMPLE_RATE = 16000;
const int MAX_RECORD_MS = 4000;                   // 4s × 16k × 2B = 128 KB
const int FRAME_SAMPLES = 512;                    // 32 ms per VAD frame
const int VAD_START_RMS = 900;                    // tune for your room/mic
const int VAD_STOP_RMS  = 500;
const int VAD_SILENCE_MS = 700;                   // end of utterance
const int MIN_SPEECH_MS  = 350;                   // ignore clicks/bangs

unsigned long lastHeartbeat = 0, lastMotorPoll = 0, lastCmdPoll = 0;
unsigned long lastTelemetry = 0, lastCatalogSync = 0;
bool sdReady = false;
bool wasOnline = true;

// Serial2 is written by the loop (motor frames) and the keep-alive task.
SemaphoreHandle_t megaLock;
// Highest current seen per motor since the last telemetry post (amps).
volatile float maxCurrent[4] = {0, 0, 0, 0};
const char* MOTOR_NAMES[4] = {"head", "arm_left", "arm_right", "track"};

// ---------------- Mega link ----------------
void megaSend(const char* frame) {
  if (xSemaphoreTake(megaLock, pdMS_TO_TICKS(200)) == pdTRUE) {
    Serial2.print(frame);
    xSemaphoreGive(megaLock);
  }
}

// Parse "<cur:head:1.23>" / "<fault:head:9.80>" lines coming back from the Mega.
void handleMegaLine(const String& line) {
  int p1 = line.indexOf(':'), p2 = line.indexOf(':', p1 + 1);
  if (p1 < 0 || p2 < 0) return;
  String kind = line.substring(0, p1), name = line.substring(p1 + 1, p2);
  float amps = line.substring(p2 + 1).toFloat();
  if (kind != "cur" && kind != "fault") return;
  for (int i = 0; i < 4; i++) {
    if (name == MOTOR_NAMES[i] && amps > maxCurrent[i]) maxCurrent[i] = amps;
  }
}

// Core-0 task: 1s keep-alive to the Mega + read its current reports. Runs
// independently of Wi-Fi/HTTP/audio so the watchdog only trips if THIS board
// is really dead.
void megaTask(void*) {
  String buf;
  unsigned long lastPing = 0;
  for (;;) {
    if (millis() - lastPing > 1000) { megaSend("<ping:0:0>"); lastPing = millis(); }
    while (Serial2.available()) {
      char c = (char) Serial2.read();
      if (c == '<') buf = "";
      else if (c == '>') { handleMegaLine(buf); buf = ""; }
      else if (buf.length() < 40) buf += c;
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

// ---------------- Wi-Fi ----------------
void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("WiFi");
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) { delay(400); Serial.print("."); }
  Serial.printf(" %s\n", WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString().c_str() : "offline");
}

// ---------------- HTTP helpers ----------------
int httpPostJson(const String& path, const String& body, String& out) {
  HTTPClient http;
  http.begin(String(API_BASE) + path);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Robot-Token", ROBOT_TOKEN);
  http.setTimeout(15000);
  int code = http.POST(body);
  out = (code > 0) ? http.getString() : "";
  http.end();
  return code;
}

int httpGet(const String& path, String& out) {
  HTTPClient http;
  http.begin(String(API_BASE) + path);
  http.addHeader("X-Robot-Token", ROBOT_TOKEN);
  http.setTimeout(10000);
  int code = http.GET();
  out = (code > 0) ? http.getString() : "";
  http.end();
  return code;
}

// ---------------- Audio: INMP441 mic ----------------
void setupMic() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
    // INMP441 with L/R tied to GND talks on the LEFT slot — but some ESP32
    // core versions swap the slots. If the VAD never triggers (RMS stays ~0),
    // change this to I2S_CHANNEL_FMT_ONLY_RIGHT.
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = 0,
    .dma_buf_count = 6,
    .dma_buf_len = FRAME_SAMPLES,
    .use_apll = false,
  };
  i2s_pin_config_t pins = {
    // MCLK unused; left at 0 it would be driven out on GPIO0 (the BOOT pin).
    .mck_io_num = I2S_PIN_NO_CHANGE,
    .bck_io_num = I2S_MIC_SCK, .ws_io_num = I2S_MIC_WS,
    .data_out_num = I2S_PIN_NO_CHANGE, .data_in_num = I2S_MIC_SD,
  };
  i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL);
  i2s_set_pin(I2S_NUM_0, &pins);
}

// Read one 32 ms frame as 16-bit PCM into `out`; returns its RMS level.
int32_t micFrame[FRAME_SAMPLES];
int readMicFrame(int16_t* out) {
  size_t got = 0;
  i2s_read(I2S_NUM_0, micFrame, sizeof(micFrame), &got, pdMS_TO_TICKS(100));
  int n = got / 4;
  double acc = 0;
  for (int i = 0; i < n; i++) {
    int32_t s = micFrame[i] >> 14;               // INMP441: 24-bit left-justified
    if (s > 32767) s = 32767; if (s < -32768) s = -32768;
    out[i] = (int16_t) s;
    acc += (double) s * s;
  }
  for (int i = n; i < FRAME_SAMPLES; i++) out[i] = 0;
  return n ? (int) sqrt(acc / n) : 0;
}

void flushMic(int ms) {                          // drop echo of our own speech
#if !HAS_MIC
  return;
#endif
  int16_t tmp[FRAME_SAMPLES];
  for (int t = 0; t < ms; t += 32) readMicFrame(tmp);
}

// ---------------- Audio: MAX98357A amp ----------------
void setupAmp() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = 0,
    .dma_buf_count = 8,
    .dma_buf_len = 256,
    .use_apll = false,
    .tx_desc_auto_clear = true,
  };
  i2s_pin_config_t pins = {
    .mck_io_num = I2S_PIN_NO_CHANGE,
    .bck_io_num = I2S_AMP_BCLK, .ws_io_num = I2S_AMP_LRC,
    .data_out_num = I2S_AMP_DIN, .data_in_num = I2S_PIN_NO_CHANGE,
  };
  i2s_driver_install(I2S_NUM_1, &cfg, 0, NULL);
  i2s_set_pin(I2S_NUM_1, &pins);
}

// Read exactly n bytes from the HTTP stream (false on timeout/close).
bool readExact(WiFiClient* s, uint8_t* dst, size_t n) {
  size_t got = 0; unsigned long t0 = millis();
  while (got < n && millis() - t0 < 5000) {
    int a = s->available();
    if (a > 0) got += s->readBytes(dst + got, min((size_t) a, n - got));
    else if (!s->connected()) return false;
    else delay(2);
  }
  return got == n;
}

// Speak `text`: the backend returns 16 kHz mono 16-bit WAV which we stream
// straight to the amp (chunk-walking the RIFF header to find "data").
void speak(const String& text) {
  if (text.length() == 0) return;
  Serial.printf("🔊 %s\n", text.c_str());
  if (WiFi.status() != WL_CONNECTED) return;
  StaticJsonDocument<768> doc;
  doc["text"] = text; doc["format"] = "wav";
  String body; serializeJson(doc, body);

  HTTPClient http;
  http.begin(String(API_BASE) + "/speak/");
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Robot-Token", ROBOT_TOKEN);
  http.setTimeout(20000);
  if (http.POST(body) != 200) { http.end(); return; }
  WiFiClient* s = http.getStreamPtr();

  uint8_t hdr[12];
  if (!readExact(s, hdr, 12) || memcmp(hdr, "RIFF", 4) || memcmp(hdr + 8, "WAVE", 4)) { http.end(); return; }
  uint8_t ch[8];
  while (readExact(s, ch, 8)) {                    // walk chunks to "data"
    uint32_t len = ch[4] | (ch[5] << 8) | (ch[6] << 16) | ((uint32_t) ch[7] << 24);
    if (!memcmp(ch, "data", 4)) break;
    for (uint32_t skip = 0; skip < len; skip++) { uint8_t b; if (!readExact(s, &b, 1)) { http.end(); return; } }
  }
  uint8_t buf[1024];
  unsigned long t0 = millis();
  while (s->connected() || s->available()) {
    int a = s->available();
    if (a <= 0) { if (millis() - t0 > 3000) break; delay(2); continue; }
    int n = s->readBytes(buf, min(a, (int) sizeof(buf)));
    size_t written; i2s_write(I2S_NUM_1, buf, n, &written, portMAX_DELAY);
    t0 = millis();
  }
  http.end();
  i2s_zero_dma_buffer(I2S_NUM_1);
  flushMic(300);
}

// Play a 16 kHz mono 16-bit WAV stored on the SD card (offline prompts).
void playSdWav(const char* path) {
  if (!sdReady || !SD.exists(path)) return;
  File f = SD.open(path, FILE_READ);
  if (!f) return;
  f.seek(12);
  uint8_t ch[8];
  while (f.read(ch, 8) == 8) {                     // walk chunks to "data"
    uint32_t len = ch[4] | (ch[5] << 8) | (ch[6] << 16) | ((uint32_t) ch[7] << 24);
    if (!memcmp(ch, "data", 4)) break;
    f.seek(f.position() + len);
  }
  uint8_t buf[1024];
  int n;
  while ((n = f.read(buf, sizeof(buf))) > 0) {
    size_t written; i2s_write(I2S_NUM_1, buf, n, &written, portMAX_DELAY);
  }
  f.close();
  i2s_zero_dma_buffer(I2S_NUM_1);
  flushMic(300);
}

// ---------------- Voice turn ----------------
// One contiguous buffer: [multipart head][44-byte WAV header][PCM][tail], so
// the upload is a single POST with no copying.
const char* BOUNDARY = "----MoussTecVoice";

void writeWavHeader(uint8_t* h, uint32_t pcmBytes) {
  uint32_t byteRate = SAMPLE_RATE * 2;
  memcpy(h, "RIFF", 4); uint32_t v = 36 + pcmBytes; memcpy(h + 4, &v, 4);
  memcpy(h + 8, "WAVEfmt ", 8); v = 16; memcpy(h + 16, &v, 4);
  uint16_t w = 1; memcpy(h + 20, &w, 2); w = 1; memcpy(h + 22, &w, 2);
  v = SAMPLE_RATE; memcpy(h + 24, &v, 4); memcpy(h + 28, &byteRate, 4);
  w = 2; memcpy(h + 32, &w, 2); w = 16; memcpy(h + 34, &w, 2);
  memcpy(h + 36, "data", 4); memcpy(h + 40, &pcmBytes, 4);
}

bool pttPressed() { return digitalRead(PTT_BUTTON) == LOW; }

// Record an utterance (VAD or while the PTT button is held) and send it.
void listenAndAnswer(int16_t* firstFrame) {
  bool ptt = pttPressed();
  // `ptt=1` tells the server the button was held: no need to say the name.
  String head = String("--") + BOUNDARY + "\r\n"
    "Content-Disposition: form-data; name=\"ptt\"\r\n\r\n" + (ptt ? "1" : "0") + "\r\n"
    "--" + BOUNDARY + "\r\n"
    "Content-Disposition: form-data; name=\"audio\"; filename=\"voice.wav\"\r\n"
    "Content-Type: audio/wav\r\n\r\n";
  String tail = String("\r\n--") + BOUNDARY + "--\r\n";
  const size_t maxPcm = (size_t) SAMPLE_RATE * 2 * MAX_RECORD_MS / 1000;
  size_t total = head.length() + 44 + maxPcm + tail.length();
  uint8_t* buf = (uint8_t*) malloc(total);
  if (!buf) { Serial.println("[VOICE] no memory"); return; }

  uint8_t* pcm = buf + head.length() + 44;
  size_t pcmBytes = 0;
  memcpy(pcm, firstFrame, FRAME_SAMPLES * 2); pcmBytes += FRAME_SAMPLES * 2;
  int silentMs = 0;
  while (pcmBytes + FRAME_SAMPLES * 2 <= maxPcm) {
    int rms = readMicFrame((int16_t*)(pcm + pcmBytes));
    pcmBytes += FRAME_SAMPLES * 2;
    if (ptt) { if (!pttPressed()) break; continue; }
    silentMs = (rms < VAD_STOP_RMS) ? silentMs + 32 : 0;
    if (silentMs >= VAD_SILENCE_MS) break;
  }
  int speechMs = (int)(pcmBytes / 2 * 1000 / SAMPLE_RATE) - silentMs;
  if (speechMs < MIN_SPEECH_MS) { free(buf); return; }

  if (WiFi.status() != WL_CONNECTED) {
    free(buf);
    // STT and TTS both live on the server, so offline the robot plays a
    // pre-recorded clip from SD ("النت فاصل دلوقتي، اسأل حد من الموظفين").
    playSdWav("/offline.wav");
    Serial.println("[VOICE] offline — can't transcribe right now");
    return;
  }

  memcpy(buf, head.c_str(), head.length());
  writeWavHeader(buf + head.length(), pcmBytes);
  memcpy(pcm + pcmBytes, tail.c_str(), tail.length());
  size_t sendLen = head.length() + 44 + pcmBytes + tail.length();

  HTTPClient http;
  http.begin(String(API_BASE) + "/voice/");
  http.addHeader("X-Robot-Token", ROBOT_TOKEN);
  http.addHeader("Content-Type", String("multipart/form-data; boundary=") + BOUNDARY);
  http.setTimeout(20000);
  int code = http.POST(buf, sendLen);
  String resp = (code > 0) ? http.getString() : "";
  http.end();
  free(buf);

  if (code != 200) { Serial.printf("[VOICE] %d\n", code); return; }
  DynamicJsonDocument r(4096);
  if (deserializeJson(r, resp)) return;
  if (!(r["addressed"] | true)) return;          // not talking to the robot
  Serial.printf("[VOICE] \"%s\"\n", (const char*)(r["transcript"] | ""));
  speak(String((const char*)(r["reply"] | "")));
}

// Called every loop: start listening when speech (or the button) begins.
void voiceLoop() {
#if !HAS_MIC
  return;
#endif
  static int16_t frame[FRAME_SAMPLES];
  int rms = readMicFrame(frame);
  if (pttPressed() || rms > VAD_START_RMS) listenAndAnswer(frame);
}

// ---------------- Heartbeat / motor bridge / commands ----------------
void sendHeartbeat() {
  StaticJsonDocument<128> doc;
  doc["firmware_version"] = FIRMWARE_VERSION;
  String body; serializeJson(doc, body);
  String resp;
  httpPostJson("/heartbeat/", body, resp);
}

void pollAndForwardMotorCommands() {
  String resp;
  if (httpGet("/motor/pending/", resp) != 200) return;
  StaticJsonDocument<2048> doc;
  if (deserializeJson(doc, resp)) return;
  for (JsonObject c : doc["commands"].as<JsonArray>()) {
    const char* frame = c["frame"];      // e.g. "<head:left:800>"
    if (frame) { megaSend(frame); Serial.printf("→Mega %s\n", frame); }
  }
}

// Dashboard / owner / enrollment commands for this board: speak them, ack.
void pollCommands() {
  String resp;
  if (httpGet("/commands/pending/", resp) != 200) return;
  DynamicJsonDocument doc(4096);
  if (deserializeJson(doc, resp)) return;
  for (JsonObject c : doc["commands"].as<JsonArray>()) {
    long id = c["command_id"] | 0;
    String kind = c["kind"] | "";
    bool ok = true;
    if (kind == "say" || kind == "page") {
      speak(String((const char*)(c["payload"]["text"] | "")));
    } else if (kind == "stream_start" || kind == "stream_stop") {
      // Camera cadence is driven by the server's /camera/frame/ reply.
    } else {
      ok = false;                         // not ours / unknown
    }
    StaticJsonDocument<96> ack; ack["command_id"] = id; ack["ok"] = ok ? 1 : 0;
    String ackBody; serializeJson(ack, ackBody);
    String r; httpPostJson("/commands/ack/", ackBody, r);
  }
}

// ---------------- Telemetry ----------------
int batteryPercent() {
#if !HAS_BATTERY_SENSE
  return -1;
#endif
  float v = analogReadMilliVolts(BATTERY_ADC) / 1000.0 * BATTERY_DIVIDER;
  if (v < 3.0) return -1;                         // no divider fitted
  int p = (int)((v - BATTERY_EMPTY_V) / (BATTERY_FULL_V - BATTERY_EMPTY_V) * 100);
  return constrain(p, 0, 100);
}

void sendTelemetry() {
  StaticJsonDocument<384> doc;
  int b = batteryPercent();
  if (b >= 0) doc["battery_percent"] = b;
  doc["cpu_temp"] = temperatureRead();
  doc["wifi_rssi"] = WiFi.RSSI();
  if (sdReady) doc["free_disk_mb"] = (int)((SD.totalBytes() - SD.usedBytes()) / (1024 * 1024));
  JsonObject cur = doc.createNestedObject("motor_current");
  for (int i = 0; i < 4; i++) {
    float amps = maxCurrent[i];           // plain copy: JSON can't take volatile
    if (amps > 0.05f) cur[MOTOR_NAMES[i]] = amps;
    maxCurrent[i] = 0;
  }
  String body; serializeJson(doc, body);
  String resp; httpPostJson("/telemetry/", body, resp);
}

// ---------------------------------------------------------------------------
// Offline resilience: keep working with no internet, sync when it returns
// ---------------------------------------------------------------------------
//   * While online, GET /sync/pull/ every 30 min → /catalog.json on SD
//     (parts, retail prices, stock, taught aliases).
//   * While offline, queueOfflineEvent() appends NDJSON lines to /queue.ndjson
//     with a unique client_uid.
//   * On reconnect, replayOfflineQueue() POSTs them in batches to /sync/push/;
//     the backend dedupes by client_uid, so a half-sent batch is safe to resend.
// Speech-to-text needs the server, so while offline the robot plays
// /offline.wav from the SD card instead of guessing.

uint32_t queueSeq = 0;

void cacheCatalogToSD() {
  if (!sdReady) return;
  HTTPClient http;
  http.begin(String(API_BASE) + "/sync/pull/");
  http.addHeader("X-Robot-Token", ROBOT_TOKEN);
  http.setTimeout(30000);
  if (http.GET() == 200) {
    File f = SD.open("/catalog.tmp", FILE_WRITE);
    if (f) {
      http.writeToStream(&f);
      f.close();
      SD.remove("/catalog.json");
      SD.rename("/catalog.tmp", "/catalog.json");
      Serial.println("[SYNC] catalog cached to SD");
    }
  }
  http.end();
}

void queueOfflineEvent(const String& kind, const String& payloadJson) {
  if (!sdReady) return;
  String uid = WiFi.macAddress() + "-" + String(millis()) + "-" + String(queueSeq++);
  uid.replace(":", "");
  File f = SD.open("/queue.ndjson", FILE_APPEND);
  if (!f) return;
  f.printf("{\"client_uid\":\"%s\",\"kind\":\"%s\",\"payload\":%s}\n",
           uid.c_str(), kind.c_str(), payloadJson.c_str());
  f.close();
}

bool postBatch(const String& events) {
  String resp;
  return httpPostJson("/sync/push/", "{\"events\":[" + events + "]}", resp) == 200;
}

void replayOfflineQueue() {
  if (!sdReady || !SD.exists("/queue.ndjson")) return;
  File f = SD.open("/queue.ndjson", FILE_READ);
  if (!f) return;
  String batch; int n = 0; bool allOk = true;
  while (f.available()) {
    String line = f.readStringUntil('\n'); line.trim();
    if (line.length() == 0) continue;
    batch += (n ? "," : "") + line; n++;
    if (n == 20) { allOk &= postBatch(batch); batch = ""; n = 0; }
  }
  if (n) allOk &= postBatch(batch);
  f.close();
  if (allOk) SD.remove("/queue.ndjson");   // otherwise retry next reconnect
  Serial.printf("[SYNC] offline queue replay %s\n", allOk ? "ok" : "partial — will retry");
}

// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  Serial2.begin(115200, SERIAL_8N1, MEGA_RX, MEGA_TX);
  megaLock = xSemaphoreCreateMutex();
  xTaskCreatePinnedToCore(megaTask, "mega", 4096, NULL, 2, NULL, 0);

  pinMode(PTT_BUTTON, INPUT_PULLUP);
  analogReadResolution(12);
#if HAS_SD
  sdReady = SD.begin(SD_CS);
#endif
  Serial.printf("[SD] %s\n", sdReady ? "ready" : "not found (offline cache off)");

#if HAS_MIC
  setupMic();
#endif
  setupAmp();
  connectWifi();
  if (WiFi.status() == WL_CONNECTED) {
    sendHeartbeat();
    cacheCatalogToSD();
    lastCatalogSync = millis();
  }
  Serial.println("[ESP32] bridge ready");
}

void loop() {
  unsigned long now = millis();
  bool online = (WiFi.status() == WL_CONNECTED);

  if (!online) {
    // The Mega keep-alive task keeps running; commands simply stop arriving.
    static unsigned long lastRetry = 0;
    if (now - lastRetry > 5000) { WiFi.reconnect(); lastRetry = now; }
    wasOnline = false;
    voiceLoop();                          // still hears people (plays the offline clip)
    return;
  }

  // Just came back online → replay everything we did offline, refresh cache.
  if (!wasOnline) {
    replayOfflineQueue();
    cacheCatalogToSD();
    lastCatalogSync = now;
    wasOnline = true;
  }

  if (now - lastHeartbeat > 30000)       { sendHeartbeat();                 lastHeartbeat = now; }
  if (now - lastMotorPoll > 500)         { pollAndForwardMotorCommands();   lastMotorPoll = now; }
  if (now - lastCmdPoll   > 800)         { pollCommands();                  lastCmdPoll   = now; }
  if (now - lastTelemetry > 15000)       { sendTelemetry();                 lastTelemetry = now; }
  if (now - lastCatalogSync > 1800000UL) { cacheCatalogToSD();              lastCatalogSync = now; }

  voiceLoop();
}
