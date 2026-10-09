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

#include <ArduinoJson.h>
// The current I2S driver. The legacy <driver/i2s.h> also links the legacy ADC
// driver, and arduino-esp32 3.x (whose analogRead uses the new ADC driver)
// aborts at boot with "ADC: CONFLICT! driver_ng is not allowed to be used with
// the legacy driver".
#include <driver/i2s_std.h>
#include <driver/dac_continuous.h>
#include <esp_system.h>
#include <Preferences.h>
#include <SPI.h>
#include <SD.h>

// ---------------- Configuration ----------------
const char* WIFI_SSID   = "YOUR_WIFI";
const char* WIFI_PASS   = "YOUR_PASS";
const char* API_BASE    = "http://192.168.1.20:8000/api/robot/v1";  // laptop/server
const char* ROBOT_TOKEN = "PASTE_DEVICE_TOKEN_FROM_ADMIN";           // printed once by create_robot_device
const char* FIRMWARE_VERSION = "2.1.8";

// ---- Fitted hardware (match what's on YOUR robot) ----
#define HAS_MIC            1   // INMP441 — needed for voice. Set 0 until it's
                               // wired: an unconnected data pin reads noise
                               // that the VAD would keep uploading.
#define HAS_SD             0   // microSD module (offline catalog/queue/clip)
#define HAS_BATTERY_SENSE  0   // 12V divider on GPIO34 (else battery isn't reported)
#define BOOT_CHIME         1   // 3 beeps (low, mid, high) at power-up: proves the
                               // amp + speaker work without the server. A car
                               // tweeter plays only the high one.
#define AUDIO_OUT_DAC      0   // 1 = no MAX98357A board: line-level audio comes
                               // out of GPIO25 (the ESP32's own DAC) into a car
                               // radio's AUX or a car amplifier's input, through
                               // a 10 µF capacitor (+ toward GPIO25). Ground to
                               // the radio/amp ground. Phone earphones on the
                               // same two wires work for a quick test.
#define DAC_GAIN         1.0f  // fixed gain on the DAC line out. Speech is
                               // normalized per clip instead (dacNormalize):
                               // 3x drove gTTS, already near full scale, deep
                               // into the limiter, and it came out as crackle.

// ---- Serial link to Arduino Mega (UART2) ----
// ESP32 GPIO17 = TX2 → Mega RX1(19);  ESP32 GPIO16 = RX2 ← Mega TX1(18)
// ⚠️ Mega TX is 5V: put a divider (1k/2k) before ESP32 GPIO16.
#define MEGA_TX 17
#define MEGA_RX 16

// ---- INMP441 I2S microphone (I2S port 0) ----
#define I2S_MIC_SCK   14   // BCLK
#define I2S_MIC_WS    15   // LRCLK / WS
#define I2S_MIC_SD    32   // DATA out of mic

// ---- MAX98357A I2S amplifier (I2S port 1; unused when AUDIO_OUT_DAC) ----
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
// TX power: 13 dBm reaches a router across the shop, but on a weak 5V supply
// the radio's current spikes brown the board out mid-connect. After one
// brownout the board remembers it (NVS) and stays at 8.5 dBm, so it never
// boot-loops; typing "w" in the Serial Monitor clears that once the supply
// is fixed (a big capacitor on VIN/GND, a short thick 5V lead).
bool wifiLowPower() {
  Preferences prefs;
  prefs.begin("bridge", false);
  bool low = prefs.getBool("txlow", false);
  if (!low && esp_reset_reason() == ESP_RST_BROWNOUT) {
    low = true;
    prefs.putBool("txlow", true);
  }
  prefs.end();
  return low;
}

void connectWifi() {
  bool low = wifiLowPower();
  // Printed before the radio starts: if the log ends here, the board died
  // (brownout) while switching the radio on, not while connecting.
  Serial.printf("WiFi \"%s\" (TX %s)", WIFI_SSID,
                low ? "8.5 dBm: low, a brownout happened before" : "13 dBm");
  WiFi.mode(WIFI_STA);
  // Set before begin() so the first probe goes out at this power too.
  WiFi.setTxPower(low ? WIFI_POWER_8_5dBm : WIFI_POWER_13dBm);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) { delay(400); Serial.print("."); }
  if (WiFi.status() != WL_CONNECTED) { Serial.println(" offline"); return; }
  // Signal: -50 is excellent, -70 fair, below -80 too weak for HTTPS.
  Serial.printf(" %s (signal %d dBm)\n", WiFi.localIP().toString().c_str(), (int) WiFi.RSSI());
}

