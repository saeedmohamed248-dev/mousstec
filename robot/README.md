# 🤖 Mouss Tec Robot — Physical Edge-Agent

A heavy-duty autonomous workshop robot built from **recycled BMW / MINI parts**
(steering columns, power-window motors, wiper motors), wired into the Mouss Tec
ERP + multi-agent SaaS platform as a first-class **edge agent**.

```
 INMP441 mic ─┐                              ┌────────────────────────────┐
 MAX98357A ◄──┤  ESP32 (bridge)  ── Wi-Fi ──►│  Mouss Tec (Django + DRF)  │
 amp          │  audio + API + UART          │  /api/robot/v1/*           │
              └──────┬───────────────────────┤  robot app ↔ inventory/hr/ │
                     │ serial <a:d:ms>        │  bmw_ecu/diagnostics       │
              ┌──────▼──────────┐             └──────────────┬─────────────┘
              │ Arduino Mega    │                            │ Wi-Fi (JPEG)
              │ 8-ch relay MCU  │             ┌──────────────▼─────────────┐
              └──────┬──────────┘             │  ESP32-CAM (vision/faces)  │
                     ▼                        └────────────────────────────┘
        BMW/MINI motors (head, 2 arms, 2 tracks)
```

Hardware split (why two ESP32s): the plain **ESP32-CAM** has no spare pins for
I2S audio + UART + relays, so **vision/faces** live on the ESP32-CAM and
**audio + the Mega serial bridge** live on a **standalone ESP32**. They
coordinate only through the backend.

## What's in this app

| File | Role |
|------|------|
| `models.py` | `RobotDevice`, `RobotScanEvent`, `RobotVoiceInteraction`, `RobotAccessLog`, `ProcurementSignal`, `MotorCommandLog` — audit trail + hardware↔ERP bridge |
| `pricing.py` | **Retail-only guard** — the single audited gate for every price the robot sees |
| `services.py` | Inventory lookup, dynamic scrap pricing, invoice creation, procurement trigger, DTC + ECU lookup |
| `security.py` | Device-token auth + facial recognition (via `hr.Employee.face_encoding`) + attendance |
| `vision.py` | Pluggable part-ID + scrap-condition (deterministic stub → real model via env) |
| `views.py` / `urls.py` | DRF API the ESP32 calls (`/api/robot/v1/*`) |
| `firmware/` | Arduino Mega + ESP32 bridge + ESP32-CAM sketches, and **`WIRING.md`** (full pinout) |
| `tests/` | Wholesale-leak guard + dynamic-pricing tests (run with no DB) |

## 🔒 The wholesale-price guarantee (hard rule)

> **متديلوش أسعار الجملة** — the robot must never expose wholesale prices.

`inventory.Product` carries `retail_price`, **`b2b_wholesale_price`**,
`purchase_price` and `average_cost`. The robot may only ever receive **retail**
figures. Enforcement is defense-in-depth:

1. **Allow-list**, not deny-list: `safe_product_payload()` builds robot payloads
   field-by-field and only ever adds `retail_price` (and `scrap_price` /
   `ai_suggested_price` for used parts).
2. **Tripwire**: `_assert_no_wholesale()` raises if any forbidden key
   (`b2b_wholesale_price`, `purchase_price`, `average_cost`, …) ever appears —
   so a future copy-paste regression fails tests instead of leaking silently.
3. **Invoice safety**: `SaleInvoiceItem.save()` auto-fills the *wholesale* price
   when no `unit_price` is given — so `create_robot_sale()` **always passes an
   explicit `retail_price`**.
4. **Redaction**: `redact()` scrubs any free-text reply before it's spoken.

`robot/tests/test_pricing_guard.py` asserts no wholesale value can appear in a
payload. Run it anywhere: `python -m unittest robot.tests.test_pricing_guard`.

## Feature → implementation map

