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
| POST | `status/` | «🩺»: each online service (Azure hearing/voice, Google, Gemini) — answering? why not, what to do, minutes left — and the brain this server ships |
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
  (`robot/audio.py`, Gemini REST with the ERP's `GEMINI_API_KEY`; the robot's
  wake name is passed to the model so it spells it right). `/speak/` returns
  speech in an Egyptian Arabic or English neural voice (`edge-tts`,
  `ROBOT_TTS_VOICE_AR` / `ROBOT_TTS_VOICE_EN` to change them), falling back
  to gTTS. Both degrade gracefully: post a ready `transcript`, or get a 204.
- **Egyptian Arabic or English** — the robot answers in the language it was
  spoken to (`robot/language.py`): replies are written in Egyptian Arabic and
  translated with Gemini for an English speaker; the voice follows the text.
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
edge-tts           # the robot's Egyptian Arabic / English voice for /speak/
gTTS               # fallback text-to-speech for /speak/
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
- **Head turns toward the speaker**: `services.look_at` pans the head to
  center a face at a horizontal offset (dead-zone so it doesn't jitter when
  already facing them), from `/look/` or the greet flow's `face_offset`. The
  plain ESP32-CAM has no face detector, so the server finds the face in its
  live frames — see «الراس يلف مع اللي بيتكلم» (round 18).
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

1. **Real face authorization** — the web image installs `face_recognition` on
   a prebuilt dlib (`dlib-bin` in `requirements-robot.txt`, then
   `face_recognition --no-deps` in the Dockerfile — no 20-40 minute dlib compile).
   Rebuild the image to pick it up: `docker compose up -d --build web`. Until
   it's installed, face matching fails closed (`/face/` → 503) — never a wrong
   authorization. Then enroll staff faces with `robot.faces.enroll_employee` so
   the stored vectors match the same extractor.
2. **Server-side TTS** — `edge-tts` and `gTTS` are in the same
   `requirements-robot.txt`, so the same rebuild enables `/speak/`.
3. **A robot in every branch** — on the robot dashboard, «➕ روبوت لفرع»: pick
   the branch, and the robot's page offers its brain for that branch's computer
   (`mouss-brain.zip`: the brain folder, the launchers, and a `brain.ini` with
   the server's address and the robot's own key — once, right after the key is
   made; «تحديث المخ» any time after is the brain alone, keeping the computer's
   `brain.ini`). `robot/brain_package.py`.
   Or one command instead of the dashboard:
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

## Fast voice on a slow line (round 11) — «سرّعه على النت البطيء»

On the shop's 4G the robot answered seconds late: the question went up as
16-bit PCM, the words came back, a second request fetched their voice, and
(on the DAC line out) the whole reply downloaded before the first word.

| What | Gain |
|------|------|
| The question goes up as **µ-law** (G.711), trailing silence cut to 150 ms | upload ≈ half |
| `/voice/` with `reply_audio=ulaw` answers with the **spoken reply itself** (WAV), the words in `X-Robot-*` headers | one request instead of two |
| The reply comes down as µ-law, loudness evened out on the server | download ≈ half |
| The bridge **plays as it downloads**: it starts once `remaining × (1 − speed/play-speed)` is buffered (the speed it measures), and if the line slows mid-reply it fades out, buffers and goes on | first word after ~0.2 s of audio on a good line, no stutter on a bad one |
| Replies said again and again (and Edge's voice only, never a gTTS stand-in) come from a 3-day cache | no TTS time for repeats |
| Gemini answers with **thinking off** (a model that must think is asked again without it, once, and remembered) | seconds less before every answer |

Memory: once the TLS link is up the ESP32's heap is in pieces (the biggest
block fell from ~98 KB to ~36 KB) and the 32 KB a second of PCM needs often
failed with `[VOICE] no memory`. The question is now always held as µ-law (half
the RAM: 4 s in 64 KB), sized to what malloc has while leaving 24 KB for the
TLS link, and expanded back to PCM on the way out for a server that doesn't
offer µ-law yet. (2.4.0 kept a 50 KB buffer from boot instead, which starved
the TLS handshake: every request failed with -1 — 2.4.1 holds nothing.) A
loose mic wire (random bits, far louder than a voice) and a push-to-talk held
in silence are no longer recorded and sent.

The bridge turns it on by itself only when the server's heartbeat offers it
(`"voice": {"ulaw": true}`), so flashing firmware 2.4.0 before or after
deploying the server is safe. Serial Monitor:

```
[VOICE] sent 1900 ms of speech (31 KB) → 200
[VOICE] "يا موس عندك فلتر زيت؟"
🔊 أيوه، فلتر زيت متوفر (3 قطعة) بسعر 450 جنيه.
[AMP] played 3.1 s of µ-law (downloaded in 1.2 s)
[VOICE] spoke 2.4 s after you stopped: server 1.1 s (hear=700;think=90;voice=310), internet 0.6 s
```

The last line says where the wait went — the server or the internet. Type
`f` to switch the fast voice off and compare with the old way.
`robot/tests/test_firmware_codec.py` compiles the firmware's plain-C++ block
with g++ and checks its µ-law against the server's, byte for byte.

### Is it the mic? (firmware 2.4.3–2.4.4)

A mic sending hiss looked exactly like one sending speech: `sent 1792 ms` →
`the server heard nothing`. Now each upload shows the level of its quietest
and loudest 32 ms frame, and says when the buffer (not a pause) ended it:

