/*
 * arduino_mega_motor_control.ino
 * ------------------------------------------------------------------
 * Mouss Tec Robot — Arduino Mega 2560 motor/relay controller.
 *
 * The Mega owns all HIGH-AMP motor logic. It never talks to Wi-Fi; it only
 * receives compact serial frames from the ESP32 and drives an 8-channel 5V
 * relay board that switches the recycled BMW/MINI motors:
 *
 *   Head   : 1x power-window motor  → pan LEFT / RIGHT
 *   Arms   : 2x power-window motors → lift UP / DOWN, self-locking
 *   Tracks : 2x wiper motors        → FORWARD / BACKWARD / TURN
 *
 * Each DC motor uses TWO relays (an H-bridge made of relays) so it can run in
 * both directions. 3 motors that reverse (head + 2 arms) would need 6 relays;
 * the 2 track motors share direction via 2 relays here for an 8-relay budget:
 *
 *   Relay CH1/CH2  → Head motor      (CH1=left coil, CH2=right coil)
 *   Relay CH3/CH4  → Arm LEFT motor  (CH3=up,   CH4=down)
 *   Relay CH5/CH6  → Arm RIGHT motor (CH5=up,   CH6=down)
 *   Relay CH7/CH8  → Track drive     (CH7=forward bus, CH8=backward bus)
 *
 * Turning is done by pulsing one track bus (skid-steer); for independent
 * tracks move to a 2-relay-per-track wiring and expand the map below.
 *
 * SERIAL PROTOCOL (from ESP32 on Serial1 @ 115200):
 *   <ACTUATOR:DIRECTION:DURATION_MS>
 *   e.g. <head:left:800>  <arm_left:up:0>  <track:forward:1500>  <arm_left:stop:0>
 *   Every move is timed (1..5000 ms). A move sent with 0 gets DEFAULT_PULSE_MS:
 *   the worm-gear arms already HOLD position with no power, so there is no need
 *   to keep a relay on — and "run until stop" would stall a window motor at its
 *   end stop (burning motor + relay) or keep the tracks driving if the stop
 *   frame is lost.
 *   <ping:0:0> is a keep-alive the ESP32 sends every second; it moves nothing.
 *
 * SAFETY:
 *   - Only ONE direction relay per motor is ever energized at a time
 *     (the opposite coil is forced OFF first) — prevents dead-shorts.
 *   - A global watchdog kills all relays if no valid frame (the ESP32's 1s
 *     keep-alive included) arrives for WATCHDOG_MS (comms lost → stop moving).
 *   - Every channel has its OWN timer, so the head and an arm moving at the
 *     same time each stop on schedule.
 *   - Current sensing (ACS712 per motor on A0..A3): a stall-level current
 *     cuts that motor immediately (jammed arm / end stop), and readings are
 *     reported back to the ESP32 as <cur:NAME:AMPS> for predictive
 *     maintenance (<fault:NAME:AMPS> when it cut a motor).
 *   - Relay board is ACTIVE-LOW (most 5V boards): LOW = energized.
 * ------------------------------------------------------------------
 */

// ---- Relay channel → Mega digital pins (active-LOW board) ----
const uint8_t CH[8] = {22, 23, 24, 25, 26, 27, 28, 29};

// Named channel indices (0-based into CH[]).
enum {
  HEAD_LEFT = 0, HEAD_RIGHT = 1,
  ARM_L_UP = 2,  ARM_L_DOWN = 3,
  ARM_R_UP = 4,  ARM_R_DOWN = 5,
  TRACK_FWD = 6, TRACK_BWD = 7,
};

const bool ACTIVE_LOW = true;                 // typical 5V relay module
const unsigned long WATCHDOG_MS = 3000;       // stop if silent this long
const unsigned long DEFAULT_PULSE_CAP = 5000; // never run a motor > 5s per move
const unsigned long DEFAULT_PULSE_MS = 500;   // a move sent with 0 ms

unsigned long lastFrameAt = 0;

