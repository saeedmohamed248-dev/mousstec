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
