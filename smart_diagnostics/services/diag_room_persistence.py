"""
💾 Diagnostics Room → Job Card persistence

Closes the loop on the AI Diagnostics Room: takes a session's final
AI summary + DTC list + live-data snapshot + uploaded photos and
attaches them to a tenant-side `VehicleDiagnosticReport`, linked
(when possible) to the customer's active Job Card.

Why this matters for the business:
    The service advisor opens the Job Card on the dashboard and shows
    the customer:
        • the exact DTCs the car reported,
        • the AI's plain-Arabic explanation of the root cause,
        • the photos the technician snapped during diagnosis.
    This is the receipt that justifies the labour + parts line items.
"""
from __future__ import annotations

import base64
import logging
import re
import uuid
from io import BytesIO

from django.core.files.base import ContentFile
from django.db import transaction

logger = logging.getLogger("mouss_tec_core")

_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>image/(?:jpeg|png|webp));base64,(?P<b64>[A-Za-z0-9+/=]+)$"
)
_MAX_PHOTOS_PER_SAVE = 12
_MAX_PHOTO_BYTES = 5 * 1024 * 1024  # 5 MB per photo, post-decode
_MAX_SUMMARY_CHARS = 8000

_ACTIVE_JOB_STATUSES = ('quotation', 'in_progress', 'quality_check', 'ready')