// Per-channel move timers: when each energized channel must switch off
// (0 = channel idle). One shared timer would forget the head's deadline the
// moment an arm started moving, leaving the head running until the watchdog.
unsigned long offAt[8] = {0, 0, 0, 0, 0, 0, 0, 0};

// ---- Current sensing (ACS712-20A: 100 mV/A, 2.5 V at 0 A) ----
// One sensor per motor, in series with its common lead. Unfitted sensors read
// ~0 A and are simply ignored.
const uint8_t CUR_PIN[4] = {A0, A1, A2, A3};           // head, arm_l, arm_r, track
const char* CUR_NAME[4] = {"head", "arm_left", "arm_right", "track"};
const uint8_t CUR_PAIR[4][2] = {{HEAD_LEFT, HEAD_RIGHT}, {ARM_L_UP, ARM_L_DOWN},
                                {ARM_R_UP, ARM_R_DOWN}, {TRACK_FWD, TRACK_BWD}};
const float CUR_LIMIT_A[4] = {8.0, 10.0, 10.0, 15.0};  // stall → cut (match the backend)
const float ACS_MV_PER_A = 100.0;
int curZero[4] = {512, 512, 512, 512};                 // calibrated at boot
unsigned long lastCurCheck = 0, lastCurReport = 0;
// A DC motor draws several times its running current for the first moment
// (inrush); ignore that window so every start isn't mistaken for a stall.
const unsigned long INRUSH_BLANK_MS = 250;
unsigned long runSince[4] = {0, 0, 0, 0};

void relayWrite(uint8_t idx, bool on) {
  digitalWrite(CH[idx], (on == ACTIVE_LOW) ? LOW : HIGH);
}

void allOff() {
  for (uint8_t i = 0; i < 8; i++) { relayWrite(i, false); offAt[i] = 0; }
}

void setup() {
  for (uint8_t i = 0; i < 8; i++) {
    pinMode(CH[i], OUTPUT);
    relayWrite(i, false);           // start safe: everything off
  }
  Serial.begin(115200);             // USB debug
  Serial1.begin(115200);            // link to ESP32 (pins 19 RX1 / 18 TX1)
  // Calibrate each current sensor's zero while every motor is off.
  for (uint8_t m = 0; m < 4; m++) {
    long acc = 0;
    for (int i = 0; i < 32; i++) { acc += analogRead(CUR_PIN[m]); delay(1); }
    curZero[m] = acc / 32;
  }
  lastFrameAt = millis();
  Serial.println(F("[MEGA] motor controller ready"));
}

// Energize exactly one channel of a motor pair, forcing the sibling OFF first.
void driveExclusive(int onCh, int offCh, unsigned long durationMs) {
  relayWrite(offCh, false);         // kill opposite coil (no shoot-through)
  offAt[offCh] = 0;
  if (durationMs == 0) durationMs = DEFAULT_PULSE_MS;
  relayWrite(onCh, true);
  offAt[onCh] = millis() + min(durationMs, DEFAULT_PULSE_CAP);
  if (offAt[onCh] == 0) offAt[onCh] = 1;  // 0 means idle; dodge millis() wrap
}

void stopPair(int a, int b) {
  relayWrite(a, false);
  relayWrite(b, false);
  offAt[a] = 0;
  offAt[b] = 0;
}

