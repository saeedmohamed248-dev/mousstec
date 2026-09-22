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
 *   DURATION_MS = 0 means latch until an explicit :stop (used for self-locking
 *   arms — the worm-gear window motor holds position with no power).
 *
 * SAFETY:
 *   - Only ONE direction relay per motor is ever energized at a time
 *     (the opposite coil is forced OFF first) — prevents dead-shorts.
 *   - A global watchdog kills all relays if no valid frame arrives for
 *     WATCHDOG_MS (comms lost → stop moving).
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
const unsigned long DEFAULT_PULSE_CAP = 5000; // never latch a motor > 5s on a timed move

unsigned long lastFrameAt = 0;

// Per-timed-move bookkeeping: which channel is on its timer and when it ends.
int timedChannel = -1;
unsigned long timedUntil = 0;

void relayWrite(uint8_t idx, bool on) {
  digitalWrite(CH[idx], (on == ACTIVE_LOW) ? LOW : HIGH);
}

void allOff() {
  for (uint8_t i = 0; i < 8; i++) relayWrite(i, false);
  timedChannel = -1;
}

void setup() {
  for (uint8_t i = 0; i < 8; i++) {
    pinMode(CH[i], OUTPUT);
    relayWrite(i, false);           // start safe: everything off
  }
  Serial.begin(115200);             // USB debug
  Serial1.begin(115200);            // link to ESP32 (pins 19 RX1 / 18 TX1)
  lastFrameAt = millis();
  Serial.println(F("[MEGA] motor controller ready"));
}

// Energize exactly one channel of a motor pair, forcing the sibling OFF first.
void driveExclusive(int onCh, int offCh, unsigned long durationMs) {
  relayWrite(offCh, false);         // kill opposite coil (no shoot-through)
  relayWrite(onCh, true);
  if (durationMs > 0) {
    timedChannel = onCh;
    timedUntil = millis() + min(durationMs, DEFAULT_PULSE_CAP);
  } else {
    timedChannel = -1;              // latch (self-locking arm / hold)
  }
}

void stopPair(int a, int b) {
  relayWrite(a, false);
  relayWrite(b, false);
  if (timedChannel == a || timedChannel == b) timedChannel = -1;
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

  // Timed-move expiry.
  if (timedChannel >= 0 && millis() >= timedUntil) {
    relayWrite(timedChannel, false);
    timedChannel = -1;
  }

  // Watchdog: comms silent → stop everything (except latched holds are also
  // dropped, which is the safe choice when the brain is unreachable).
  if (millis() - lastFrameAt > WATCHDOG_MS) {
    allOff();
    lastFrameAt = millis();  // re-arm so we don't spam
  }
}
