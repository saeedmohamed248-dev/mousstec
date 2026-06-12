"""
🌐 REST API — smart_diagnostics
=================================
كل endpoints هنا tenant-scoped (django-tenants schema routing بـ يـ enforce العزل).
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from smart_diagnostics.services.dtc_resolver import DTCResolver, VINResolver
from smart_diagnostics.services.parts_finder import SmartPartsFinder
from smart_diagnostics.services.quota import (
    DiagnosticsQuotaService,
    FEATURE_LIVE_DATA,
    FEATURE_GUIDED_TESTS,
    FEATURE_PARTS_FINDER,
)
from smart_diagnostics.models import DiagnosticScan, FaultLog
from diagnostics_catalog.models import VehicleProtocolMemory
from django.db import connection
from django.utils import timezone


def _tenant(request):
    return getattr(request, 'tenant', None)


def _deny(gate):
    body = {
        'error': gate.reason,
        'upgrade_required': gate.upgrade_required,
        'feature': gate.feature_code,
    }
    return Response(body, status=status.HTTP_402_PAYMENT_REQUIRED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def scan_dtc(request):
    """POST { vin, dtc_code } → resolves DTC + creates a DiagnosticScan + FaultLog."""
    vin = (request.data.get('vin') or '').strip().upper()
    dtc = (request.data.get('dtc_code') or '').strip().upper()
    if not vin or not dtc:
        return Response({'error': 'vin و dtc_code مطلوبين'}, status=400)

    tenant = _tenant(request)
    gate = DiagnosticsQuotaService.check_feature(tenant, FEATURE_GUIDED_TESTS)
    if not gate.allowed:
        return _deny(gate)

    from inventory.models import Vehicle
    vehicle = Vehicle.objects.filter(chassis_number=vin).first()
    if not vehicle:
        return Response({'error': 'مركبة غير موجودة في سجلات الشركة'}, status=404)

    sig = f"{vehicle.brand}|{vehicle.model_name or ''}".strip('|')
    resolver = DTCResolver(tenant=tenant, user=request.user)
    resolved, denial = resolver.resolve(dtc, vehicle_signature=sig, allow_external=True)
    if denial:
        return _deny(denial)

    scan = DiagnosticScan.objects.create(
        vehicle=vehicle,
        technician=request.user if request.user.is_authenticated else None,
        source='api',
        status='completed',
        summary=f"{dtc}: {resolved.short_description}",
    )
    FaultLog.objects.create(
        vehicle=vehicle, scan=scan,
        dtc_code=dtc, severity=resolved.severity,
        mileage_at_detection=vehicle.last_mileage,
    )

    return Response({
        'scan_id': scan.id,
        'dtc': {
            'code': resolved.code,
            'short': resolved.short_description,
            'full': resolved.full_description,
            'severity': resolved.severity,
            'guided_steps': resolved.guided_steps,
            'likely_oem_parts': resolved.likely_oem_parts,
            'source': resolved.source,
        },
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def decode_vin(request, vin):
    tenant = _tenant(request)
    gate = DiagnosticsQuotaService.check_feature(tenant, FEATURE_GUIDED_TESTS)
    if not gate.allowed:
        return _deny(gate)
    resolver = VINResolver(tenant=tenant, user=request.user)
    cached, denial = resolver.resolve(vin)
    if denial:
        return _deny(denial)
    return Response({
        'vin': cached.vin,
        'make': cached.make,
        'model': cached.model,
        'model_year': cached.model_year,
        'engine': cached.engine,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dtc_test_plan(request, code):
    tenant = _tenant(request)
    gate = DiagnosticsQuotaService.check_feature(tenant, FEATURE_GUIDED_TESTS)
    if not gate.allowed:
        return _deny(gate)
    resolver = DTCResolver(tenant=tenant, user=request.user)
    # allow_external=False: لا نـ deduct quota لمجرد عرض خطة فحص
    resolved, denial = resolver.resolve(code, allow_external=False)
    if denial or not resolved:
        return Response({'error': 'الكود غير موجود في المرجع المحلي'}, status=404)
    return Response({
        'code': resolved.code,
        'severity': resolved.severity,
        'short': resolved.short_description,
        'steps': resolved.guided_steps,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dtc_parts(request, code):
    tenant = _tenant(request)
    gate = DiagnosticsQuotaService.check_feature(tenant, FEATURE_PARTS_FINDER)
    if not gate.allowed:
        return _deny(gate)
    matches = SmartPartsFinder.find_for_dtc(code)
    return Response({
        'code': code.upper(),
        'matches': [
            {
                'product_id': m.product_id,
                'name': m.name,
                'part_number': m.part_number,
                'oem_codes': m.oem_codes,
                'in_stock_qty': m.in_stock_qty,
                'matched_oem': m.matched_oem,
                'matched_by': m.matched_by,
            }
            for m in matches
        ],
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def vehicle_health_passport(request, vin):
    """Digital Health Passport — full fault history لمركبة."""
    tenant = _tenant(request)
    gate = DiagnosticsQuotaService.check_feature(tenant, FEATURE_GUIDED_TESTS)
    if not gate.allowed:
        return _deny(gate)
    from inventory.models import Vehicle
    vehicle = Vehicle.objects.filter(chassis_number=vin.upper()).first()
    if not vehicle:
        return Response({'error': 'مركبة غير موجودة'}, status=404)
    faults = FaultLog.objects.filter(vehicle=vehicle).order_by('-detected_at')[:200]
    return Response({
        'vin': vehicle.chassis_number,
        'brand': vehicle.brand,
        'model': vehicle.model_name,
        'plate': vehicle.car_plate,
        'mileage': vehicle.last_mileage,
        'health_score': vehicle.ai_health_score,
        'faults': [
            {
                'code': f.dtc_code,
                'severity': f.severity,
                'detected_at': f.detected_at.isoformat(),
                'resolved_at': f.resolved_at.isoformat() if f.resolved_at else None,
                'resolution_note': f.resolution_note,
                'mileage_at_detection': f.mileage_at_detection,
            }
            for f in faults
        ],
    })


# ────────────────────────────────────────────────────────────────────────
# 🧠 Protocol memory — saves ~30s per session by remembering which OBD
# protocol succeeded last time on a given vehicle. Lives in public schema
# (shared across tenants — one VIN has the same protocol everywhere).
# ────────────────────────────────────────────────────────────────────────


def _with_public_schema(fn):
    """Run a callable in the public schema (where VehicleProtocolMemory lives),
    restoring the previous schema afterward."""
    def wrapped(*args, **kwargs):
        prev = connection.schema_name
        try:
            if prev != 'public':
                connection.set_schema_to_public()
            return fn(*args, **kwargs)
        finally:
            if prev != 'public':
                connection.set_schema(prev)
    return wrapped


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def protocol_memory_lookup(request):
    """GET ?vin=...&dongle_id=... → { protocol_code, protocol_label, hit_count } or 404."""
    vin = (request.GET.get('vin') or '').strip().upper()
    dongle_id = (request.GET.get('dongle_id') or '').strip()
    if not vin and not dongle_id:
        return Response({'error': 'vin or dongle_id required'}, status=400)

    @_with_public_schema
    def find():
        qs = VehicleProtocolMemory.objects.all()
        # Prefer VIN match (most specific); fall back to dongle.
        if vin:
            hit = qs.filter(vin=vin).first()
            if hit:
                return hit
        if dongle_id:
            return qs.filter(dongle_id=dongle_id).first()
        return None

    hit = find()
    if not hit:
        return Response({'found': False}, status=200)
    return Response({
        'found': True,
        'protocol_code': hit.protocol_code,
        'protocol_label': hit.protocol_label,
        'hit_count': hit.hit_count,
        'last_used': hit.last_used.isoformat(),
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def protocol_memory_save(request):
    """POST { vin?, dongle_id?, protocol_code, protocol_label?, sweep_seconds_saved? }.

    Upserts on (vin) preferring exact VIN match, else (dongle_id)."""
    data = request.data
    vin = (data.get('vin') or '').strip().upper()
    dongle_id = (data.get('dongle_id') or '').strip()
    code = (data.get('protocol_code') or '').strip().upper()
    label = (data.get('protocol_label') or '').strip()
    seconds_saved = float(data.get('sweep_seconds_saved') or 0)

    if not vin and not dongle_id:
        return Response({'error': 'vin or dongle_id required'}, status=400)
    if code not in {'1', '2', '3', '4', '5', '6', '7', '8', '9', 'A', 'B'}:
        return Response({'error': f'invalid protocol_code: {code}'}, status=400)

    @_with_public_schema
    def upsert():
        existing = None
        if vin:
            existing = VehicleProtocolMemory.objects.filter(vin=vin).first()
        if not existing and dongle_id:
            existing = VehicleProtocolMemory.objects.filter(dongle_id=dongle_id).first()

        if existing:
            existing.protocol_code = code
            if label:
                existing.protocol_label = label
            existing.sweep_seconds_saved = max(existing.sweep_seconds_saved, seconds_saved)
            existing.hit_count += 1
            existing.last_used = timezone.now()
            if vin and not existing.vin:
                existing.vin = vin
            if dongle_id and not existing.dongle_id:
                existing.dongle_id = dongle_id
            existing.save()
            return existing, False
        obj = VehicleProtocolMemory.objects.create(
            vin=vin, dongle_id=dongle_id,
            protocol_code=code, protocol_label=label,
            sweep_seconds_saved=seconds_saved,
        )
        return obj, True

    obj, created = upsert()
    return Response({
        'created': created,
        'protocol_code': obj.protocol_code,
        'hit_count': obj.hit_count,
    }, status=201 if created else 200)
