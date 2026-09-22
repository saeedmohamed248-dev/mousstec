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

## Extra innovations added (وابتكر معايا)

- **Trigger-agent procurement** with an idempotent `ProcurementSignal` (one open
  signal per part/branch) that hands a ready **manifest payload** to the
  Procurement Agent — no duplicate ordering.
- **Safety interlocks in firmware**: exclusive motor direction (no dead-short),
  a comms watchdog that stops all motion on Wi-Fi/serial loss, and a pulse cap
  so a lost `stop` can't run a motor forever.
- **Self-locking arms**: worm-gear window motors hold position with zero power
  (`duration_ms=0` latches), saving current and holding load safely.
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