```
[VOICE] sent 1792 ms of speech (28 KB, level 4100–8800, cut: no room for more) → 200
[MIC] it never went quiet: noise on the mic wires, not a voice? Type e to hear what the mic sends
```

Words have gaps; a recording without one quiet frame is noise (a loose or
unsoldered mic pin, L/R off GND), not someone talking. Type `e` in the Serial
Monitor: 3 s from the mic are played straight back on the speaker (a weak
mic louder, up to ×8), so you hear what the server gets.

A broken mic also cost money: it sent a clip every two seconds, each one a
Gemini call (the same quota the rest of the ERP uses) for nothing. Since
2.4.4 a clip with a dead frame in it (level 0: the mic cut out; a working one
never reads 0) isn't sent, and after 3 clips in a row the server made nothing
of, listening pauses for 60 s. The push-to-talk button still works meanwhile.

```
[MIC] the mic cut out (level 0–6801): a loose or unsoldered pin — not sent
[VOICE] 3 clips in a row with nothing understood: listening paused 60 s (saves the server's Gemini quota). Fix the mic (type e) or hold the button to talk
```

## «مخ موس» — the robot on a mini PC (round 12)

The ESP32 can't understand speech: the audio went to Gemini on the server
(3.5–4.8 s of every 5–11 s answer) and an unsoldered mic pin made it deaf.
`robot/brain/` runs the robot on a real computer — a mini PC inside the robot,
or a Mac to try it — with a USB mic and speakers: Whisper writes down what was
said **on the machine**, only the words go to `/voice/` (`transcript`), and
the spoken reply comes back in the same response. No server change. See
[`robot/brain/README.md`](brain/README.md).

## The shop's sound, the staff's questions, the sale that goes on (round 13)

`robot/assistant.py`, reached from `/voice/`:

| Said | Who may | What happens |
|---|---|---|
| «شغّل قرآن» · «شغّل موسيقى هادية» · «وقّف الصوت» · «علّي / وطّي الصوت» | `media`: manager, supervisor, owner — the face the camera recognized | the reply «حاضر، …» + `media` (`X-Robot-Media: play:quran` beside the WAV); «مخ موس» plays. A caller without speakers (no `caps=media`: the ESP32) is told so, not promised |
| «بعنا كام النهارده؟» | `sales_report`: manager, supervisor, accountant | the dashboard's own numbers (ReportingService): invoices, net sales, expenses |
| «إيه اللي قرّب يخلص؟» · «النواقص» | `stock_report`: manager, supervisor, stock, purchasing | the dashboard's low-stock list, the emptiest first |
| a part that's out of stock | anyone | «… بس عندي بديله X متوفر بسعر …» — an in-stock part sharing a part / OEM number — or «بس موجود في فرع …» |

Nobody recognized → «بص للكاميرا ثانية علشان أعرفك»; the wrong role → the
usual polite refusal. Offline, «مخ موس» declines these commands («محتاج النت
علشان أتأكد مين بيكلمني») instead of reading «شغل قرآن» as a part name, and
offers the same equivalent from its own catalog.

## It remembers what it was just talking about (round 14)

`robot/memory.py` keeps two minutes of the conversation per robot (the
cache, per workshop): the part being talked about, the parts it just
offered, the last four exchanges. When a sentence is a follow-up, and the
words that answer it, are in `robot/brain/talk.py` — the same file the brain
uses with the net down, so both say the same words.

| Said after… | The robot says | From |
|---|---|---|
| «تقصد أنهي؟» → «E90» · «التانية» · «بتاع الهوا» | that part's price and stock, as if asked by name | the parts it offered |
| «وبكام؟» · «موجود؟» · «وده؟» | its price and stock again | inventory, retail price |
| «مكانه فين؟» | «… في الرف B3.» and the head turns to the shelf | `Inventory.shelf_location` |
| «عليه ضمان؟» | «… عليه ضمان سنة.» — or that none is recorded | `warranty_months` |
| «ماركته إيه؟» · «أصلي ولا تقليد؟» | brand, condition, warranty | `brand`, `condition` |
| «بيركب على E91؟» · «وعلى F30؟» | yes, or what it's recorded to fit + «اتأكد من رقم الشاسيه» | `chassis_compatibility`, `car_model` |
| «فيه أرخص؟» · «فيه بديل؟» | in-stock parts sharing its part/OEM number here, cheapest first; «التانية» then picks one | `assistant.alternatives_here` |

Only a sentence made of follow-up words is a follow-up: one that names
another part is a new question, and only an answer picks one of the offered
parts — «فيه تاني؟» asks for another, «التانية» picks the second. A question
no rule covers goes to the LLM with what was just said and that part's
retail data in front of it (`ai_reply(…, facts=, history=)`); a reply that
names a sum of money not in that data is dropped (`invented_price`). The
memory is an extra: with the cache down, each sentence is answered on its own.

## The morning, the words queued for it, and what the ERP doesn't know (round 15)

* **«مخ موس» says what the server queues** (`/commands/pending/` → `/commands/ack/`,
  every 3 s): the greeting at clock-in, a page for an employee, a voice-asked
  scan's answer, the dashboard's «انطق». The ESP32 bridge did; the brain
  didn't, so on the mini PC these were never heard. Motor commands are acked
  as not done — the motors aren't on that computer.
