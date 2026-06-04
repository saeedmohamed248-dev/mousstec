# DMS Backlog — Tier-1 Polish Pass

Status snapshot taken on **2026-06-04** at the end of the 5-Pillar build.
The core DMS is **live in production** and the check-in → diagnose → repair →
invoice → customer-signoff → HR-audit loop is closed. The items below are
non-blocking polish that should land before we market this as a fully audited
tier-1 system.

Owner column is intentionally blank — fill in when picked up.

---

## 1. Test coverage for the new DMS surface
**Pillar:** 1 / 2 / 3 / 4
**Risk if skipped:** Regressions like the dashboard 500 (RelatedObjectDoesNotExist) will keep slipping into production.
**Files in scope:**
- `inventory/views_tech.py`
- `inventory/views_hr.py`
- `inventory/api_obd.py`
- `inventory/signals.py` (CustomerFeedback auto-create)
- `inventory/views_lightning.py::quick_expense_create` (salary employee FK)

**Acceptance criteria:**
- pytest module per file with at least: happy path, role-forbidden path, validation failures.
- OBD endpoint: HMAC valid + invalid, unknown VIN, VIN with no active job card.
- RepairLog: start → pause → resume → complete computes correct `duration_minutes`.
- Feedback signal: only fires once when status transitions to `posted`.
- Target: green CI on `pytest -k "dms"` within ~30 minutes of work.

**Owner:** _

---

## 2. Per-device HMAC secrets for the OBD API
**Pillar:** 2
**Risk if skipped:** Leaked secret on one mechanic's phone compromises every scanner; no rotation path; no audit trail of which device sent a scan.
**Files in scope:**
- `inventory/models.py` — add `OBDDevice(name, secret_hash, owner_employee, last_seen, is_active)`
- `inventory/api_obd.py::_verify_signature` — look up by `X-OBD-Device-Id` header, compare HMAC against per-row secret.
- New admin action: rotate secret / deactivate device.

**Acceptance criteria:**
- Deactivating a device on the admin immediately rejects its next request.
- `VehicleDiagnosticReport.device_id` joins to `OBDDevice` (FK, nullable for legacy rows).
- Migration backfills any existing `device_id` strings into the new table as inactive entries.

**Owner:** _

---

## 3. Feedback link regeneration + WhatsApp resend
**Pillar:** 4
**Risk if skipped:** Customers who lose the SMS/WhatsApp link have no path to leave a rating; star scores stay artificially low.
**Files in scope:**
- `inventory/views.py` — new endpoint `POST /system/invoice/<id>/feedback/resend/` that:
  - Rotates `CustomerFeedback.public_token` (new UUID).
  - Returns a `wa.me` deep link prefilled with the new URL.
- `inventory/templates/inventory/invoice_print_a4.html` — button "إعادة إرسال رابط التقييم" visible to cashier/admin/manager.
- Lock: cannot rotate after `responded_at` is set (keeps audit integrity).

**Acceptance criteria:**
- Old URL returns 410 Gone after rotation.
- Button hidden once feedback has been submitted.
- WhatsApp deep link is `https://wa.me/<phone>?text=<urlencoded message + link>`.

**Owner:** _

---

## 4. Active geofencing on attendance check-ins
**Pillar:** 1
**Risk if skipped:** `AttendanceCheckIn.is_inside_geofence` and `flagged_reason` are dead columns; payroll cannot trust GPS data; users can clock in from home.
**Files in scope:**
- `inventory/models.py::Branch` — add `lat`, `lng`, `geofence_radius_m` (default 200).
- `inventory/views_tech.py::attendance_checkin_api` — haversine distance to employee's `branch.lat/lng`; set `is_inside_geofence` and populate `flagged_reason` (`"outside_radius"`, `"low_accuracy"`, `"no_branch_geo"`).
- `inventory/templates/inventory/hr_workspace.html` — render a red chip on rows where `is_inside_geofence=False`.

**Acceptance criteria:**
- Branch admin can drop a pin on a Leaflet map to set lat/lng/radius.
- Check-in farther than radius is saved but flagged (NOT blocked — HR decides).
- HR Workspace shows a "Flagged" filter chip.

**Owner:** _

---

## 5. Commission payout / settle action
**Pillar:** 3
**Risk if skipped:** `EmployeeProfile.commission_balance` accumulates forever; no link between accrued commission and actual disbursement; no payroll-period reset.
**Files in scope:**
- `inventory/services/commissions.py` — `settle_commissions(period_start, period_end, treasury, employee_ids=None)`:
  - For each employee with `commission_balance > 0`, create a `FinancialTransaction(transaction_type='out', category=system_key='salaries', employee=..., description='Commission settlement <period>')`.
  - Atomically zero the `commission_balance` and write a `CommissionPayout(employee, amount, transaction, period_start, period_end)` ledger row.
- New model `CommissionPayout` for the audit trail.
- Admin action "Settle commissions for selected employees" + cashier UI button (admin/manager only).

**Acceptance criteria:**
- Settling produces both a FinancialTransaction and a CommissionPayout in one DB transaction.
- Cannot settle twice for an overlapping period (unique constraint or guard).
- Treasury balance and employee balance both move correctly.

**Owner:** _

---

## 6. AuditLog coverage for Pillar 4 actions
**Pillar:** 4
**Risk if skipped:** No compliance trail for who chose the detailed-vs-summary invoice, when the customer rated/signed, or who resent the feedback link.
**Files in scope:**
- `inventory/views.py::print_invoice_a4` — log entry on each render with `{mode, invoice_id, user, ip}`.
- `inventory/views.py::customer_feedback_public` (POST handler) — log `{token, rating, received_in_good_condition, ip}`.
- `inventory/views.py::feedback_resend` (from item 3) — log rotation events.
- Reuse the existing `AuditLog` model in `inventory/models.py:544`.

**Acceptance criteria:**
- Every Pillar 4 mutation produces exactly one AuditLog row.
- Admin changelist filter by `action_type` includes: `INVOICE_PRINTED_DETAILED`, `INVOICE_PRINTED_SUMMARY`, `FEEDBACK_SUBMITTED`, `FEEDBACK_LINK_ROTATED`.
- Anonymous (public) feedback submissions are logged with `user=None` and the IP/UA recorded.

**Owner:** _

---

## Triage notes

- **None of the six is a blocker.** Production is serving real customers today on the 5-Pillar build.
- Suggested order if we get a 1-day window: **#1 (tests)** first so the rest can land safely, then **#6 (AuditLog)** because it's small and unblocks compliance review, then **#4 (geofence)** and **#5 (commission payout)** which together turn payroll into a closed loop, then **#2 (per-device HMAC)** and **#3 (feedback resend)** as customer-facing polish.
- Re-estimate after item #1 ships — test infra reveals hidden coupling.

## Out of scope (deliberately)

- Printing/B2B tenant cross-functional work (separate roadmap).
- Mobile app side of the OBD contract (owned by the mobile team).
- AI Design Generator P0 bug currently being investigated (separate context).
