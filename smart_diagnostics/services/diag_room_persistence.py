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