class DiagnosticSaveError(Exception):
    """Recoverable failure — view turns this into a 4xx with a user-readable message."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _decode_data_url(data_url: str) -> tuple[str, bytes]:
    """Return (mime, raw_bytes). Raises ValueError on bad input."""
    if not isinstance(data_url, str):
        raise ValueError("not_a_string")
    m = _DATA_URL_RE.match(data_url.strip())
    if not m:
        raise ValueError("bad_data_url")
    try:
        raw = base64.b64decode(m.group("b64"), validate=True)
    except (ValueError, base64.binascii.Error):
        raise ValueError("bad_base64")
    if len(raw) > _MAX_PHOTO_BYTES:
        raise ValueError("too_large")
    return m.group("mime"), raw


def _resolve_vehicle(vin: str):
    """Returns Vehicle or raises DiagnosticSaveError."""
    from inventory.models import Vehicle

    vin = (vin or "").strip().upper()
    if not vin:
        raise DiagnosticSaveError("vin_required", "ارفع VIN السيارة عشان نربط التقرير.")
    vehicle = Vehicle.objects.filter(chassis_number__iexact=vin).first()
    if vehicle is None:
        raise DiagnosticSaveError(
            "vehicle_not_found",
            f"السيارة بالـ VIN ده مش موجودة في النظام: {vin}",
            status=404,
        )
    return vehicle


def _resolve_job_card(*, vehicle, job_card_id: int | None):
    """Explicit job_card_id wins. Otherwise auto-suggest the latest active
    job card for this vehicle. Returns SaleInvoice or None — None is OK
    (orphan report); the model FK is nullable by design."""
    from inventory.models import SaleInvoice

    if job_card_id:
        jc = SaleInvoice.objects.filter(
            pk=job_card_id, vehicle=vehicle, invoice_type='maintenance',
        ).first()
        if jc is None:
            raise DiagnosticSaveError(
                "job_card_mismatch",
                "بطاقة الإصلاح المختارة مش لنفس السيارة أو مش بطاقة صيانة.",
                status=400,
            )
        return jc

    # Auto-suggest: most recent active job card for this VIN
    return (SaleInvoice.objects
            .filter(vehicle=vehicle, invoice_type='maintenance',
                    status__in=_ACTIVE_JOB_STATUSES)
            .order_by('-date_created').first())


def list_active_job_cards_for_vin(vin: str) -> list[dict]:
    """Used by the UI's 'Save to Job Card' modal to populate the dropdown.

    Returns a small JSON-friendly list. Empty list if the VIN is unknown —
    the UI will then offer to save as an orphan (no job_card_id)."""
    from inventory.models import SaleInvoice, Vehicle

    vin = (vin or "").strip().upper()
    if not vin:
        return []
    vehicle = Vehicle.objects.filter(chassis_number__iexact=vin).first()
    if vehicle is None:
        return []
    rows = (SaleInvoice.objects
            .filter(vehicle=vehicle, invoice_type='maintenance',
                    status__in=_ACTIVE_JOB_STATUSES)
            .select_related('customer', 'branch')
            .order_by('-date_created')[:10])
    return [{
        "id": r.id,
        "label": (
            f"JC #{r.id} · {r.get_status_display()} · "
            f"{r.customer.name if r.customer_id else '—'} · "
            f"{r.date_created:%Y-%m-%d}"
        ),
        "status": r.status,
        "branch_id": r.branch_id,
        "customer_name": r.customer.name if r.customer_id else None,
    } for r in rows]


def save_diagnostic_session(
    *,
    vin: str,
    dtcs: list[str],
    live_data: dict,
    ai_summary: str,
    photos: list[str],
    job_card_id: int | None = None,
    scan_type: str = 'ad_hoc',
    engineer_profile=None,
    created_by_user=None,
):
    """Atomic write of one diagnostic session into the tenant DB.

    Returns:
        {
          "report_id": int,
          "job_card_id": int | None,
          "photos_saved": int,
          "photos_skipped": int,
        }
    Raises DiagnosticSaveError on user-correctable failures.
    """
    from inventory.models import (
        VehicleDiagnosticReport, VehicleDiagnosticPhoto,
    )

    vehicle = _resolve_vehicle(vin)
    job_card = _resolve_job_card(vehicle=vehicle, job_card_id=job_card_id)

    # Trim summary to a sensible upper bound — the UI shouldn't ever exceed
    # this, but a malicious payload could.
    summary = (ai_summary or "").strip()[:_MAX_SUMMARY_CHARS]

    # Normalise DTCs: upper, deduped, max 40 codes.
    norm_dtcs = []
    seen = set()
    for c in (dtcs or []):
        c2 = str(c).strip().upper()
        if c2 and c2 not in seen:
            seen.add(c2)
            norm_dtcs.append(c2)
        if len(norm_dtcs) >= 40:
            break

    # Sanity: cap photos.
    photo_inputs = (photos or [])[:_MAX_PHOTOS_PER_SAVE]

    with transaction.atomic():
        report = VehicleDiagnosticReport.objects.create(
            job_card=job_card,
            vehicle=vehicle,
            engineer=engineer_profile,
            scan_type=scan_type if scan_type in {
                c[0] for c in VehicleDiagnosticReport.SCAN_CHOICES
            } else 'ad_hoc',
            fault_codes=norm_dtcs,
            live_data=live_data or {},
            device_id='diag_room_web_bluetooth',
            source=VehicleDiagnosticReport.SOURCE_DIAG_ROOM,
            vin_snapshot=(vin or "").strip().upper()[:17],
            ai_summary=summary,
            created_by=created_by_user,
            raw_payload={
                "saved_from": "diagnostics_room",
                "snapshot_keys": list((live_data or {}).keys()),
            },
        )

        saved = 0
        skipped = 0
        for idx, data_url in enumerate(photo_inputs):
            try:
                mime, raw = _decode_data_url(data_url)
            except ValueError as exc:
                logger.warning(
                    "[Diag Save] skipping photo %s: %s", idx, exc,
                )
                skipped += 1
                continue
            ext = {'image/jpeg': 'jpg', 'image/png': 'png',
                   'image/webp': 'webp'}[mime]
            fname = f"diag_{uuid.uuid4().hex[:10]}.{ext}"
            VehicleDiagnosticPhoto.objects.create(
                report=report,
                image=ContentFile(raw, name=fname),
                caption='',
            )
            saved += 1

    logger.info(
        "[Diag Save] tenant=%s user=%s report=%s job_card=%s vin=%s "
        "dtcs=%s photos_saved=%s photos_skipped=%s",
        getattr(vehicle, '_state', None) and vehicle._state.db,
        getattr(created_by_user, 'username', None),
        report.id, getattr(job_card, 'id', None), vin,
        len(norm_dtcs), saved, skipped,
    )

    return {
        "report_id": report.id,
        "job_card_id": job_card.id if job_card else None,
        "photos_saved": saved,
        "photos_skipped": skipped,
    }


# =====================================================================
# 💼 Diagnostic → Invoice Pipeline
# =====================================================================
#
# What this does (and why):
#   The mechanic opens the Diagnostics Room, scans the car, sees DTCs +
#   sensor sweep results. With one click he can now PROPOSE billable
#   service items to the accountant. Each DTC becomes one
#   SaleInvoiceServiceItem with is_billable=False — the accountant later
#   ticks the items the customer agrees to pay for. This keeps the
#   diagnostic record (truth) separate from the invoice (decision).
#
#   Designed against the existing models (no schema changes):
#       • SaleInvoice           → the "Job Card" (invoice_type='maintenance')
#       • SaleInvoiceServiceItem → individual proposed services
#       • ServiceCatalog         → catalogue auto-populated on first use
#       • VehicleInspection      → inspection report attached to invoice
#       • VehicleDiagnosticReport → the diagnostic log (separate audit)

_INSPECTION_HEALTH_BUCKETS = (
    (85, 'green'),   # 85+ → ممتاز
    (60, 'yellow'),  # 60-84 → يحتاج متابعة
    (0,  'red'),     # <60 → تغيير فوري
)


def _health_score_to_color(score: int | None) -> str:
    if score is None:
        return 'yellow'
    for threshold, color in _INSPECTION_HEALTH_BUCKETS:
        if score >= threshold:
            return color
    return 'red'


def _resolve_default_branch(*, user, vehicle):
    """Pick a sensible Branch for a new SaleInvoice."""
    from inventory.models import Branch

    # 1. The user's EmployeeProfile.branch (most accurate)
    try:
        profile = getattr(user, 'employeeprofile', None)
        if profile and getattr(profile, 'branch_id', None):
            return profile.branch
    except Exception:
        pass

    # 2. First active branch (fallback for newly-provisioned tenants)
    branch = Branch.objects.first()
    if branch is None:
        raise DiagnosticSaveError(
            "no_branch", "مفيش فرع مسجّل في النظام — أضف فرع أولاً.",
            status=400,
        )
    return branch


def attach_diagnostic_to_invoice(
    *,
    vin: str,
    dtcs: list[str],
    sensor_sweep: dict | None = None,
    health_score: int | None = None,
    freeze_frame: dict | None = None,
    readiness: dict | None = None,
    manufacturer_dids: dict | None = None,
    emissions_health: dict | None = None,
    ai_summary: str = "",
    job_card_id: int | None = None,
    created_by_user=None,
    diagnostic_fee: float = 100.00,
):
    """Attach diagnostic findings as proposed billable items on a Job Card.

    Workflow:
      1. Resolve Vehicle + Customer (vehicle must exist in CRM).
      2. Use existing Job Card (job_card_id) or create a new one in 'quotation'.
      3. For each DTC, create a SaleInvoiceServiceItem (is_billable=False).
      4. Add one "فحص حساسات شامل" service item with the diagnostic fee.
      5. Create/update VehicleInspection with health-score-derived status.
      6. Persist the diagnostic log via save_diagnostic_session() for audit.

    Returns:
        {
          "invoice_id": int,
          "items_created": int,
          "inspection_id": int,
          "report_id": int,
          "invoice_admin_url": str,  # for the success modal link
        }
    """
    from inventory.models import (
        SaleInvoice, SaleInvoiceServiceItem, ServiceCatalog,
        VehicleInspection,
    )
    from decimal import Decimal

    if not created_by_user or not getattr(created_by_user, 'is_authenticated', False):
        raise DiagnosticSaveError(
            "auth_required", "لازم تسجل دخول قبل إرسال التشخيص للفاتورة.",
            status=401,
        )

    vehicle = _resolve_vehicle(vin)
    customer = vehicle.customer
    if customer is None:
        raise DiagnosticSaveError(
            "no_customer",
            "السيارة دي مش مربوطة بعميل في CRM. اربطها بعميل أولاً.",
            status=400,
        )

    # Normalise + dedupe DTCs (same logic as save_diagnostic_session).
    norm_dtcs = []
    seen = set()
    for c in (dtcs or []):
        c2 = str(c).strip().upper()
        if c2 and c2 not in seen and len(c2) <= 10:   # P0301 = 5 chars
            seen.add(c2)
            norm_dtcs.append(c2)
        if len(norm_dtcs) >= 40:
            break

    with transaction.atomic():
        # ── 1. Resolve or create the Job Card (SaleInvoice) ──────────────
        if job_card_id:
            invoice = SaleInvoice.objects.filter(
                pk=job_card_id, vehicle=vehicle, invoice_type='maintenance',
            ).first()
            if invoice is None:
                raise DiagnosticSaveError(
                    "job_card_mismatch",
                    "بطاقة العمل المختارة مش لنفس السيارة.",
                    status=400,
                )
        else:
            branch = _resolve_default_branch(user=created_by_user, vehicle=vehicle)
            invoice = SaleInvoice.objects.create(
                invoice_type='maintenance',
                status='quotation',
                customer=customer,
                vehicle=vehicle,
                branch=branch,
                notes=f"تم الإنشاء تلقائياً من غرفة التشخيص — {len(norm_dtcs)} كود عطل.",
            )

        # ── 2. Bulk-create service items (is_billable=False by default) ─
        items_to_create = []
        # Each DTC → one ServiceCatalog row (auto-created on first sight)
        for code in norm_dtcs:
            svc, _ = ServiceCatalog.objects.get_or_create(
                name=f"تشخيص وإصلاح {code}",
                defaults={'labor_price': Decimal('0.00'),
                          'estimated_hours': Decimal('0.5')},
            )
            items_to_create.append(SaleInvoiceServiceItem(
                invoice=invoice, service=svc,
                is_billable=False,
                billing_note='تلقائي — في انتظار قرار المحاسب',
                actual_hours=Decimal('0.5'),
            ))

        # Sensor sweep fee — only if a meaningful sweep was performed
        if sensor_sweep:
            svc, _ = ServiceCatalog.objects.get_or_create(
                name="فحص حسّاسات شامل بالكمبيوتر",
                defaults={'labor_price': Decimal(str(diagnostic_fee)),
                          'estimated_hours': Decimal('1.0')},
            )
            items_to_create.append(SaleInvoiceServiceItem(
                invoice=invoice, service=svc,
                is_billable=False,
                billing_note='تلقائي — فحص بالكمبيوتر',
                actual_hours=Decimal('1.0'),
            ))

        # Use individual save() not bulk_create — model has auto-fill logic
        # in save() that bulk_create skips.
        for item in items_to_create:
            item.save()

        items_created = len(items_to_create)

        # ── 3. Vehicle inspection record ─────────────────────────────────
        status_color = _health_score_to_color(health_score)
        inspection_notes = {
            'health_score': health_score,
            'dtcs': norm_dtcs,
            'sensor_sweep': sensor_sweep or {},
            'freeze_frame': freeze_frame or {},
            'readiness': readiness or {},
            'manufacturer_dids': manufacturer_dids or {},
            'emissions_health': emissions_health or {},
        }
        inspection, _ = VehicleInspection.objects.update_or_create(
            invoice=invoice,
            defaults={
                'vehicle':            vehicle,
                'brakes_status':      status_color,
                'engine_oil_status':  status_color,
                'tires_status':       'green',  # not measurable via OBD
                'battery_status':
                    ('red' if (sensor_sweep or {}).get('idleTpsBattery', {})
                              .get('battery', {}).get('severity') == 'critical'
                     else status_color),
                'technician_notes':
                    f"درجة الصحة: {health_score or '?'}/100\n"
                    f"عدد الأعطال: {len(norm_dtcs)}\n\n"
                    f"التفاصيل (JSON):\n{_safe_json(inspection_notes)}",
            },
        )

        # ── 4. Recalc invoice total (services_total will be 0 — items not
        #    billable yet — but signals/downstream expect it called) ───
        try:
            invoice.update_total()
        except Exception as e:
            logger.warning("[Attach] update_total failed (non-fatal): %s", e)

    # ── 5. Also write the diagnostic log via the existing pipeline ──────
    # (Outside the transaction so a save failure doesn't roll back the
    # invoice — the diagnostic log is auxiliary audit data.)
    try:
        diag_result = save_diagnostic_session(
            vin=vin,
            dtcs=norm_dtcs,
            live_data={'sensor_sweep': sensor_sweep or {},
                       'freeze_frame': freeze_frame or {},
                       'readiness': readiness or {},
                       'manufacturer_dids': manufacturer_dids or {},
                       'emissions_health': emissions_health or {},
                       'health_score': health_score},
            ai_summary=ai_summary or
                       f"تشخيص تلقائي — {len(norm_dtcs)} عطل، درجة الصحة {health_score or '?'}/100",
            photos=[],
            job_card_id=invoice.id,
            scan_type='ad_hoc',
            created_by_user=created_by_user,
        )
        report_id = diag_result.get('report_id')
    except Exception as e:
        logger.warning("[Attach] diagnostic log skipped: %s", e)
        report_id = None

    logger.info(
        "[Attach] invoice=%s items=%s inspection=%s report=%s",
        invoice.id, items_created, inspection.id, report_id,
    )

    return {
        "invoice_id":        invoice.id,
        "items_created":     items_created,
        "inspection_id":     inspection.id,
        "report_id":         report_id,
        "invoice_admin_url": f"/secure-portal/inventory/saleinvoice/{invoice.id}/change/",
    }


def _safe_json(obj):
    import json
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)[:4000]
    except Exception:
        return str(obj)[:4000]