* **The morning briefing.** The first face of the day (`clock_in`) hears,
  each line only for a role allowed to: yesterday's invoices and net sales
  (`sales_report`, the dashboard's rules), what's running low and what
  customers asked for while it was out (`stock_report`), and how many
  questions the robot couldn't answer (`teach`) — «علّمهولي من صفحة الروبوت».
* **«دوّر على النت على …»** — a manager or supervisor (`web_search`) asks what
  the ERP doesn't know; Gemini looks it up with Google Search and the robot
  says it in two or three sentences, with the source («(المصدر: bmw.com)»;
  the pages are in the interaction's payload). A question nothing in the ERP
  answered is looked up the same way for them — but not a stock question
  («عندك كوتش؟» stays «ملقتش»), and never for a customer or another role.
  Prices found online are said as prices online, not the shop's. Offline:
  «النت فاصل دلوقتي، فمقدرش أدوّر».

## «أبلغك أول ما ييجي؟» — the sale that waits for the part (round 16)

`robot/restock.py` + `RobotRestockRequest` (migration `0010`). A part that's
out, with nothing to offer instead (no equivalent on this shelf, not in
another branch): «… لو تحب أبلغك أول ما ييجي، قولي رقم موبايلك.» The number
said next — «010 1234 5678», «+20 10…», even «صفر واحد صفر…» — is kept, linked
to the customer with that number (never the shared «عميل نقدي»), and never
said back out loud; «أيوه» first gets «قولي رقم موبايلك», a wrong number
«قوله تاني». Saying it twice is one request.

When the part is back in the branch — checked on every heartbeat, so a
purchase, a transfer or a count that raised the stock any way is seen — the
requests turn `arrived`: an alert, the robot tells the staff («وصل X، وفيه 2
عميل مستنيه»), the morning briefing counts them, and the customer hears «X
اللي سألت عليه وصل!» at their next visit. The robot's page «📲 مستنيين أصناف»
(`/robot/restock/`) has each one's WhatsApp message ready (wa.me, with the
retail price) and «اتبلّغ» once sent.

Offline the brain asks and keeps the number the same way (`brain/talk.py`),
in `~/.mouss-brain/outbox.jsonl` (`brain/outbox.py`), and sends it to
`/sync/push/` (kind `restock`) when the net is back — applied once, however
often it's sent.

## The counter page — cash sales, expenses and counts with the net down (round 17)

«مخ موس» serves a small page on the shop's Wi-Fi (`robot/brain/counter.py`,
`http://<mini PC>:8765`, brain.ini `[counter]`). An employee picks their name
and types the PIN a manager set on the robot's page («🔢 أرقام الكاشير»,
`RobotStaffPin`, migration `0011`; only the hash is kept, in Django's PBKDF2
format — `robot/brain/pins.py`, shared by both sides). The PIN is never said
out loud: that's why it's a page and not a voice command. The rules are the
owner's:

| | Who (role) | Rule |
|---|---|---|
| 🧾 cash sale | `sale` | cash only; the catalog's retail price; never more than what's left — the catalog's stock minus what was sold here since (`brain/ledger.py`); one receipt (`OFF-…`) |
| 💸 expense | `expense` | recorded at once; the manager reviews / edits it |
| 📦 count | `stock_take` | lands in a stock-take; the manager approves the differences |

Five wrong PINs from one address wait five minutes; every form carries the
session's token; nothing is loaded from the internet. Each one goes to the
outbox and reaches `/sync/push/` as soon as the net is there — kinds `sale`,
`expense`, `count` — applied once per `client_uid` by `robot/offline_ledger.py`
through the ERP's own pipeline (`services.create_robot_sale_lines` — the
explicit retail price, the posting order, cash into the branch's treasury —
and `entries.record_expense`). Each becomes a `RobotEntry`. What the books
can't take as it is any more (the part was sold online meanwhile, the role
changed, the treasury can't cover the expense) is never forced and never
lost: the entry is kept as failed with «محتاجة مراجعة: …» and an alert.

Known limits: the invoice and the transaction are dated when they reach the
server (the time it happened is in the notes); the employee's face for the
page waits for the camera on the mini PC.

## «الراس يلف مع اللي بيتكلم» — the head follows whoever talks to it (round 18)

While the robot is in a conversation (called by its name in the last 20 s,
`wakename.in_conversation`), every live frame the ESP32-CAM pushes to
`/camera/frame/` is looked at on the server (`robot/headfollow.py`): dlib's face
detector — `dlib-bin`, already in the image for face matching — finds the
nearest face in 50–100 ms (the frame at 480 wide, twice the size inside: faces
up to ~1.5 m away). Seen twice in the same place (not someone passing by) and
off the middle, the head gets one short pulse toward it (`services.look_at`,
a `MotorCommandLog` the bridge takes from `/motor/pending/`); then nothing until
the head has settled (its pulse + 1 s), so it never swings back and forth. Of
two faces about as near, it stays with the one it was facing. While following,
the camera pushes every 400 ms; no conversation, no frame is looked at. No
firmware change: the camera, the bridge and the Mega do what they already did.

Off by default — the camera on the head (it turns with it), the head motor and
its limit switches (WIRING.md §5b) first. A move that brings the face no nearer
the middle (the head at its end switch, or the camera not on it) is tried twice,
then not again for 20 s.

