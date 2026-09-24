# 🔌 Mouss Tec Robot — Wiring & Pinout

Hardware bridge between the **ESP32** (brain-stem: Wi-Fi/API/audio), the
**ESP32-CAM** (vision/faces), the **Arduino Mega 2560** (motor logic), and the
**8-channel 5V relay** board driving the recycled BMW/MINI motors.

```
                    ┌─────────────────────┐
   INMP441 mic ───► │                     │        ┌──────────────────┐
   MAX98357A amp ◄─ │   ESP32 (bridge)    │◄─Wi-Fi►│  Mouss Tec API   │
                    │  Wi-Fi + I2S + UART │        │ /api/robot/v1/*  │
                    └─────────┬───────────┘        └──────────────────┘
                              │ UART2 (serial frames)      ▲
                              ▼                            │ Wi-Fi (JPEG POST)
                    ┌─────────────────────┐        ┌───────┴──────────┐
                    │ Arduino Mega 2560   │        │   ESP32-CAM      │
                    │  8-ch relay logic   │        │ vision + faces   │
                    └─────────┬───────────┘        └──────────────────┘
                              ▼
                    ┌─────────────────────┐
                    │ 8-CH 5V RELAY BOARD │──► BMW/MINI motors (12V, high-amp)
                    └─────────────────────┘
```

> ⚡ **Power**: logic (ESP32/Mega/relay coils) on regulated **5V**; motors on
> **12V** from a separate high-current supply/battery. **Common all grounds.**
> Never back-feed 12V into the 5V logic rail. Fuse each motor line.

## 0) Parts list — what you have and what's still missing

| Part | Status | Used for |
|------|--------|----------|
| Arduino Mega 2560 R3 (CP2102) | ✅ bought | relays + limit switches |
| ESP32 38-pin (CP2102) | ✅ bought | the "bridge": Wi-Fi, voice, Mega link |
| ESP32-CAM OV2640 | ✅ bought | camera: faces, parts, live view |
| MAX98357A I2S amp | ✅ bought | the robot's voice |
| Relay 8CH 5V, optocoupler, **active-LOW** | ✅ bought | motor switching (firmware set to active-LOW) |
| USB-TTL FTDI FT232RL | ✅ bought | flashing the ESP32-CAM (it has no USB) — see §9 |
| XL4015 5A buck converter | 🛒 in cart | 12 V → 5 V for all the boards — see §0a |
| 6× micro limit switch | 🛒 in cart | end-stops: head L/R, each arm up/down — see §5b |
| Breadboard 1660 | 🛒 in cart | logic wiring only (never motor current) |
| **INMP441 I2S microphone** | ❌ **missing** | without it the robot can't hear (`HAS_MIC`) |
| **Speaker 4 Ω or 8 Ω, 3 W** | ❌ **missing** | the MAX98357A needs a speaker to talk |
| **Resistors 1 kΩ + 2 kΩ** (or a logic-level converter) | ❌ **missing** | Mega TX (5 V) → ESP32 RX (3.3 V) — without it you can damage the ESP32 |
| **Blade fuses + holders** (≈10 A per motor, 15 A main) | ❌ **missing** | protect wiring + relay contacts |
| **2× 12 V automotive relay 30/40 A** (from the BMW parts pile) | ❌ recommended | tracks: two wiper motors exceed the module's 10 A contacts — see §5 |
| 12 V battery / supply for the motors | ❓ | motors + the XL4015 input |
| microSD module (SPI) | optional | offline catalog/queue (`HAS_SD`) |
| PIR sensor | optional | presence on the camera (`HAS_PRESENCE_SENSOR`) |
| ACS712-20A ×4 | optional | current-based maintenance (`HAS_CURRENT_SENSORS`) — the limit switches already stop stalls |
| Push button | optional | push-to-talk on GPIO4 |

Firmware flags already match this list: `HAS_LIMIT_SWITCHES 1`, `HAS_CURRENT_SENSORS 0`
(Mega); `HAS_MIC 1`, `HAS_SD 0`, `HAS_BATTERY_SENSE 0` (bridge — set `HAS_MIC 0`
until the microphone is wired); `HAS_PRESENCE_SENSOR 0` (camera).

## 0a) Power with the XL4015

```
12 V battery ──[15 A main fuse]──┬──► relay contacts (motor +12 V side)
                                 └──► XL4015 IN+ / IN−
XL4015 OUT 5.1 V ──► Mega 5V · ESP32 5V/VIN · ESP32-CAM 5V · relay VCC(+JD-VCC) · MAX98357A VIN
All GNDs joined (battery −, XL4015 OUT−, every board GND).
```

> ⚠️ **Set the XL4015 to 5.0–5.1 V with its trimmer BEFORE connecting any
> board** (measure with its own voltmeter/USB port). They ship set to anything —
> 12 V on the 5 V pins kills every board at once. Its 5 A is enough: ≈2.5 A worst
> case (relays ≈0.6 A, two ESP32s ≈1 A peaks, amp ≈0.7 A, Mega ≈0.1 A).
> Feed the boards through their **5V/VIN pins**, not USB, when running on battery.