// ---------------- HTTP helpers ----------------
// ONE connection to the backend, kept open and reused by every request. A new
// TLS handshake costs ~1 s and ~40 KB of RAM on the ESP32; doing that for the
// twice-a-second polls would leave the loop no time to listen to the mic
// (the start of "يا موس" would be cut off). All HTTP runs on the loop task,
// so a single shared client is safe. The certificate isn't pinned — the
// device token is what authenticates the robot.
WiFiClientSecure apiTls;
HTTPClient api;

void apiBegin(const String& path, uint16_t timeoutMs) {
  String url = String(API_BASE) + path;
  api.setReuse(true);
  api.setConnectTimeout(8000);
  // The core waits up to 120 s for a TLS handshake; on a weak link that froze
  // the whole loop (no commands, no voice). Give up after 15 s and retry later.
  apiTls.setHandshakeTimeout(15);
  if (url.startsWith("https://")) { apiTls.setInsecure(); api.begin(apiTls, url); }
  else api.begin(url);
  api.addHeader("X-Robot-Token", ROBOT_TOKEN);
  api.setTimeout(timeoutMs);
}

// Finish a request. `clean` = the whole response was read; otherwise the
// connection is in an unknown state, so drop it and the next request
// reconnects.
void apiEnd(bool clean) {
  api.end();
  if (!clean) apiTls.stop();
}

int httpPostJson(const String& path, const String& body, String& out) {
  apiBegin(path, 15000);
  api.addHeader("Content-Type", "application/json");
  int code = api.POST(body);
  out = (code > 0) ? api.getString() : "";
  apiEnd(code > 0);
  return code;
}

int httpGet(const String& path, String& out) {
  apiBegin(path, 10000);
  int code = api.GET();
  out = (code > 0) ? api.getString() : "";
  apiEnd(code > 0);
  return code;
}

// ---------------- Audio: INMP441 mic ----------------
i2s_chan_handle_t micRx = NULL, ampTx = NULL;
dac_continuous_handle_t dacOut = NULL;

// The DAC's DMA runs on I2S0, so in DAC mode the mic moves to I2S1 (free,
// since the MAX98357A isn't used then).
#if AUDIO_OUT_DAC
#define MIC_I2S_PORT I2S_NUM_1
#else
#define MIC_I2S_PORT I2S_NUM_0
#endif

// INMP441 with L/R tied to GND talks on the LEFT slot. If the VAD never
// triggers (RMS stays ~0), set this to I2S_STD_SLOT_RIGHT.
#define MIC_SLOT I2S_STD_SLOT_LEFT

void setupMic() {
  i2s_chan_config_t chan = I2S_CHANNEL_DEFAULT_CONFIG(MIC_I2S_PORT, I2S_ROLE_MASTER);
  chan.dma_desc_num = 6;
  chan.dma_frame_num = FRAME_SAMPLES;
  if (i2s_new_channel(&chan, NULL, &micRx) != ESP_OK) {
    Serial.println("[MIC] I2S channel failed");
    micRx = NULL;
    return;
  }
  i2s_std_config_t std = {
    .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
    .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO),
    .gpio_cfg = {
      .mclk = I2S_GPIO_UNUSED,     // never drive GPIO0 (the BOOT pin)
      .bclk = (gpio_num_t) I2S_MIC_SCK,
      .ws = (gpio_num_t) I2S_MIC_WS,
      .dout = I2S_GPIO_UNUSED,
      .din = (gpio_num_t) I2S_MIC_SD,
      .invert_flags = {.mclk_inv = false, .bclk_inv = false, .ws_inv = false},
    },
  };
  std.slot_cfg.slot_mask = MIC_SLOT;
  esp_err_t err = i2s_channel_init_std_mode(micRx, &std);
  if (err == ESP_OK) err = i2s_channel_enable(micRx);
  if (err != ESP_OK) {
    Serial.printf("[MIC] I2S setup failed: %s\n", esp_err_to_name(err));
    i2s_del_channel(micRx);
    micRx = NULL;
    return;
  }
  Serial.println("[MIC] ready (SCK 14, WS 15, SD 32)");
}