On the server's `.env`, then `docker compose --env-file .env up -d`:

| | |
|---|---|
| `ROBOT_HEAD_FOLLOW=1` | on |
| `ROBOT_HEAD_EDGE_MS=250` | the pulse for a face at the frame's edge; a face half-way gets half |
| `ROBOT_HEAD_FLIP=1` | it turns away from the person: the camera or the relays the other way round |

Calibrating in the shop: stand at the edge of the camera's view and call it.
It turns past you → lower `ROBOT_HEAD_EDGE_MS`; it creeps up in several steps →
raise it. The same calibration serves `/look/` and the greet flow.


## «عربية الزبون» and the rest of what customers say (round 19)

**The customer's car stays with the conversation.** «عندكم قطع غيار بي ام؟» →
the shop's own reply; «طيب عندك طرمبة مية؟» right after offers the BMW pumps,
not the Mercedes one; once they take the F30's, «والفلتر بتاعها؟» is the F30's
filter. The car is heard in any sentence — a make («بي ام»، «مرسيدس»، «لميني»)
or a chassis («E90»، «F30») — or taken from the part they picked
(`brain/talk.py`: `car_in`، `car_of`، `fit`; kept in `robot/memory.py`). It only
breaks a tie: the words said pick the part first, and a car said again replaces
it. Server and «مخ موس» offline share the file, so they say the same words
(`robot/tests/test_conversation_car_db.py` asks both a whole conversation).

**A new customer starts over:** a greeting («السلام عليكم»), a goodbye
(«مع السلامة»), «يا موس» on its own after 30 s of quiet, or the camera greeting
someone (`/customer/greet/`) after 30 s of quiet.