---

## 1) ESP32 (bridge)  ↔  Arduino Mega 2560 — UART

| ESP32 pin | Mega pin | Signal |
|-----------|----------|--------|
| GPIO17 (TX2) | 19 (RX1) | ESP32 → Mega commands |
| GPIO16 (RX2) | 18 (TX1) | Mega → ESP32 limit/current reports — **via a divider**: Mega TX1 → 1 kΩ → GPIO16, and GPIO16 → 2 kΩ → GND (5 V → 3.3 V) |
| GND | GND | common ground |

Baud **115200**. Frames: `<actuator:direction:duration_ms>`.

## 2) ESP32 ↔ INMP441 I2S microphone

| INMP441 | ESP32 | Note |
|---------|-------|------|
| SCK | GPIO14 | I2S bit clock |
| WS  | GPIO15 | word select (L/R) |
| SD  | GPIO32 | data out → ESP32 |
| L/R | GND | selects left channel |
| VDD | 3V3 | |
| GND | GND | |

## 3) ESP32 ↔ MAX98357A I2S amplifier

| MAX98357A | ESP32 | Note |
|-----------|-------|------|
| BCLK | GPIO26 | bit clock |
| LRC  | GPIO25 | word select |
| DIN  | GPIO22 | data from ESP32 |
| VIN  | 5V | |
| GND  | GND | |
| GAIN/SD | (see datasheet) | gain select / shutdown |

> The plain **ESP32-CAM has no spare pins** for I2S audio + UART + relays, which
> is why audio + the Mega bridge live on the **standalone ESP32**, and vision
> lives on the ESP32-CAM. They coordinate only through the backend.

## 4) ESP32-CAM (AI-Thinker) — camera + presence

Camera pins are the standard AI-Thinker map (see `esp32_cam.ino`). Free pin:

| Sensor | ESP32-CAM | Note |
|--------|-----------|------|
| PIR / IR presence OUT | GPIO13 | optional — set `HAS_PRESENCE_SENSOR 1` in `esp32_cam.ino`; HIGH triggers a face check |
| VCC | 5V | |
| GND | GND | |

## 5) Arduino Mega  ↔  8-Channel 5V Relay (ACTIVE-LOW)

| Relay CH | Mega pin | Motor line | Function |
|----------|----------|------------|----------|
| IN1 | 22 | Head motor coil A | Head **LEFT** |
| IN2 | 23 | Head motor coil B | Head **RIGHT** |
| IN3 | 24 | Arm-LEFT coil A | Arm L **UP** |
| IN4 | 25 | Arm-LEFT coil B | Arm L **DOWN** |
| IN5 | 26 | Arm-RIGHT coil A | Arm R **UP** |
| IN6 | 27 | Arm-RIGHT coil B | Arm R **DOWN** |
| IN7 | 28 | Track drive bus + | **FORWARD** |
| IN8 | 29 | Track drive bus − | **BACKWARD** |
| VCC | 5V | relay logic | |
| GND | GND | common | |
| JD-VCC | 5V (motor-side, jumper per board) | coil power | keep opto-isolation if board supports it |

### Motor wiring (each relay on the module is SPDT: COM / NO / NC)
Each **reversible** motor (head, arms) uses two relays like this:

```
            Relay A (e.g. CH1)          Relay B (e.g. CH2)
   +12V ──── NO                  +12V ── NO
   GND  ──── NC                  GND  ── NC
             COM ──► motor lead 1         COM ──► motor lead 2
```
- A on → lead 1 = +12 V, lead 2 = GND → turns one way; B on → the other way.
- Both off → both leads on GND → the motor brakes. Wired like this, even both
  on can't short the battery (both leads at +12 V); the firmware still never
  energizes both.
- Fuse each motor's +12 V feed (≈10 A).
- **Arms self-lock**: power-window worm-gear motors hold position with no power,
  so every move is a short timed pulse (`0` = the 500 ms default, max 5 s) and
  the arm simply stays where it stopped.
- **Tracks** here share a forward/backward bus (skid-steer via pulsing). For
  independent left/right tracks, move to 4 relays and extend the enum + map in
  `arduino_mega_motor_control.ino`.
- ⚠️ **The module's relays are rated 10 A.** One window motor is fine, but the
  two wiper motors on the track bus together draw more than that (and far more
  at start). Let CH7/CH8 switch the **coils of two 12 V automotive relays
  (30/40 A)** — there are plenty in the BMW/MINI parts pile — and put the track
  motors on those. Same wiring pattern as above, on the big relays.
- **Flyback protection**: use relays rated for the motor's inrush; add a
  snubber/diode across DC motor terminals to protect contacts.

## 5b) Limit switches (end-stops) ↔ Arduino Mega

Six micro limit switches stop each motor at the end of its travel — the motor
is cut the instant it touches the switch, and a move further that way is
refused. The robot never stalls a motor against its mechanical end.

