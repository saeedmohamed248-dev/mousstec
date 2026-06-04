"""Live OBD ingest endpoint — mobile diagnostic app pushes scans here.

Contract (POST /api/obd/ingest/):
{
  "vin": "WBA...",                     # required, matches Vehicle.chassis_number
  "scan_type": "pre_repair",           # pre_repair | post_repair | ad_hoc
  "device_id": "android-XYZ",          # optional
  "engineer_username": "ahmed.m",      # optional, attaches engineer
  "fault_codes": ["P0171", "P0300"],
  "live_data": {"rpm": 820, "coolant_temp_c": 92, "maf_gs": 4.2}
}

Authentication: HMAC-SHA256 over raw request body using OBD_HMAC_SECRET env var,
sent in `X-OBD-Signature` header. If the secret is empty we run unsigned (dev only).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os

from django.db import transaction
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .models import (
    EmployeeProfile,
    SaleInvoice,
    Vehicle,
    VehicleDiagnosticReport,
)

log = logging.getLogger("erp.obd")

OBD_HMAC_SECRET = os.environ.get("OBD_HMAC_SECRET", "")
ACTIVE_JOB_STATUSES = ('quotation', 'in_progress', 'quality_check', 'ready')
VALID_SCAN_TYPES = {c[0] for c in VehicleDiagnosticReport.SCAN_CHOICES}


def _verify_signature(request) -> bool:
    if not OBD_HMAC_SECRET:
        return True
    sig = request.headers.get("X-OBD-Signature", "")
    mac = hmac.new(OBD_HMAC_SECRET.encode(), request.body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, mac)


def _compute_severity(fault_codes: list, live_data: dict) -> int:
    """Cheap heuristic: 10 points per DTC + 30 if coolant>110C + 20 if RPM>6500."""
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
    """Accepts JSON OBD payloads from the mobile app; matches VIN → active Job Card."""

    http_method_names = ['post']

    def post(self, request, *args, **kwargs):
        if not _verify_signature(request):
            log.warning("OBD ingest: bad signature from %s", request.META.get('REMOTE_ADDR'))
            return JsonResponse({"error": "bad_signature"}, status=401)

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

        vehicle = Vehicle.objects.filter(chassis_number__iexact=vin).first()
        if vehicle is None:
            return JsonResponse({"error": "vehicle_not_found", "vin": vin}, status=404)

        # Most-recent active Job Card for this vehicle
        job_card = (SaleInvoice.objects
                    .filter(vehicle=vehicle,
                            invoice_type='maintenance',
                            status__in=ACTIVE_JOB_STATUSES)
                    .order_by('-date_created').first())

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
                    device_id=(payload.get("device_id") or "")[:80],
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