**What isn't about a part** (`talk.small_talk`), the same with the net down
(the clips are in `talk.PHRASES`): greetings («صباح الخير» → «صباح النور»)،
«ازيك؟»، «شكراً يا باشا»، «إنت مين؟» (its name from the robot's page)، «بتعرف تعمل
إيه؟»، «ثانية واحدة»، «قول تاني» (what it said last)، «هاخدها / لفّهالي» (→ the
cashier)، «عايز أكلم حد». «السلام عليكم، عندك …؟» gets «وعليكم السلام،» and the
answer. A reply the owner wrote for the same words («🗨️ الردود الجاهزة» or
taught) is said instead.

**More of how it's asked about the part just named:** «عاملة كام؟»، «لسه
موجودة؟»، «صيني ولا ياباني؟»، «تمشي على F30؟»، «فيه أوفر؟»، «وريني»، and any of
it with «يا باشا / يا ريس / يا هندسة». «أيوه» to «تقصد X؟» takes X.

**«🗨️ الردود الجاهزة»:** new topics — the cars the shop works on, original or
not, installation, discounts, ordering a part, wholesale, the invoice, the
checkup — and each topic's usual Egyptian questions (`robot/replies.SUGGESTED`)
are answered by the reply the owner wrote for it, without retyping them. A topic
nobody wrote still says nothing made up.

## Offline learns from online, part numbers read aloud, and «🩺 صحة الخدمات» (round 20)

**Whole sentences learned from the teachers** (`robot/teachers.sentences`,
`brain/heardfix.sentence`): when Vosk writes the same words twice and the
teacher (Azure, Google, or — the last one — Gemini) hears the same words in them
each time, the pair goes out with the catalog (`heard_sentences`). «مخ موس»
then writes what the teacher heard and answers **at once, with no teacher** —
from its own catalog, net up or down; when the teacher heard exactly what Vosk
wrote, Vosk was right, and that sentence is answered at once too. Two
different hearings teach nothing, a number never (`ميتين` for `مية` is money),
and the owner turns a wrong one off on «🎓 تعلّم السمع». Gemini teaches when
Azure and Google can't, with the branch's part names in its prompt
(`ROBOT_GEMINI_TEACH_MINUTES`, 600 a month); when the server hears with Gemini
it gets the same names. The teachers share 6 s together (`TEACH_BUDGET_S`) —
Azure hanging and then Google used to take 12 s of the robot's 15. Letters an
Egyptian says alike (ط/ت، ث/ت، ص/س …) count as alike when a word is learned:
«سلطة الزيت» is «فلتر زيت». `robot/tests/test_learning_e2e_db.py` runs the
brain's own loop and client against these views: taught twice, learned, then
answered alone — online and offline.

**Part numbers read aloud** (`brain/arabic_numbers.spoken_codes`): digit by
digit («واحد واحد اربعة اتنين سبعة…») or in their printed groups («حداشر اتنين
واربعين سبعة تسعمية تلاتة وخمسين مية خمسة وعشرين»), Egyptian or news Arabic,
found by the server and by the brain offline alike; a count said right before
one («بعت اتنين واحد واحد اربعة…») stays a count; an OEM number read off the old
part's box finds the part too. A part number is never read as a count
(«بعت 11427953125 فلتر زيت»), «صفر عشرة …» is a mobile, and a long number is
*said* digit by digit in Egyptian («واحد واحد أربعة…»), not as billions.

**«🩺 صحة الخدمات»** (`robot/health.py`): every call to Azure (hearing, voice),
Google and Gemini notes how it went; the dashboard's card says which one isn't
answering, why (`429`, `401`, `404`, a timeout) and what to do — «فعّل الفوترة»,
«راجع المفتاح» — with the minutes left. «مخ موس» asks `/status/` when it starts
and when the net comes back and says it in its log, with «a newer brain is on the
server» when there is one; the robot's card says «🆕 فيه مخ أحدث». `python -m
brain doctor` (`run_windows.bat doctor`) checks everything the robot needs and
says what to do about each thing that isn't right. `ROBOT_REPLY_MODEL` picks the
Gemini model asked first for its answers; `gemini-flash-lite-latest` joined the
models tried (its own free quota). The teachers' recordings are kept
`ROBOT_HEARD_AUDIO_DAYS` (120; their words for ever) — they filled the disk.

## It hears what makes sense, asks when it isn't sure, and answers at once what it knows (round 21)

**The hearing that makes sense in the shop** (`robot/teachers._likeliest`): Azure
answers with its NBest and Google with its alternatives — when the likeliest
means nothing here and the next is a part, one of the shop's replies, a command
or the books (`robot/views._understood`, reads only), and isn't much less sure
(0.25), the next is what was said, and what's learned from.

**Asked again, not guessed at:** a teacher under 0.45 sure, and nothing here
making sense of what it heard, gets «معلش، مسمعتكش كويس. ممكن تقولها تاني؟» —
the AI isn't asked to answer a sentence nobody said (and the answer comes sooner).
A part it could be is still answered. Offline, the same when Vosk wasn't sure.

**The words its ears write right** (`robot/teachers.trusted_words`,
`brain/heardfix.trusted`): from what the teachers heard, each word Vosk wrote
the way the teacher heard it 8 times or more, wrong at most 2% of the time —
never a number, and none at all while Vosk leaves out more than a tenth of
what's said. They go out with the catalog (`heard_trusted`); a sentence made only
of them is answered at once by «مخ موس», a new one too, and every fifth is heard
by a teacher behind the answer. The catalog's lessons are worked out once per
change in what the teachers heard (`teachers.lessons`), not every minute for
every robot. `robot/tests/test_learning_e2e_db.py` runs it end to end.

**«🎯 جودة السمع»** on «🎓 تعلّم السمع»: the share of the words the teachers heard
that the robot's own ears got right this week and the week before, the sentences
and words it knows by heart, and the share of questions answered at once with no
teacher (the brain says which: `known` → `RobotVoiceInteraction.payload.heard_as`).


## The sound goes up while it's said, and the shop's own hearing model (round 22)

**«إرسال الصوت أول بأول»** (`robot/consumers.py`, `ws/robot/voice/`; the brain's
`brain/live.py`): over `/voice/` the recording went up only once the customer
had stopped, and a teacher started hearing it only then. Now «مخ موس» keeps a
socket open to the server (the device token in its `X-Robot-Token` header, never
the URL) and sends each sentence's sound — µ-law, a quarter second at a time —
while it's being said; `end` answers it with the same turn as `/voice/`
(`views._voice_turn`) and the spoken reply comes back on the socket. When a
teacher is to hear it, Azure hears it as it comes (`teachers.AzureStream`, a
chunked request), so its words are ready a moment after the last one:
- once the sentence has the gaps words have (a fan's or a radio's steady sound
  never reaches it, nor costs its minutes) — when the server listens there, a
  conversation's on, or Vosk has heard the robot's name while it's said
  (`teach`, Azure then hears it from its start): no minutes on the shop's chatter;
- a sentence let go of (answered on the robot, not for it) is cut short, and the
  seconds Azure heard are counted against the month's minutes all the same;
- a live hearing that fails — refused, cut off, or nothing heard where the whole
  recording has words — gives way to the whole recording, as before, and is left
  off for 6 h; once one has heard words, its «nothing heard» is believed.

The brain's microphone loop only queues the sound (another thread sends it): a
slow line never holds the microphone, and a line slower than the speaking (3 s
behind) sends that sentence over `/voice/`. No socket — a server not updated
yet, the net down, websocket-client missing, a dropped socket mid-sentence — and
the sentence goes over `/voice/` with the whole recording, as before. 60 sentences
a minute per robot at most; no channel layer (Redis down doesn't take the voice).
`[server] live = no` in brain.ini turns it off; `run_windows.bat doctor` says
whether the server takes it. `robot/tests/test_live_voice_e2e_db.py` runs the
brain's own socket against the server's own consumer.

**«📦 بيانات تدريب موديل المحل»** on «🎓 تعلّم السمع» (managers and the owner):
every recording a teacher heard, with the words it heard, in one zip —
`audio/<id>.wav` (16-bit PCM, 16 kHz), `metadata.csv` (the robot's words, the
teacher's, how sure, seconds, when, which robot) and a README — streamed, for
training the shop's own model (Whisper or Vosk fine-tuned on its customers'
Egyptian Arabic, its part names, its noise). English guesses and rows without a
recording are left out. The recordings are kept `ROBOT_HEARD_AUDIO_DAYS` (120);
0 keeps them for good. They're the customers' voices: not to leave the business.

Also: «model = vosk-big» sends its questions to the teachers too (only «vosk» did).

## From the shop's log: the guided invoice, the staff called, a conversation that stays (round 23)

**«اعملي فاتورة مبيعات» step by step** (`robot/wizard.py`): it went to the AI, which
promised an invoice the robot couldn't make. Now the robot asks — the customer (or
«نقدي»), each part with its retail price («فلتر زيت، الحتة بـ 450 جنيه، حتة واحدة —
أضيفها؟»), another part?, cash or credit — then entries' own «أسجله على أنهي فرع؟».
Every money rule is entries': retail prices, no credit on «عميل نقدي» (a name said
for it goes on the invoice's notes), the branch's cash treasury. A purchase (the
vendor, what each part cost) and an expense (how much, on what) the same way.
Only staff: the face the camera knows, or — no camera — the counter page's PIN,
said once and remembered 10 minutes; what's said for it is never kept (the
interaction log, the teacher's row, the memory). «الغي» stops it anywhere.

**The staff called, for real:** a reply that hands over to the staff («هسأل لك حد من
زمايلي»، «اسأل حد من الموظفين») ends with «يا موظف! يا موظف! يا موظف!», and the
dashboard's alerts say what the customer wanted — once a minute at most.

**A conversation that stays:** the brain says when a conversation's on (`talking`),
by its own clock — from when it was done speaking — so an answer that came late
doesn't leave the next sentence «مش ليا». The name said last («الموظفين يمس») is
its name. «أقصد، الأولاني سعره كام؟» picks the first part offered. A sentence that
starts in Arabic is answered in Arabic, whatever English words Azure wrote into it.

**Sooner, and said where the time went** (brain-0.9.6): «ثانية واحدة، بشوفلك» is
cut short the moment the answer is here (it held every slow answer 2–3 s); the
socket's answer never waits behind another request; the log says `up / answer /
voice … KB in … s`, and why a socket dropped. Azure hears live only a sentence that
lasts (a cough never) and one the robot doesn't already know (trusted words, a
learned sentence); a proxy's 502 during a deploy is tried again in seconds.

**From the review:** the training zip streams under daphne (Django 4.2 collected a sync
iterator whole) and keeps its file name with an Arabic shop name; its card counts what
the zip holds; a let-go live hearing is billed after the reply, on every path; live
hearings are capped per robot like sentences; JSON numbers that aren't (`Infinity`)
are refused instead of closing the socket.

## The teachers' minutes on a ledger, and the review of round 21 (round 24)

**«⏱️ استهلاك المدرّسين»** (`RobotTeacherUsage`, migration 0016): seconds a teacher billed
with nothing kept of them — a live hearing let go, a PIN said, a try that failed — are
rows of their own, not empty «🎓 تعلّم السمع» rows. `teachers.used_seconds` adds both, so
the month's budget holds; the quality card says how many minutes went so, and the admin
lists them. A PIN's digits as the teacher heard them are used now (`usable` refused
digits, so Vosk's were taken instead).

**From the review of round 21** (brain-0.9.7):
- «يا موس عندك» (Vosk lost the part) was all «trusted» words and skipped the teacher: a
  sentence of them must ask *about* something (`heardfix.ASKING` aside).
- Google's hearings without a confidence: only the very next one may replace the first.
- A teacher unsure of a sentence the robot answered itself (learned, trusted) no longer
  makes it say «مسمعتكش» after a right answer; an English question isn't asked again
  because an ar-EG teacher is unsure of it.
- The server keeps what the ears wrote (`heard`), not a lesson's words — trust was circular.
- A recheck rides only an answer given here (never holds a server answer for a teacher),
  and only a sentence said to the robot (staff chatter isn't uploaded).
- A whole sentence the owner turned off is never answered at once by trusted words
  (`heard_off` in the catalog); offline, a learned sentence isn't «مسمعتكش».
- `lessons()` reads the teachers' pairs once; the hearing page reads the lessons kept.

**From the shop's log of brain-0.9.6** (brain-0.9.8):
- The PIN step forgave nothing: «طرق مبيع» (a misheard «فاتورة مبيعات») was a wrong PIN, three
  of those cancelled the invoice, and the real PIN said next («The رقم 9988.») went to the AI.
  Words without four digits aren't a try now (re-asked; three of them let it go), «واحد» and
  «صفر» are digits, «فاتورة مبيعات» said again begins again (its wrong tries kept), and six
  wrong PINs on one robot lock the PIN out for 15 minutes (`PIN_LOCK_TRIES`).
- «عايز أعمل فاتورة» (which one not said; Vosk writes «عمل» without its alef) asks «مبيعات ولا
  مشتريات؟».
- Azure wrote «Yeah most anna. Murderer. وعايز أعمل فاتورة.» where Vosk had the sentence right:
  an Arabic sentence half-guessed in English never stands against Arabic ears
  (`teachers.usable(text, against)`) — nor teaches a lesson, nor goes in the training zip. A
  sentence with two Arabic words in a row is answered in Arabic.
- «voice lost»: the reply's words came, its voice (78–123 KB) didn't on a bad line, and the
  robot was silent 38–55 s. The brain keeps the replies as they were voiced
  (`voicebank.ReplyVoices`, the 300 said lately) and tells the socket which (`have`); the
  server sends those as words alone (`"voice": "kept"`, no TTS either). A voice still lost is
  fetched as µ-law with `SPEAK_S` to come, not PCM with 20 s.
- The PIN said just after a PIN step was let go of was kept (log, «🎓 تعلّم السمع»): what's
  said for `ANSWER_S` after one is never kept either.

## Pieces into the stock by voice, and a guided invoice you can walk out of (round 25)

**«ضيف 3 فلتر زيت للمخزون»** (`wizard.py`, kind `stock`, the `intake` right — «إدخال بضاعة»):
- A part the ERP has: «فلتر زيت: عندك منه 5 في الفرع ده. أزود 3 حتت تبقى 8؟». The count may
  come first or last («بوجيه 2»), or be asked («تزود كام حتة؟»).
- One it hasn't: «أضيفه صنف جديد؟» → its part number («مفيش»: a `ROBOT-…` one) → its price →
  what it cost («مش عارف»: 0) → how many → «صنف جديد: …، أسجله؟». A number the ERP already has is
  that part.
- Several parts, then «أسجله على أنهي فرع؟» (`RobotEntry` kind `stock`, migration 0017 — choices
  only). `entries.record_stock` goes through the ERP's own `set_branch_stock`, as the product
  screen's edit does: the movement, the average cost weighed with what the pieces cost, and the
  ledger (a part's first pieces as opening stock, more of one as «فروقات جرد»). No money moves —
  pieces bought from a vendor are «فاتورة مشتريات».

**From the shop's log of brain-0.9.8:**
- «الغي الفاتوره واعمل لي… حط لي منتج في المخزون» at «من أنهي مورد؟» was looked up as a vendor
  («مش لاقي مورد اسمه لا اعملها لي، …» — 254 KB of voice), and so were «شكرا» and «خلاص، مش عايز
  حاجة». «الغي» anywhere in the sentence stops it — or begins what else was asked for with it;
  «شكرا» to a step waiting for a name ends it; a sentence is never read back as a name; three
  names or parts not found let it go.
- «إدخال البضاعة بالصورة» (`services.intake_part`, `/intake/`) wrote the quantity straight in: no
  ledger entry, no average cost, an unexplained «manual edit» movement. It goes through
  `set_branch_stock` too now (reference `RobotIntake`).

## A socket the line lost, an apology, and a voice that wasn't down (round 26, brain-0.9.9)

From the shop's log of brain-0.9.8:
- The line had lost the live socket — nothing at all came back. The `end` went into it, «no
  answer in time» 15 s later, and the robot answered offline («مفهمتش») while `/voice/` worked;
  the drop itself was noticed only after 45 s. Now nothing heard for `PROBE_S` (8 s) and the
  socket is pinged (a websocket ping: the server's protocol layer answers it even while a turn is
  thinking); no pong in `PONG_S` (5 s) and it's let go of — a sentence waiting on it goes over
  `/voice/` with its recording (`LiveDown("the socket went quiet")`), and a new socket is made.
- «معلش»، «أعذرني»، «آسف» are small talk now (`talk.NEVER_MIND`, «ولا يهمك، تحت أمرك.»): answered
  at once, here and on the server — not 7–16 s on the AI.
- Azure's voice answering 200 with no sound for one sentence showed «Azure — الصوت ✗ 200»: that
  sentence goes to edge, the voice stays ✓.

## «📱 كلّم موس»: the robot from a phone (round 27)

`/robot/talk/` (a button on the robot's page): the owner — anyone on the staff — opens it on
their phone and talks. The phone hears them (the browser's own speech recognition, ar-EG: no
minutes spent), or records and the server hears it (ffmpeg → Azure, then Gemini); the server
answers exactly as it answers the robot in the shop (`views._voice_turn`, no name needed), and
the phone says it in the robot's voice (`talk/say/`, the browser's own voice when there's none).
«محادثة مستمرة» opens the mic again after each answer.

Signed in, they're known (`robot_employee` on the turn): a sale, a purchase, stock added, an
expense are asked step by step by their role, no PIN. The phone *is* the branch's robot — its
conversation, memory, log and learning (only a shop with no robot yet gets a phone robot of its
own, «phone-<user id>», never listed) — and never calls «يا موظف» (the one asking is the staff).
30 sentences a minute at most.

«🎓 علّم الروبوت» (on by default, remembered on the phone): every sentence goes up as a recording
— it stops by itself a second after they stop talking — Azure hears it, and it's kept with its
words (`teachers.hear(keep_audio=True)`): a sample to train the shop's own hearing on, in «🎓 تعلّم
السمع»'s download. Off, the phone's own speech recognition hears it: instant, free, teaches nothing.

## From the shop's log of brain-0.9.9: small talk to an open invoice, and one invoice per channel (round 28)

- An invoice left at «اسم العميل إيه؟» took «إزيك»، «بقول لك إزيك»، «شكرا يا أبو» for customers'
  names («مش لاقي عميل متسجل اسمه إزيك…»), sentence after sentence. Small talk to a step waiting
  for a name or a number is answered and the question asked again (`wizard._small_talk`); «شكرا»
  / «مع السلامة» end it; a «لا» or «تمام» that answers the step stays the step's. Three names not
  found let it go. («أبو»، «بقول لك»، «ليك» are said around small talk: `talk._POLITE`.)
- The phone and the robot being one robot, an invoice begun on «📱 كلّم موس» and left open was
  answered by the shop's customers. An invoice is answered on the channel it was begun on only
  (`via`: the robot, or the phone) — elsewhere a sentence is answered as usual.

## The robot talks shop — with the system's own repair coach (round 29)

The system has a repair coach already: Repair Atlas's «أسطى Mouss Tec»
(`repair_atlas/services/repair_coach.py`) — a workshop master's way of talking, its honesty rules
(no invented connector numbers, torques or manuals), the shop's slang («مكنة الإزاز» isn't the
engine), and the shop's verified knowledge (`VerifiedKnowledge`). `robot/workshop.py` lets the
robot talk with it:
- What's about how a car works or is fixed — wiring, a part's place, fitting and removing,
  BMW/MINI upkeep, a symptom («بتسخن ليه؟») — is answered with the coach's own system prompt, its
  mode (wiring / locate / install / disassembly), the customer's car (robot/memory.car), the
  shop's verified answer when there is one, then how to say it aloud: two to four short
  sentences, a step at a time, «أكمّلك؟», no markdown, no diagrams, no prices, never «هسأل حد».
- «ازاي أغير فلتر الزيت؟» is the coach's before the stock («أيوه، فلتر زيت موجود» isn't an answer
  to it); «عندك فلتر زيت؟» stays the shop's; «فلتر الزيت فين؟» (the shelf, maybe) stays the stock's.
- 🐛 A whole sentence with no part in it («إيه رأيك في الجو النهارده؟») was cut short with «تحت
  أمرك، محتاج قطعة إيه؟»: general talk gets the robot's own answer now (allowed to be useful);
  «عندك» alone (Vosk lost the rest) is still asked.

## From the shop's log of brain-0.9.9 (brain-0.9.10)

- «موس» answered here offline («أيوه، تحت أمرك»), the net back, and the next sentence («العربية
  موتور إن توينتي») was «مش ليا» on the server: an answer said offline opens the conversation
  (`_talked`) as one said online or here does.
- Azure wrote «عربية إف سيتي 2015 موتور الإن تونتي» for «F30, N20»: «سيتي»/«تونتي» are its
  «thirty»/«twenty», an engine said aloud («إن تونتي» → N20) is read, and «… موتور N20» is the car
  (`talk.only_car`), not a «موتور» to look for.
- Pieces into the stock: «طقم فانوس F45» was taken for «طقم جوان قاعدة مبرد F48 F45» (the same
  car): a part found that isn't what was said is asked about («أقرب حاجة عندي … هي دي؟ ولا أضيف
  «طقم فانوس F45» صنف جديد؟», `wizard._fits`); «منتج جديد اسمه …» anywhere in it starts that new
  part; «لا، أنا عايز …» to «تزود كام حتة؟» is another part, and three answers without a count let
  it go; «مبيعات ولا مشتريات؟» answered with a stock request begins that.

## From the shop's log of brain-0.9.10 (brain-0.9.11)

- «عندك فلتر زيت، إيه فسيتي؟»: «إف سيتي» written as one word is F30 (only English-sounding tens:
  «بعشرين» is a price); «موتور إن توينتي» alone answers «الموتور إيه؟» — the part asked before
  is answered, not the AI asked.
- Azure wrote «The facility متوريني twenty؟» where the ears had «الساتي», and the AI answered in
  English («Could you please confirm…»): one Arabic word heard stands against a half-English guess
  (`teachers.usable`), and an English answer to an Arabic question isn't said (`ai_reply`).
- «تعرف تضيفلي في مخزون منتج جديد» looked up «تعرف تضيفلي مخزون»: a new part with no name said
  is asked its name, and the name said is the new part (no «أضيفه صنف جديد؟» again); a name said
  again and again to «أضيفه صنف جديد؟» is let go after three; «شكرا ليك obama» (an English guess at
  the end) is thanks, not a part's name.

## «🧠 مخ الفهم» (robot/understand.py, brain-0.9.12)

The robot understood by rules — a pattern for each way a thing is said — and every shop log
brought sentences no rule had seen. Now each sentence is understood first, by Gemini (JSON
out), with all the robot knows at that moment: every hearing of it (its own ears and the
teacher's), what was just said, its last question, the step the wizard waits on, the
customer's car and the branch's part names. The answer is a meaning — an intent and the
sentence the robot acts on, in the words its rules read for sure («ضيف منتج جديد اسمه طقم
فانوس F45 للمخزون»، «عربيتي F30 موتور N20»، «أيوه»، «850») — never a reply. The rules still
do everything: prices from the catalog, stock and money through the wizard's steps, «أسجله؟»,
the role and the PIN. Not understood at all («unclear»): asked again, the step still waiting.

Never for a PIN, nor while a count, a branch question, a staff-photo round or a mobile number
waits on its own words, nor for the plain words («أيوه»، «لا»، «شكرا»، a number to a step).
Without a key, out of time (`TIMEOUT_S`/`BUDGET_S`), or `ROBOT_UNDERSTAND=0`: the rules alone.
`ROBOT_UNDERSTAND_MODEL`: a model asked first.

It learns in the background (RobotUnderstanding, «🧠 الفهم» in the admin):
- the same words understood the same way `LEARN_SEEN` times (two or more words, never
  otherwise, needing nothing said before them) are understood from then on without Gemini;
- those with no step waiting — a part, the car, small talk — go to «مخ موس» with the catalog
  (`heard_sentences`): understood offline too, and at once online (`known=learned`);
- every meaning is in «🎓 تعلّم السمع»'s download (`understanding.jsonl`), to train the shop's
  own model on; the page's «🧠 مخ الفهم» card counts what it understood and learned.

The robot's log says what was understood: `[MEANT] «عربيتي F30 موتور N20» (car · gemini 640 ms)`.