| # | Requirement | Where |
|---|-------------|-------|
| 1 | Physical articulation (relays: head/arms/tracks, self-locking) | `firmware/arduino_mega_motor_control` + `MotorCommandLog` + `POST /motor/` |
| 2 | Smart POS & inventory (vision → invoice, deduct stock) | `POST /scan/` + `POST /sale/` → `inventory.SaleInvoice` |
| 3 | Access control & facial recognition (attendance + security gate) | `POST /face/` + `security.py` → `hr.Employee`/`AttendanceRecord` |
| 4 | Voice workshop assistant (hands-free inventory/fault queries) | `POST /voice/` + `services.inventory_answer` / `lookup_fault_code` |
| 5 | Scrap condition & dynamic pricing | `vision.assess_condition` + `services.suggest_used_price` (retail-anchored) |
| 6 | Multi-agent sync (low stock → Procurement Agent) | `services.maybe_raise_procurement_signal` → `ProcurementSignal` |
| ➕ | Part codes / OEM cross-reference | `Product.oem_cross_reference` surfaced safely in scan/voice |
| ➕ | Fault codes / "قرابات الأعطال" | `services.lookup_fault_code` → `diagnostics_catalog.DTCDefinition` |
| ➕ | ECU coding / programming refs | `services.lookup_ecu_profile` → `bmw_ecu.EcuHardwareProfile` |
| ➕ | Branch-aware (فروع) | every stock/price scoped to `device.branch` |

## API (prefix `/api/robot/v1/`, device-token auth via `X-Robot-Token`)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `heartbeat/` | liveness + firmware version |
| POST | `scan/` | part image/barcode → stock + **retail** price (scrap → suggested price) |
| POST | `voice/` | transcript → intent → spoken reply |
| POST | `face/` | face → attendance clock-in/out + authorize session |
| POST | `sale/` | create retail sale (**requires face-authorized employee**) |
| POST | `motor/` | queue articulation command (**requires face auth**) |
| GET | `motor/pending/` | ESP32 pulls frames → forwards to Mega, auto-acked |
| GET | `procurement-signals/` | open low-stock signals for the Procurement Agent |
| POST | `teach/` | staff correct a scan or teach an alias (**requires face auth**, role `teach`) |
| POST | `enroll/capture/` | camera capture during a staff face-enrollment round |
| GET | `kiosk/part/`, `kiosk/customer/`, `kiosk/return-check/` | read-only answers for the `smart_robot` kiosk |

## Extra innovations added (وابتكر معايا)

- **Trigger-agent procurement** with an idempotent `ProcurementSignal` (one open
  signal per part/branch) that hands a ready **manifest payload** to the
  Procurement Agent — no duplicate ordering.
- **Safety interlocks in firmware**: exclusive motor direction (no dead-short),
  a comms watchdog that stops all motion on Wi-Fi/serial loss, and a pulse cap
  so a lost `stop` can't run a motor forever.
- **Self-locking arms**: worm-gear window motors hold position with zero power,
  so every move is a short timed pulse (max 5s) — nothing stays energized.
- **Face-gated physical actions**: an anonymous request can't create a sale *or*
  lift an arm — the same authorization gate protects money and motion.
- **Voice fault-code coach**: a mechanic under the car can ask about a DTC (e.g.
  "P0301") and hear the description, likely causes and guided steps.
- **Bilingual** (Arabic/English) voice replies.

### Ideas worth adding next
- On-device wake-word + VAD so the mic only streams speech (saves bandwidth).
- ESP32-CAM edge face-embedding to avoid sending raw face images.
- Battery/curr­ent telemetry per motor for predictive-maintenance on the robot.
- Shelf-mapping so the robot's arm auto-points to a part's bin location.

## Completed end-to-end flows (round 3)

- **Cash vs. credit** — a cash sale now settles the full amount into the branch
  cash `Treasury` (recorded as a `FinancialTransaction`); credit leaves it due.
> ⚠️ **Face authorization needs `face_recognition` installed.** Without it the
> Pillow fallback is a 16x16 grayscale thumbnail, not a face embedding: two
> different people in front of the same camera score ~0.99 against each other,
> far above the 0.85 match threshold, so it would authorize the first enrolled
> employee for anyone. The backend therefore refuses to match at all when no
> real model is present — `/face/` returns 503 and no sale, dispense or
> clock-in is authorized. Install `face_recognition` and enroll faces with it,
> or set `ROBOT_FACE_ALLOW_INSECURE_MATCH=1` knowingly for a demo on fake data.