// Read one 32 ms frame as 16-bit PCM into `out`; returns its RMS level.
int32_t micFrame[FRAME_SAMPLES];
int readMicFrame(int16_t* out) {
  size_t got = 0;
  if (micRx) i2s_channel_read(micRx, micFrame, sizeof(micFrame), &got, 100);
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

// ---------------- Audio out ----------------
// Line-level audio on GPIO25 from the ESP32's 8-bit DAC, for a car radio's
// AUX or a car amplifier's input (AUDIO_OUT_DAC 1).
void setupDac() {
  dac_continuous_config_t cfg = {
    .chan_mask = DAC_CHANNEL_MASK_CH0,            // CH0 = GPIO25
    .desc_num = 8,
    .buf_size = 1024,
    .freq_hz = SAMPLE_RATE,
    .offset = 0,
    .clk_src = DAC_DIGI_CLK_SRC_APLL,             // the default clock can't go down to 16 kHz
    .chan_mode = DAC_CHANNEL_MODE_SIMUL,
  };
  esp_err_t err = dac_continuous_new_channels(&cfg, &dacOut);
  if (err == ESP_OK) err = dac_continuous_enable(dacOut);
  if (err != ESP_OK) {
    Serial.printf("[AMP] DAC setup failed: %s\n", esp_err_to_name(err));
    if (dacOut) dac_continuous_del_channels(dacOut);
    dacOut = NULL;
    return;
  }
  Serial.println("[AMP] ready (DAC line out on GPIO25)");
}

// MAX98357A board on I2S1 (AUDIO_OUT_DAC 0).
void setupAmp() {
#if AUDIO_OUT_DAC
  setupDac();
  return;
#endif
  i2s_chan_config_t chan = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
  chan.dma_desc_num = 8;
  chan.dma_frame_num = 256;
  chan.auto_clear = true;          // silence (not the last sound) when idle
  if (i2s_new_channel(&chan, &ampTx, NULL) != ESP_OK) {
    Serial.println("[AMP] I2S channel failed");
    ampTx = NULL;
    return;
  }
  i2s_std_config_t std = {
    .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
    .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
    .gpio_cfg = {
      .mclk = I2S_GPIO_UNUSED,
      .bclk = (gpio_num_t) I2S_AMP_BCLK,
      .ws = (gpio_num_t) I2S_AMP_LRC,
      .dout = (gpio_num_t) I2S_AMP_DIN,
      .din = I2S_GPIO_UNUSED,
      .invert_flags = {.mclk_inv = false, .bclk_inv = false, .ws_inv = false},
    },
  };
  // Same samples on both slots: the MAX98357A plays left, right or their
  // mix depending on its SD pin, so it gets the audio either way.
  std.slot_cfg.slot_mask = I2S_STD_SLOT_BOTH;
  esp_err_t err = i2s_channel_init_std_mode(ampTx, &std);
  if (err == ESP_OK) err = i2s_channel_enable(ampTx);
  if (err != ESP_OK) {
    Serial.printf("[AMP] I2S setup failed: %s\n", esp_err_to_name(err));
    i2s_del_channel(ampTx);
    ampTx = NULL;
    return;
  }
  Serial.println("[AMP] ready (BCLK 26, LRC 25, DIN 22)");
}

// 16-bit sample → 8-bit DAC code, boosted by DAC_GAIN with a soft knee so
// loud syllables round off instead of cracking.
uint8_t dacSample(int16_t pcm) {
  float x = pcm * (DAC_GAIN / 32768.0f);         // 1.0 = DAC full scale
  if (x > 0.6f)       x =  0.6f + 0.4f * tanhf((x - 0.6f) / 0.4f);
  else if (x < -0.6f) x = -0.6f - 0.4f * tanhf((-x - 0.6f) / 0.4f);
  return (uint8_t) constrain((int) lrintf(x * 127.0f) + 128, 1, 255);
}

// A network read can end mid-sample (odd byte count); the DAC path keeps that
// byte for the next call so the 16-bit samples never slip out of alignment.
uint8_t dacCarry;
bool dacHasCarry = false;

// 16-bit little-endian PCM bytes → DAC codes in `out`; returns how many.
// `out` must hold (n + 1) / 2 codes.
size_t dacConvert(const uint8_t* buf, size_t n, uint8_t* out) {
  size_t k = 0, i = 0;
  if (dacHasCarry && n) {
    out[k++] = dacSample((int16_t) (dacCarry | (buf[0] << 8)));
    dacHasCarry = false;
    i = 1;
  }
  for (; i + 1 < n; i += 2) out[k++] = dacSample((int16_t) (buf[i] | (buf[i + 1] << 8)));
  if (i < n) { dacCarry = buf[i]; dacHasCarry = true; }
  return k;
}

// Scale buffered DAC codes so the loudest one reaches ~85 % of full swing
// (never turned down, at most 4x): clips differ in level, and the quiet ones
// get lost in the 8-bit DAC. k <= 0 measures it; pass the result back to
// keep the same level across the buffers of one long clip.
float dacNormalize(uint8_t* codes, size_t n, float k) {
  if (k <= 0) {
    int peak = 1;
    for (size_t i = 0; i < n; i++) peak = max(peak, abs((int) codes[i] - 128));
    k = constrain(108.0f / peak, 1.0f, 4.0f);
  }
  if (k > 1.0f) {
    for (size_t i = 0; i < n; i++)
      codes[i] = (uint8_t) constrain((int) lrintf(128 + (codes[i] - 128) * k), 1, 255);
  }
  return k;
}

// Play 16-bit mono PCM through the amp (or the DAC, as 8-bit unsigned).
void ampWrite(const uint8_t* buf, size_t n) {
  size_t written;
  if (dacOut) {
    uint8_t out[257];
    for (size_t off = 0; off < n; ) {
      size_t chunk = min(n - off, (size_t) 512);
      size_t k = dacConvert(buf + off, chunk, out);
      if (k) dac_continuous_write(dacOut, out, k, &written, -1);
      off += chunk;
    }
    return;
  }
  if (ampTx) i2s_channel_write(ampTx, buf, n, &written, portMAX_DELAY);
}

// A sine tone generated on the board — no server, no Wi-Fi involved.
void ampTone(int freq, int ms) {
  int16_t buf[256];
  const int total = SAMPLE_RATE * ms / 1000;
  // The DAC line out needs close to full swing for a car amp to be heard
  // (DAC_GAIN multiplies it back up); the MAX98357A is loud at 9000 already.
  const float level = dacOut ? 0.9f * 32767 / DAC_GAIN : 9000;
  for (int i = 0; i < total; ) {
    int k = min(256, total - i);
    for (int j = 0; j < k; j++, i++) {
      buf[j] = (int16_t) (level * sinf(2.0f * PI * freq * i / SAMPLE_RATE));
    }
    ampWrite((const uint8_t*) buf, k * 2);
  }
}

// Read exactly n bytes from the HTTP stream (false on timeout/close).
bool readExact(NetworkClient* s, uint8_t* dst, size_t n) {
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

  apiBegin("/speak/", 20000);
  api.addHeader("Content-Type", "application/json");
  int code = api.POST(body);
  if (code != 200) {
    // -1: no connection to the server (Wi-Fi too weak or the server down);
    // 204: the server has no TTS right now.
    Serial.printf("[AMP] /speak/ → %d, nothing to play\n", code);
    apiEnd(code > 0 && api.getSize() == 0);
    return;
  }
  NetworkClient* s = api.getStreamPtr();
  // The connection stays open (keep-alive), so the end of the audio is known
  // only from Content-Length — not from the server closing the socket.
  int32_t left = api.getSize();                    // -1 = unknown

  uint8_t hdr[12];
  if (!readExact(s, hdr, 12) || memcmp(hdr, "RIFF", 4) || memcmp(hdr + 8, "WAVE", 4)) {
    Serial.println("[AMP] reply is not a WAV file");
    apiEnd(false);
    return;
  }
  if (left > 0) left -= 12;
  uint8_t ch[8];
  bool found = false;
  while (readExact(s, ch, 8)) {                    // walk chunks to "data"
    if (left > 0) left -= 8;
    uint32_t len = ch[4] | (ch[5] << 8) | (ch[6] << 16) | ((uint32_t) ch[7] << 24);
    if (!memcmp(ch, "data", 4)) { found = true; break; }
    for (uint32_t skip = 0; skip < len; skip++) { uint8_t b; if (!readExact(s, &b, 1)) { apiEnd(false); return; } }
    if (left > 0) left -= len;
  }
  if (!found) { Serial.println("[AMP] WAV has no data chunk"); apiEnd(false); return; }
  dacHasCarry = false;
  uint8_t buf[1024];
  uint32_t played = 0;
  unsigned long t0 = millis(), tStart = millis();

  // Speech plays at 32 KB/s; weak Wi-Fi can deliver less than that, and
  // streamed straight to the DAC every stall came out as a click. On the DAC
  // the clip is downloaded first (as 8-bit codes, half the RAM) and played
  // in one go; a clip longer than the buffer plays a buffer at a time.
  uint8_t* clip = NULL;
  size_t clipCap = 0, clipLen = 0, written;
  float clipK = 0;                                   // set by the first dacNormalize
  if (dacOut) {
    clipCap = (left > 0) ? (size_t) left / 2 + 1 : 96000;
    clipCap = constrain(clipCap, (size_t) 1024, (size_t) 96000);   // ≥ 2 reads' worth
    while (!(clip = (uint8_t*) malloc(clipCap)) && clipCap >= 8192) clipCap /= 2;
  }

  while (left != 0 && (s->connected() || s->available())) {
    int a = s->available();
    if (a <= 0) { if (millis() - t0 > 3000) break; delay(2); continue; }
    int want = min(a, (int) sizeof(buf));
    if (left > 0 && want > left) want = left;
    int n = s->readBytes(buf, want);
    if (left > 0) left -= n;
    if (clip) {
      if (clipCap - clipLen < sizeof(buf) / 2 + 1 && clipLen) {   // full: play it
        clipK = dacNormalize(clip, clipLen, clipK);
        dac_continuous_write(dacOut, clip, clipLen, &written, -1);
        clipLen = 0;
      }
      clipLen += dacConvert(buf, n, clip + clipLen);
    } else {
      ampWrite(buf, n);
    }
    played += n;
    t0 = millis();
  }
  float fetched = (millis() - tStart) / 1000.0f;
  if (clip) {
    if (clipLen) {
      clipK = dacNormalize(clip, clipLen, clipK);
      dac_continuous_write(dacOut, clip, clipLen, &written, -1);
    }
    free(clip);
  }
  // If fetching took longer than the audio lasts, the link is slower than
  // real time: streaming would stutter, which is why the DAC buffers.
  Serial.printf("[AMP] played %lu bytes (%.1f s of audio, fetched in %.1f s)\n",
                (unsigned long) played, played / (2.0f * SAMPLE_RATE), fetched);
  apiEnd(left == 0);
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
  dacHasCarry = false;
  while ((n = f.read(buf, sizeof(buf))) > 0) {
    ampWrite(buf, n);
  }
  f.close();
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
  // Without PSRAM the biggest free RAM block is ~100 KB, so a 4 s clip
  // (128 KB) may not fit: fall back to shorter clips instead of going deaf.
  size_t maxPcm = (size_t) SAMPLE_RATE * 2 * MAX_RECORD_MS / 1000;
  uint8_t* buf = NULL;
  while (!buf && maxPcm >= (size_t) SAMPLE_RATE * 2) {     // down to 1 s
    buf = (uint8_t*) malloc(head.length() + 44 + maxPcm + tail.length());
    if (!buf) maxPcm -= SAMPLE_RATE;                        // −0.5 s
  }
  if (!buf) { Serial.printf("[VOICE] no memory (largest block %lu)\n", (unsigned long) ESP.getMaxAllocHeap()); return; }

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

  apiBegin("/voice/", 20000);
  api.addHeader("Content-Type", String("multipart/form-data; boundary=") + BOUNDARY);
  BufStream bs(buf, sendLen);
  int code = api.sendRequest("POST", &bs, sendLen);
  String resp = (code > 0) ? api.getString() : "";
  apiEnd(code > 0);
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
  int code = httpPostJson("/heartbeat/", body, resp);
  Serial.printf("[HB] /heartbeat/ → %d%s\n", code, code == 200 ? " (online)" :
                code == 401 ? " — wrong ROBOT_TOKEN or API_BASE workshop" : "");
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
  apiBegin("/sync/pull/", 30000);
  int code = api.GET();
  bool clean = false;
  if (code == 200) {
    File f = SD.open("/catalog.tmp", FILE_WRITE);
    if (f) {
      clean = api.writeToStream(&f) > 0;
      f.close();
      if (clean) {                            // keep the old copy on a failed download
        SD.remove("/catalog.json");
        SD.rename("/catalog.tmp", "/catalog.json");
        Serial.println("[SYNC] catalog cached to SD");
      }
    }
  } else {
    clean = code > 0 && api.getSize() == 0;
  }
  apiEnd(clean);
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

// Why the board last restarted. The ROM banner says only "SW_RESET" for a
// crash, a brownout and a normal restart alike; ESP-IDF keeps the real cause.
const char* lastResetReason() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:   return "power on";
    case ESP_RST_EXT:       return "reset pin";
    case ESP_RST_SW:        return "software restart";
    case ESP_RST_PANIC:     return "CRASH (panic)";
    case ESP_RST_INT_WDT:
    case ESP_RST_TASK_WDT:
    case ESP_RST_WDT:       return "WATCHDOG (something hung)";
    case ESP_RST_BROWNOUT:  return "BROWNOUT (5V supply dropped)";
    case ESP_RST_DEEPSLEEP: return "deep sleep wake";
    default:                return "unknown";
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("\n[BOOT] Mouss Tec bridge %s\n", FIRMWARE_VERSION);
  Serial.printf("[BOOT] last reset: %s\n", lastResetReason());
  Serial2.begin(115200, SERIAL_8N1, MEGA_RX, MEGA_TX);
  megaLock = xSemaphoreCreateMutex();
  xTaskCreatePinnedToCore(megaTask, "mega", 4096, NULL, 2, NULL, 0);
  Serial.println("[BOOT] mega link task started");

  pinMode(PTT_BUTTON, INPUT_PULLUP);
  analogReadResolution(12);
#if HAS_SD
  sdReady = SD.begin(SD_CS);
#endif
  Serial.printf("[SD] %s\n", sdReady ? "ready" : "not found (offline cache off)");

#if HAS_MIC
  Serial.println("[BOOT] mic...");
  setupMic();
#endif
  Serial.println("[BOOT] audio out...");
  setupAmp();
#if BOOT_CHIME
  Serial.println("[BOOT] chime");
  ampTone(500, 180);  delay(80);   // low
  ampTone(1500, 180); delay(80);   // mid
  ampTone(4000, 180);              // high (a tweeter plays only this one)
#endif
  connectWifi();
  Serial.printf("[MEM] free %lu, largest block %lu\n",
                (unsigned long) ESP.getFreeHeap(), (unsigned long) ESP.getMaxAllocHeap());
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("[HB] contacting the server...");
    sendHeartbeat();
    cacheCatalogToSD();
    lastCatalogSync = millis();
  }
  Serial.println("[ESP32] bridge ready — type t for a test tone, s <text> to speak");
}

// Bench tests typed in the Serial Monitor (115200, "New Line"):
//   t          → 2 s test tone (no server involved)
//   s <text>   → fetch <text> from /speak/ and play it
//   w          → forget the brownout, retry 13 dBm Wi-Fi (restarts)
void serialCommands() {
  if (!Serial.available()) return;
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line == "t") {
    Serial.println("[TEST] 1 kHz tone, 2 s");
    ampTone(1000, 2000);
  } else if (line.startsWith("s ")) {
    speak(line.substring(2));
  } else if (line == "w") {
    Preferences prefs;
    prefs.begin("bridge", false);
    prefs.putBool("txlow", false);
    prefs.end();
    Serial.println("[TEST] Wi-Fi back to 13 dBm, restarting");
    delay(200);
    ESP.restart();
  } else if (line.length()) {
    Serial.println("[TEST] type t (tone), s <text> (speak) or w (Wi-Fi power)");
  }
}

void loop() {
  serialCommands();
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