| Switch | Mega pin | Stops |
|--------|----------|-------|
| Head fully LEFT | 30 | head → left |
| Head fully RIGHT | 31 | head → right |
| Left arm fully UP | 32 | arm_left → up |
| Left arm fully DOWN | 33 | arm_left → down |
| Right arm fully UP | 34 | arm_right → up |
| Right arm fully DOWN | 35 | arm_right → down |

Wire each switch **COM → GND and NC → the pin** (pins use INPUT_PULLUP). At
rest NC is closed → pin reads LOW (free). Pressed → opens → HIGH (stop). A
broken wire also reads HIGH, so a fault stops that direction instead of
letting the motor run blind. Mount each switch so the moving part presses it
just **before** the mechanical end.

Test from the Mega's Serial Monitor (115200): type `<arm_left:up:300>` — it
moves; hold the up switch pressed and send it again — it refuses and prints
`[MEGA] limit arm_left:up`.

## 6) Safety interlocks (firmware-enforced)
- **Exclusive direction** per motor pair (opposite coil forced off first).
- **Watchdog** kills all relays if no valid serial frame arrives for 3 s
  (Wi-Fi/serial loss → stop moving).
- **Pulse cap** (5 s) on any timed move so a lost `:stop` can't run a motor
  indefinitely.
- **Limit switches** (§5b) cut a motor at the end of its travel.
- Motor commands require a **face-authorized employee** at the API layer — an
  anonymous request can't lift an arm.

---

## Command reference (what the backend queues → ESP32 → Mega)

| actuator | directions | notes |
|----------|-----------|-------|
| `head` | `left`, `right`, `stop` | pan |
| `arm_left` | `up`, `down`, `stop` | always timed; holds position unpowered |
| `arm_right` | `up`, `down`, `stop` | |
| `track` | `forward`, `backward`, `stop` | skid-steer turns = pulse one side |
| `all` | `stop` | emergency stop-all |

| `ping` | `0` | keep-alive from the ESP32 every 1 s (moves nothing) |

Example frames: `<head:left:800>`  `<arm_left:up:700>`  `<track:forward:1500>`
`<all:stop:0>`  `<ping:0:0>`

Mega → ESP32: `<cur:head:2.35>` (every 1 s while running), `<fault:arm_left:11.20>`
(stall — the Mega already cut that motor).

## 7) Current sensors — OPTIONAL (set `HAS_CURRENT_SENSORS 1` once fitted)

One **ACS712-20A** per motor, in series with the motor's common lead:

| Sensor | Mega pin | Motor | Stall limit |
|--------|----------|-------|-------------|
| OUT | A0 | head | 8 A |
| OUT | A1 | arm_left | 10 A |
| OUT | A2 | arm_right | 10 A |
| OUT | A3 | track | 15 A |
| VCC / GND | 5V / GND | | |

Zero is calibrated at boot with all motors off. Tune the limits in both
`arduino_mega_motor_control.ino` (`CUR_LIMIT_A`) and `robot/services.py`
(`MOTOR_CURRENT_LIMIT_A`) to your motors.

## 8) Bridge ESP32 extras

| Part | ESP32 pin | Note |
|------|-----------|------|
| microSD CS / SCK / MISO / MOSI | 5 / 18 / 19 / 23 | offline catalog + queue + `/offline.wav` |
| Push-to-talk button | GPIO4 → GND | optional; otherwise voice activity detection |
| Battery sense | GPIO34 | 12 V via 100k/22k divider |

## 9) Flashing each board

**Arduino IDE setup**: install the *esp32 by Espressif* board package and the
**ArduinoJson** library (v6 or v7).

| Board | IDE board | How |
|-------|-----------|-----|
| Mega 2560 | *Arduino Mega or Mega 2560* | USB (CP2102 driver) → Upload |
| ESP32 38-pin | *ESP32 Dev Module* | USB (CP2102) → Upload (hold BOOT if it won't connect) |
| ESP32-CAM | *AI Thinker ESP32-CAM* | through the **FT232RL**, below |

ESP32-CAM ↔ FT232RL (jumper on the FTDI at **3.3 V** logic):

| FT232RL | ESP32-CAM |
|---------|-----------|
| GND | GND |
| TX | U0R (GPIO3) |
| RX | U0T (GPIO1) |
| — | **GPIO0 → GND** while uploading |

Power the cam's **5V pin from the XL4015** (grounds shared with the FTDI).
Don't use the FTDI's VCC pin: with the jumper on 3.3 V it outputs 3.3 V, which
isn't enough for the cam.

Connect GPIO0 to GND, press the cam's RST, Upload; when done remove the
GPIO0 wire and press RST again to run. A "brownout" reset means its 5 V is
too weak — power it from the XL4015, not the FTDI, once running.

Before flashing, fill in each sketch's `WIFI_SSID`, `WIFI_PASS`, `API_BASE`
(your workshop's `https://<workshop>.mousstec.com/api/robot/v1`) and
`ROBOT_TOKEN` (printed once by `create_robot_device`).
