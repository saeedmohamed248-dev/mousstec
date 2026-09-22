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

---

## 1) ESP32 (bridge)  ↔  Arduino Mega 2560 — UART

| ESP32 pin | Mega pin | Signal |
|-----------|----------|--------|
| GPIO17 (TX2) | 19 (RX1) | ESP32 → Mega commands |
| GPIO16 (RX2) | 18 (TX1) | Mega → ESP32 acks/telemetry |
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
| PIR / IR presence OUT | GPIO13 | HIGH triggers a face+part scan |
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

### Motor wiring notes
- Each **reversible** motor (head, arms) uses a **2-relay H-pattern**: one relay
  swings the motor lead to +12V, the sibling to the other polarity. The firmware
  guarantees the two are **never energized together** (no dead-short).
- **Arms self-lock**: power-window worm-gear motors hold position with no power,
  so `duration_ms=0` latches the relay OFF after reaching position (the arm stays
  put). Send `<arm_left:stop:0>` to release.
- **Tracks** here share a forward/backward bus (skid-steer via pulsing). For
  independent left/right tracks, move to 4 relays and extend the enum + map in
  `arduino_mega_motor_control.ino`.
- **Flyback protection**: use relays rated for the motor's inrush; add a
  snubber/diode across DC motor terminals to protect contacts.

## 6) Safety interlocks (firmware-enforced)
- **Exclusive direction** per motor pair (opposite coil forced off first).
- **Watchdog** kills all relays if no valid serial frame arrives for 3 s
  (Wi-Fi/serial loss → stop moving).
- **Pulse cap** (5 s) on any timed move so a lost `:stop` can't run a motor
  indefinitely.
- Motor commands require a **face-authorized employee** at the API layer — an
  anonymous request can't lift an arm.

---

## Command reference (what the backend queues → ESP32 → Mega)

| actuator | directions | notes |
|----------|-----------|-------|
| `head` | `left`, `right`, `stop` | pan |
| `arm_left` | `up`, `down`, `stop` | `duration_ms=0` latches (self-lock) |
| `arm_right` | `up`, `down`, `stop` | |
| `track` | `forward`, `backward`, `stop` | skid-steer turns = pulse one side |
| `all` | `stop` | emergency stop-all |

Example frames: `<head:left:800>`  `<arm_left:up:0>`  `<track:forward:1500>`
`<all:stop:0>`