- **Face recognition from an image** — `/face/` accepts the ESP32-CAM JPEG and
  extracts an embedding server-side (`robot/faces.py`). Enroll staff with the
  SAME extractor (`faces.enroll_employee`) so vectors are comparable. Uses the
  `face_recognition` (dlib) library when installed, else a consistent
  dependency-free fallback (Pillow) so the pipeline works out of the box.
- **Speech in/out** — `/voice/` accepts raw `audio` and transcribes it
  (`robot/audio.py`, Gemini STT); `/speak/` returns synthesized audio (gTTS).
  Both degrade gracefully: post a ready `transcript`, or fall back to on-device
  synthesis, when no provider is configured.
- **Conversational stock-take** — say "اجرد", then "<part> <qty>" per item, then
  "خلص الجرد"; `/stock-take/apply/` (device) or the dashboard "اعتمد التسوية"
  button corrects inventory to the counted numbers (adjustment movements).
- **Procurement → RFQ** — a low-stock signal now also opens a real
  `inventory.RFQ` (deduplicated) so vendors can quote; the RFQ id is stored on
  the signal's manifest for the Procurement Agent.
- **Visual learning** — intake stores a perceptual image hash as a
  `RobotKnowledge` fingerprint key, so the same part is re-recognized from a
  photo, not only from its code/label.

### Optional dependencies (all degrade gracefully)
```
face_recognition   # REQUIRED for face auth — see the note below
gTTS               # text-to-speech for /speak/
google-generativeai# Gemini STT for /voice/ audio (already used by the ERP)
rembg / onnxruntime# studio-white background (via inventory bg_removal)
```

> **Multi-tenant routing**: the ERP uses `django_tenants` (schema per branch/
> workshop), routed by hostname. Point each device's `API_BASE` at the tenant's
> own subdomain (e.g. `https://<workshop>.mousstec.com/api/robot/v1`) so its
> `RobotDevice`, inventory and staff resolve in the right schema — not the base
> domain.

## Per-role command permissions + customer memory (round 4)

- **Role-gated commands** (`robot/permissions.py`): every privileged action is
  checked against the RECOGNIZED employee's role (`EmployeeProfile.role`), not
  just "is a face known". Cashier/sales → sell; stock/purchasing → intake;
  stock/manager → stock-take; manager/owner → approve adjustments; tech/engineer
  /stock → motor. Wrong role → the robot refuses out loud with the reason.
  Owner/admin (and Django superusers) may do everything.
- **Customer memory** (`robot/customers.py` + `RobotCustomerFace`): the robot
  greets walk-ins and **remembers them**. `POST /customer/greet/` recognizes a
  customer by **face, name, phone, or invoice number**, bumps their visit
  counter, enrolls their face for next time, and returns a warm personal
  greeting — returning-customer aware, VIP/loyalty aware, with a recall of the
  last part they bought and a gentle suggestion. Any outstanding balance is
  surfaced only in a private `staff_note`, never spoken aloud (privacy first).
  No wholesale/cost is ever exposed — only the customer's own retail history.

## Live control, 24/7 camera, offline mode, paging, head-tracking (round 5)

- **24/7 camera + live view**: the ESP32-CAM never sleeps; it pushes a frame to
  `/camera/frame/` ~every 1.5s. The dashboard **Control** page shows it live
  (auto-refreshing `<img>`), so you can watch any branch anytime.
- **Full remote control from the dashboard** (`/robot/device/<id>/control/`,
  owner/manager): snapshot on demand, pan the head, raise/lower arms, drive the
  tracks, make the robot **speak** a typed line — each queues a `RobotCommand`
  the device polls at `/commands/pending/` (motion goes through `/motor/pending/`).
- **Owner-only paging** (`أنا بس اللي أعمل كده`): `/robot/device/<id>/page/` is
  restricted to **owner/admin**. Pick an employee → the robot announces "مطلوب
  في المكتب" by voice (a `page` command + `RobotPageCall`).