void handleCommand(const String& actuator, const String& dir, unsigned long ms) {
  if (actuator == "head") {
    if (dir == "left")       driveExclusive(HEAD_LEFT, HEAD_RIGHT, ms);
    else if (dir == "right") driveExclusive(HEAD_RIGHT, HEAD_LEFT, ms);
    else                     stopPair(HEAD_LEFT, HEAD_RIGHT);
  } else if (actuator == "arm_left") {
    if (dir == "up")         driveExclusive(ARM_L_UP, ARM_L_DOWN, ms);
    else if (dir == "down")  driveExclusive(ARM_L_DOWN, ARM_L_UP, ms);
    else                     stopPair(ARM_L_UP, ARM_L_DOWN);
  } else if (actuator == "arm_right") {
    if (dir == "up")         driveExclusive(ARM_R_UP, ARM_R_DOWN, ms);
    else if (dir == "down")  driveExclusive(ARM_R_DOWN, ARM_R_UP, ms);
    else                     stopPair(ARM_R_UP, ARM_R_DOWN);
  } else if (actuator == "track") {
    if (dir == "forward")    driveExclusive(TRACK_FWD, TRACK_BWD, ms);
    else if (dir == "backward") driveExclusive(TRACK_BWD, TRACK_FWD, ms);
    else                     stopPair(TRACK_FWD, TRACK_BWD);
  } else if (actuator == "all" && dir == "stop") {
    allOff();
  }
}

bool motorRunning(uint8_t m) {
  return offAt[CUR_PAIR[m][0]] != 0 || offAt[CUR_PAIR[m][1]] != 0;
}

float readAmps(uint8_t m) {
  long acc = 0;
  for (int i = 0; i < 8; i++) acc += analogRead(CUR_PIN[m]);
  float mv = ((acc / 8.0) - curZero[m]) * 5000.0 / 1023.0;
  return fabs(mv) / ACS_MV_PER_A;
}

// Every 50 ms: cut a stalled motor at once; every 1 s: report running motors.
void checkCurrents() {
  unsigned long now = millis();
  if (now - lastCurCheck < 50) return;
  lastCurCheck = now;
  bool report = (now - lastCurReport >= 1000);
  if (report) lastCurReport = now;
  for (uint8_t m = 0; m < 4; m++) {
    if (!motorRunning(m)) { runSince[m] = 0; continue; }
    if (runSince[m] == 0) runSince[m] = now;
    if (now - runSince[m] < INRUSH_BLANK_MS) continue;
    float a = readAmps(m);
    if (a >= CUR_LIMIT_A[m]) {
      stopPair(CUR_PAIR[m][0], CUR_PAIR[m][1]);
      Serial1.print('<'); Serial1.print("fault:"); Serial1.print(CUR_NAME[m]);
      Serial1.print(':'); Serial1.print(a, 2); Serial1.print('>');
    } else if (report) {
      Serial1.print('<'); Serial1.print("cur:"); Serial1.print(CUR_NAME[m]);
      Serial1.print(':'); Serial1.print(a, 2); Serial1.print('>');
    }
  }
}

// Parse "<actuator:direction:ms>" out of the serial stream.
void parseFrame(const String& frame) {
  int p1 = frame.indexOf(':');
  int p2 = frame.indexOf(':', p1 + 1);
  if (p1 < 0 || p2 < 0) return;
  String actuator = frame.substring(0, p1);
  String dir = frame.substring(p1 + 1, p2);
  unsigned long ms = (unsigned long) frame.substring(p2 + 1).toInt();
  handleCommand(actuator, dir, ms);
  lastFrameAt = millis();
}

String buf;

void readSerial(Stream& s) {
  while (s.available()) {
    char c = (char) s.read();
    if (c == '<') { buf = ""; }
    else if (c == '>') { parseFrame(buf); buf = ""; }
    else { buf += c; if (buf.length() > 48) buf = ""; }  // overflow guard
  }
}

void loop() {
  readSerial(Serial1);   // commands from ESP32
  readSerial(Serial);    // allow manual testing over USB

  checkCurrents();

  // Timed-move expiry, per channel. Signed difference so it survives the
  // ~49-day millis() rollover.
  unsigned long now = millis();
  for (uint8_t i = 0; i < 8; i++) {
    if (offAt[i] != 0 && (long)(now - offAt[i]) >= 0) {
      relayWrite(i, false);
      offAt[i] = 0;
    }
  }

  // Watchdog: comms silent (not even the 1s keep-alive) → stop everything.
  if (millis() - lastFrameAt > WATCHDOG_MS) {
    allOff();
    lastFrameAt = millis();  // re-arm so we don't spam
  }
}
