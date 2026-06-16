"""Live OBD ingest endpoint — mobile diagnostic app pushes scans here.

Authentication (per-device HMAC):
    Required headers
        X-OBD-Device-Id:  opaque device identifier
        X-OBD-Timestamp:  unix epoch seconds (string)
        X-OBD-Nonce:      random hex (>= 16 bytes), unique per request
        X-OBD-Signature:  hex(hmac_sha256(secret, f"{ts}.{nonce}.{sha256(body)}"))

    Flow
        1. Resolve OBDDevice in public schema by device_id.
        2. Reject inactive/suspended/revoked devices BEFORE crypto compare.
        3. Reject timestamps outside replay_window (default 300s).
        4. Reject nonce reuse (OBDDeviceNonce uniqueness).
        5. HMAC verify with the device's own secret.
        6. Switch DB connection to device.tenant schema, do vehicle/report work.
        7. Stamp report with device.branch_id; refuse VINs whose active Job Card
           sits in another branch (cross-branch spoofing guard).

Body contract (POST):
    { "vin": "...", "scan_type": "pre_repair", "fault_codes": [...],
      "live_data": {...}, "engineer_username": "..." (optional) }
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

from django.db import connection, transaction
from django.db.utils import IntegrityError
from django.http import JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django_tenants.utils import schema_context

from clients.obd_device_models import OBDDevice, OBDDeviceNonce
from inventory.models import (  # models still live in inventory until Phase 2B
    EmployeeProfile,
    SaleInvoice,
    Vehicle,
    VehicleDiagnosticReport,
)

log = logging.getLogger("erp.obd")

ACTIVE_JOB_STATUSES = ('quotation', 'in_progress', 'quality_check', 'ready')
VALID_SCAN_TYPES = {c[0] for c in VehicleDiagnosticReport.SCAN_CHOICES}


def _client_ip(request) -> str | None:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR")


def _auth_device(request) -> tuple[OBDDevice | None, JsonResponse | None]:
    """Return (device, None) on success, (None, error_response) on failure.

    Runs entirely in the public schema — caller must switch to device.tenant
    afterwards.
    """
    headers = request.headers
    device_id = headers.get("X-OBD-Device-Id", "").strip()
    ts_raw = headers.get("X-OBD-Timestamp", "").strip()
    nonce = headers.get("X-OBD-Nonce", "").strip()
    sig = headers.get("X-OBD-Signature", "").strip()

    if not all([device_id, ts_raw, nonce, sig]):
        return None, JsonResponse({"error": "missing_auth_headers"}, status=401)
    if len(nonce) < 16 or len(nonce) > 64:
        return None, JsonResponse({"error": "bad_nonce"}, status=401)

    # Ensure we're operating in public for the device lookup. The ingest
    # endpoint should be wired without TenantMiddleware, but defense in depth.
    with schema_context("public"):
        device = OBDDevice.objects.select_related("tenant").filter(
            device_id=device_id,
        ).first()
        if device is None or device.status != OBDDevice.STATUS_ACTIVE:
            # Same response for not-found and suspended — no enumeration oracle
            log.warning("OBD auth: unknown/inactive device %s from %s",
                        device_id, _client_ip(request))
            return None, JsonResponse({"error": "unauthorized"}, status=401)

        try:
            ts = int(ts_raw)
        except ValueError:
            return None, JsonResponse({"error": "bad_timestamp"}, status=401)
        if abs(int(time.time()) - ts) > device.replay_window_seconds:
            return None, JsonResponse({"error": "stale_timestamp"}, status=401)

        if not device.verify_signature(body=request.body, timestamp=ts_raw,
                                       nonce=nonce, signature_hex=sig):
            log.warning("OBD auth: bad signature device=%s ip=%s",
                        device_id, _client_ip(request))
            return None, JsonResponse({"error": "unauthorized"}, status=401)

        # Record nonce — uniqueness constraint is the replay guard.
        try:
            OBDDeviceNonce.objects.create(device=device, nonce=nonce)
        except IntegrityError:
            log.warning("OBD auth: nonce replay device=%s nonce=%s",
                        device_id, nonce)
            return None, JsonResponse({"error": "replay_detected"}, status=401)

        # Update telemetry — small writes, OK to do inline.
        OBDDevice.objects.filter(pk=device.pk).update(
            last_seen_at=timezone.now(),
            last_seen_ip=_client_ip(request),
            last_seen_generation=device.rotation_generation,
        )

    return device, None


def _compute_severity(fault_codes: list, live_data: dict) -> int:
    score = min(len(fault_codes) * 10, 60)
    try:
        if float(live_data.get("coolant_temp_c", 0)) > 110:
            score += 30
        if float(live_data.get("rpm", 0)) > 6500:
            score += 20
    except (TypeError, ValueError):
        pass
    return min(score, 100)


@method_decorator(csrf_exempt, name='dispatch')
class ReceiveOBDDataView(View):
    """Accepts JSON OBD payloads; per-device HMAC; tenant-routed."""

    http_method_names = ['post']

    def post(self, request, *args, **kwargs):
        device, err = _auth_device(request)
        if err is not None:
            return err

        try:
            payload = json.loads(request.body or b"{}")
        except ValueError:
            return JsonResponse({"error": "invalid_json"}, status=400)
        if not isinstance(payload, dict):
            return JsonResponse({"error": "invalid_payload"}, status=400)

        vin = (payload.get("vin") or "").strip().upper()
        scan_type = (payload.get("scan_type") or "ad_hoc").strip()
        if not vin:
            return JsonResponse({"error": "vin_required"}, status=400)
        if scan_type not in VALID_SCAN_TYPES:
            return JsonResponse({"error": "invalid_scan_type",
                                 "allowed": sorted(VALID_SCAN_TYPES)}, status=400)

        fault_codes = payload.get("fault_codes") or []
        live_data = payload.get("live_data") or {}
        if not isinstance(fault_codes, list) or not isinstance(live_data, dict):
            return JsonResponse({"error": "invalid_data_shape"}, status=400)

        # All tenant-schema work happens inside this context.
        with schema_context(device.tenant.schema_name):
            vehicle = Vehicle.objects.filter(chassis_number__iexact=vin).first()
            if vehicle is None:
                return JsonResponse({"error": "vehicle_not_found", "vin": vin},
                                    status=404)

            job_card = (SaleInvoice.objects
                        .filter(vehicle=vehicle,
                                invoice_type='maintenance',
                                status__in=ACTIVE_JOB_STATUSES)
                        .order_by('-date_created').first())

            # Cross-branch spoofing guard: if device is pinned to a branch and
            # the active Job Card is in another, refuse.
            if (device.branch_id and job_card
                    and job_card.branch_id
                    and job_card.branch_id != device.branch_id):
                log.warning("OBD: device %s (branch=%s) tried to ingest VIN "
                            "%s whose job card is in branch %s",
                            device.device_id, device.branch_id, vin,
                            job_card.branch_id)
                return JsonResponse({"error": "branch_mismatch"}, status=403)

            engineer = None
            eng_uname = payload.get("engineer_username")
            if eng_uname:
                engineer = (EmployeeProfile.objects
                            .select_related('user')
                            .filter(user__username=eng_uname,
                                    role__in=['engineer', 'tech']).first())

            try:
                with transaction.atomic():
                    report = VehicleDiagnosticReport.objects.create(
                        job_card=job_card,
                        vehicle=vehicle,
                        engineer=engineer,
                        scan_type=scan_type,
                        fault_codes=fault_codes,
                        live_data=live_data,
                        device_id=device.device_id[:80],
                        raw_payload=payload,
                        severity_score=_compute_severity(fault_codes, live_data),
                    )
            except Exception as exc:
                log.exception("OBD ingest failed: %s", exc)
                return JsonResponse({"error": "ingest_failed"}, status=500)

            return JsonResponse({
                "ok": True,
                "report_id": report.id,
                "job_card_id": job_card.id if job_card else None,
                "vehicle_id": vehicle.id,
                "severity_score": report.severity_score,
                "code_count": len(report.fault_codes),
            }, status=201)