- **After-hours motion alerts**: a motion-flagged frame outside the guard window
  (`RobotDevice.guard_from/guard_to`, default 20:00–08:00) raises a
  `RobotAlert` with a saved snapshot; the **Alerts** page shows them (deduped to
  one per 5 min).
- **Offline mode + sync**: `/sync/pull/` gives the robot a **retail-only** cached
  catalog (parts, prices, stock) so it keeps answering with no internet; the
  firmware queues everything it does/learns to SD and replays it on reconnect
  via `/sync/push/`, which dedupes by `client_uid` (idempotent — a resent batch
  never double-applies).
- **Head turns toward the speaker**: the ESP32-CAM computes the face's
  horizontal position and calls `/look/`; `services.look_at` pans the head to
  center them (dead-zone so it doesn't jitter when already facing them). The
  greet flow also turns the head using the `face_offset` the cam reports.
- **Customer memory page** (`/robot/customers/`): who the robot greeted, visit
  counts, last seen, and whether a face is enrolled.

New device endpoints: `/camera/frame/`, `/snapshot/`, `/commands/pending/`,
`/commands/ack/`, `/look/`, `/sync/pull/`, `/sync/push/`. New dashboard pages:
device **control** + live frame, **alerts**, **customers**, owner **paging**.

## Smooth live video + robot health (round 6)

- **Smooth MJPEG stream**: `/robot/device/<id>/mjpeg/` (owner/admin/manager)
  serves a `multipart/x-mixed-replace` stream a plain `<img>` renders as
  near-real-time video. The control page toggles between light frame-polling
  and this smooth stream. To make it genuinely smooth the loop is closed: the
  "بث سلس" view sets `stream_until`, and `/camera/frame/` returns the target
  `push_interval_ms` so the ESP32-CAM speeds up to ~5 fps while someone is
  watching and drops back to the idle rate afterwards (no 24/7 network hammer).
- **Robot health telemetry**: the device posts battery %, CPU temp, free SD and
  Wi-Fi RSSI to `/telemetry/`; the control page shows them at a glance and a
  **low-battery** alert fires below 15% (deduped). This is the "رؤية شاملة" —
  you see the robot's own vitals, not just what it did.

## Enabling the optional features in production

1. **Real face authorization** — the web image now builds `face_recognition`
   (dlib) from `requirements-robot.txt` (cmake + BLAS added to the Dockerfile).
   Rebuild the image to pick it up: `docker compose up -d --build web`. Until
   it's installed, face matching fails closed (`/face/` → 503) — never a wrong
   authorization. Then enroll staff faces with `robot.faces.enroll_employee` so
   the stored vectors match the same extractor.
2. **Server-side TTS** — `gTTS` is in the same `requirements-robot.txt`, so the
   same rebuild enables `/speak/`; otherwise the ESP32 synthesizes on-device.
3. **Register each robot + get its token** — one command instead of the admin:
   ```bash
   python manage.py tenant_command create_robot_device \
       --name "روبوت الفرع الرئيسي" --branch "<اسم الفرع>" --schema=<tenant_schema>
   ```
   It prints the API token once — paste it into `ROBOT_TOKEN` in the firmware
   (`firmware/esp32_bridge/esp32_bridge.ino` and `esp32_cam/esp32_cam.ino`).
   Run without `--branch` first to list the branch names/ids.

## Setup

The app is registered in `TENANT_APPS` and mounted at `/api/robot/v1/`.

```bash
python manage.py migrate            # applies robot/migrations/0001_initial
# In the admin: create a RobotDevice for a branch, copy its api_token
# Flash firmware/ sketches with your Wi-Fi + API_BASE + that token.
```

Vision runs a safe deterministic **stub** by default (unknown parts defer to the
printed barcode — never a wrong sale). Set `ROBOT_VISION_PROVIDER` and implement
the provider hooks in `vision.py` to plug in a real model.

## Tests

```bash
python -m unittest robot.tests.test_pricing_guard   # 10 tests, no DB needed
```

## Review fixes + the learning loop (round 7) — «الروبوت يتعلم من كل حاجه»

**Fixes**
- `services.py` never imported `timedelta`: every `/voice/` call (it checks for
  an open stock-take first), every after-hours alert and every low-battery
  alert crashed with a 500. Fixed + covered by tests.
- **Attendance**: the ESP32-CAM posts `/face/` on every motion, and each match
  toggled in/out — walking past the robot twice ended the shift. `/face/` now
  takes `purpose` (`authorize` = passive sighting, `attendance` = deliberate
  check-in/out); a second match within 60 min is never a clock-out, and
  passive sightings only move "last seen" forward.
- **Motors**: actuator/direction/duration are validated and clamped
  (`services.validate_motor_command`) on the API and the dashboard. `0` no
  longer means "run until stop" (it would stall a window motor at its end stop
  or keep the tracks driving if a stop frame is lost).
- **Mega firmware**: one shared move timer meant a second motor made the first
  run until the watchdog — now one timer per relay channel. The ESP32 bridge
  sends a `<ping:0:0>` keep-alive every second so the Mega's 3s watchdog only
  fires when the bridge is really gone.
- Intake rejects bad/negative quantities and prices (negative would *remove*
  stock); only a **completed** stock-take can be applied; offline sync uses a
  savepoint per event and now applies `count` events (as a stock-take a manager
  still approves); snapshot `reason` is checked against its choices.
- **Connectivity alerts** (`offline` / `back_online` kinds existed but were
  never raised): a device returning after 5+ min raises `back_online`, and the
  Celery Beat task `robot.tasks.raise_offline_alerts` (every 5 min) raises one
  `offline` alert per outage.

**Learning — every interaction teaches it something**
| It learns from | How | Used by |
|---|---|---|
| A confirmed sale | code + label + **photo look** → product | `/scan/` |
| Goods intake | code + label + photo look | `/scan/`, `/intake/` |
| A correction (`/teach/` with `scan_id`) | weakens the wrong guess (forgets it at 0), learns the right one | `/scan/` |
| A taught word (`/teach/` with `alias`, voice «اتعلم X يعني Y», or the dashboard) | shop slang → part, found **inside sentences** | `/voice/`, offline catalog |
| Questions it couldn't answer | flagged `unresolved`, listed on the dashboard next to a teach form | staff |
| "Out of stock" answers | someone asked → demand → procurement signal | Procurement Agent |
| Every customer purchase | car model / category tallies (no prices) | the greeting |

A photo is matched by a 64-bit average-hash within 5 bits of a learned one;
because similar-looking parts exist, a look-only match comes back with
`needs_confirmation: true` and is never treated as certain.

## First-install face enrollment + everything else (round 8)

### 🧑‍💼 The robot enrolls your staff itself, one by one, by name
After installing the robot nobody's face is on file, so nobody can be
recognized. Open **Robot → the device → «تسجيل بصمات الموظفين»**
(`/robot/device/<id>/faces/`, owner/admin/manager) and press **ابدأ النداء**
(or tick specific people to re-enroll them). Then the robot:

1. says «أحمد، اتفضل قف قدام الكاميرا وبص لها لحد ما أقولك خلاص»;
2. the camera learns from its `/camera/frame/` reply that enrollment is on and
   sends a capture to `/enroll/capture/` about once a second;
3. only captures with **exactly one face** count, and each must match the
   first one (someone else stepping in is ignored); with two people in frame
   it asks for the employee alone;
4. after 3 good samples it stores the averaged embedding on
   `hr.Employee.face_encoding`, says «تمام يا أحمد، بصمتك اتسجلت», keeps the
   photo for you to check, and calls the next name;
5. no-shows are called again every 30 s and skipped after 4 calls; you can
   also press «مش موجود — اللي بعده» or say «مش موجود» / «التالي»;
6. at the end it reads a summary (who was enrolled, who was skipped).

It refuses to start without `face_recognition` installed, and it won't enroll
a face that already belongs to another employee under a second name.
Later (new hires) a manager can also say «سجّل بصمات الموظفين».

### Everything else
- **Voice end to end** (bridge firmware 2.0): energy VAD or push-to-talk
  (GPIO4) → WAV upload to `/voice/` → reply spoken from `/speak/?format=wav`
  (16 kHz PCM via ffmpeg, now in the Docker image). Voice commands use the
  face the camera recognized in the last 60 s for role checks.
- **Camera commands actually reach the camera**: snapshot/scan requests ride
  on the `/camera/frame/` reply (the bridge used to ack snapshots nobody
  took). «امسح القطعة دي» queues a scan and the result is spoken. Motion no
  longer fires a part scan every time.
- **SD offline mode**: catalog cache, NDJSON queue replayed to `/sync/push/`,
  and `/offline.wav` played when the net is down
  (`python manage.py robot_offline_clip`).
- **Hashed device tokens**: only SHA-256 is stored (migration 0007 converts
  existing ones — robots keep working). New tokens are shown once.
- **No overselling**: `/sale/` refuses more than branch stock (409) unless
  `allow_backorder`.
- **Predictive maintenance**: ACS712 per motor on the Mega; a stall cuts the
  motor instantly (250 ms inrush blanking); currents flow to `/telemetry/`
  → running baseline per motor → `motor_fault` alerts for stall or wear.
- **Shelf pointing**: the voice answer says the shelf (`Inventory.shelf_location`)
  and turns the head using `RobotDevice.shelf_map`, e.g.
  `{"A": {"direction": "left", "ms": 600}, "B3": {"direction": "right", "ms": 300}}`.
- **Learned used-part pricing**: suggestions are scaled by the median of
  sold ÷ suggested over recent scrap sales (≥5 sales, bounded 0.7–1.3, never
  outside scrap..retail).
- **LLM fallback** for general questions (ERP gateway, `redact`ed, no prices).
- **Kiosk (`smart_robot/`) on the real ERP**: set `MOUSS_ERP_API` +
  `MOUSS_ROBOT_TOKEN`; it never falls back to mock data if the ERP is down.

### Not done (needs hardware you choose)
- **Liveness / anti-photo**: a single RGB JPEG from an ESP32-CAM can't tell a
  printed photo from a face reliably. Needs an IR/depth camera or a dedicated
  liveness model; until then face auth resists casual misuse, not a photo.
- **On-device wake word**: needs an ESP32-S3 (ESP-SR). Until then the robot
  has a **name** checked on the server — see round 9.

## Its name — it answers only when spoken to (round 9)

The robot is called **«موس»** by default (change it on the device profile
page). It answers only speech addressed to it:

- «يا موس، عندك طرمبة مية E90؟» → answers. «يا موس» alone → «أيوه، تحت أمرك.»
- People talking to each other nearby → silence; the sentence is **not stored**.
- Follow-ups within 20 s need no name («وبكام؟»).
- No name needed while the push-to-talk button is held, or for the
  enrollment round's own words («مش موجود» / «التالي»). During a stock count
  each accepted count keeps the 20 s window open, so a steady counter never
  repeats the name — but other chatter nearby is still ignored.
- Speech-to-text may spell the name differently (موص / ماوس / Mouss); common
  variants are built in, and you can add more on the profile page
  (check «التفاعلات الصوتية» for how it was heard).

How: the bridge's VAD uploads each utterance, the server transcribes it and
`robot/wakename.py` looks for the name as a whole word (Arabic letter forms
normalized), strips it, and only then routes the command. Trade-off: every
utterance near the robot is still sent to the server for transcription, even
the ones it then ignores — an on-device wake word (ESP32-S3) would keep those
on the robot.

## Matched to the purchased parts (round 10)

See `firmware/WIRING.md` §0 for the full have/missing list. Firmware flags:
Mega `HAS_LIMIT_SWITCHES 1` (the 6 micro switches as end-stops on pins 30–35,
NC-to-GND fail-safe) and `HAS_CURRENT_SENSORS 0`; bridge `HAS_MIC 1`,
`HAS_SD 0`, `HAS_BATTERY_SENSE 0`; camera `HAS_PRESENCE_SENSOR 0`. Unfitted
inputs are never read, so floating pins can't fake stalls, motion or speech.
Still to buy: INMP441 mic, a 3 W speaker, a 1k/2k divider for Mega→ESP32
serial, fuses, and automotive relays for the track motors.
