"""Tenant-side HTML views for the Live Diagnostics dashboard."""
import logging
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from smart_diagnostics.models import DiagnosticDevice
from smart_diagnostics.services.quota import (
    DiagnosticsQuotaService,
    FEATURE_LIVE_DATA,
)

logger = logging.getLogger('mouss_tec_core')


def _on_public_schema() -> bool:
    """True if we're serving from the public/shared schema — diagnostics
    views require a tenant context (Vehicle table only exists per-tenant)."""
    try:
        return connection.schema_name == 'public'
    except Exception:
        return False


@login_required
def live_dashboard(request, vin: str):
    # 1. Reject public-schema access early (Vehicle table doesn't exist there).
    if _on_public_schema():
        return HttpResponse(
            "هذه الصفحة متاحة فقط من نطاق الشركة (tenant). "
            "ادخل من sub-domain شركتك مباشرة.",
            status=400,
            content_type='text/html; charset=utf-8',
        )

    tenant = getattr(request, 'tenant', None)

    try:
        gate = DiagnosticsQuotaService.check_feature(tenant, FEATURE_LIVE_DATA)
    except Exception as e:
        logger.error(f"[live_dashboard] entitlement check failed: {e}", exc_info=True)
        gate = type('G', (), {'allowed': False, 'reason': 'تعذّر التحقق من الباقة', 'feature_code': FEATURE_LIVE_DATA})()

    if not gate.allowed:
        return render(request, 'smart_diagnostics/live_dashboard.html', {
            'vehicle': None,
            'denied': True,
            'reason': getattr(gate, 'reason', 'الباقة غير مفعّلة'),
        }, status=402)

    # 2. Vehicle lookup — clean 404 instead of crashing the request.
    from inventory.models import Vehicle  # imported lazily; public-schema requests already rejected above
    vehicle = Vehicle.objects.filter(chassis_number=vin.upper()).first()
    if vehicle is None:
        return render(request, 'smart_diagnostics/live_dashboard.html', {
            'vehicle': None,
            'denied': True,
            'reason': f'لا توجد مركبة بـ VIN: {vin}. تأكد من الرقم في سجل المركبات.',
        }, status=404)

    return render(request, 'smart_diagnostics/live_dashboard.html', {
        'vehicle': vehicle,
        'denied': False,
    })


@login_required
def device_list(request):
    """Tenant self-service: list + register OBD devices."""
    tenant = getattr(request, 'tenant', None)
    gate = DiagnosticsQuotaService.check_feature(tenant, FEATURE_LIVE_DATA)
    if not gate.allowed:
        return render(request, 'smart_diagnostics/devices.html', {
            'denied': True, 'reason': gate.reason, 'devices': [],
        }, status=402)
    devices = DiagnosticDevice.objects.select_related('vehicle').order_by('-created_at')
    return render(request, 'smart_diagnostics/devices.html', {
        'devices': devices,
        'recently_created_token': request.session.pop('_recent_device_token', None),
    })


@login_required
@require_POST
def device_register(request):
    """POST { vin?, hardware_id } → creates a DiagnosticDevice and shows the
    token ONCE (stored in flash session).

    `vin` is OPTIONAL: workshop scanners (ELM327 etc.) roam between
    customer vehicles, so the device is registered without a permanent
    vehicle binding. Provide a VIN only for permanently-installed devices
    (Fleet IoT trackers)."""
    tenant = getattr(request, 'tenant', None)
    gate = DiagnosticsQuotaService.check_feature(tenant, FEATURE_LIVE_DATA)
    if not gate.allowed:
        messages.error(request, gate.reason)
        return redirect('smart_diagnostics:device-list')

    vin = (request.POST.get('vin') or '').strip().upper()
    hardware_id = (request.POST.get('hardware_id') or '').strip()

    vehicle = None
    if vin:
        vehicle = Vehicle.objects.filter(chassis_number=vin).first()
        if vehicle is None:
            messages.error(request, f"رقم الشاسيه {vin} غير موجود في سجل المركبات.")
            return redirect('smart_diagnostics:device-list')

    token = secrets.token_urlsafe(32)
    DiagnosticDevice.objects.create(
        vehicle=vehicle, device_token=token, hardware_id=hardware_id, is_active=True,
    )
    # Flash the token so it shows once after redirect; never persisted in cleartext on subsequent views.
    request.session['_recent_device_token'] = token
    if vehicle:
        label = vehicle.car_plate or vin
        messages.success(request, f"تم تسجيل الجهاز لـ {label}.")
    else:
        messages.success(request, "تم تسجيل جهاز محمول (غير مرتبط بمركبة محددة).")
    return redirect('smart_diagnostics:device-list')


@login_required
@require_POST
def device_rotate(request, device_id: int):
    device = get_object_or_404(DiagnosticDevice, pk=device_id)
    new_token = secrets.token_urlsafe(32)
    device.device_token = new_token
    device.save(update_fields=['device_token'])
    request.session['_recent_device_token'] = new_token
    messages.success(request, "تم تدوير التوكن. احفظه فوراً — لن يُعرض مرة أخرى.")
    return redirect('smart_diagnostics:device-list')


@login_required
@require_POST
def device_toggle(request, device_id: int):
    device = get_object_or_404(DiagnosticDevice, pk=device_id)
    device.is_active = not device.is_active
    device.save(update_fields=['is_active'])
    return redirect('smart_diagnostics:device-list')


# ──────────────────────────────────────────────────────────────────────
# 💎 Premium Diagnostics upgrade — pricing landing + Paymob checkout entry
# ──────────────────────────────────────────────────────────────────────
@login_required
def upgrade_premium(request):
    """عرض صفحة الـ upgrade لباقة Premium Diagnostics. الـ POST بـ يـ
    forward لـ paymob_checkout الموجود في clients (نفس الـ flow الحالي)."""
    from clients.models import Plan, TenantSubscription
    tenant = getattr(request, 'tenant', None)

    plan = Plan.objects.filter(slug='premium_diagnostics', is_active=True).first()
    if plan is None:
        return render(request, 'smart_diagnostics/upgrade_unavailable.html', status=503)

    current_sub = None
    if tenant:
        current_sub = TenantSubscription.objects.filter(tenant=tenant).select_related('plan').first()
    already_premium = bool(
        current_sub and current_sub.is_active
        and current_sub.plan_id == plan.id
    )

    return render(request, 'smart_diagnostics/upgrade.html', {
        'plan': plan,
        'features': [
            ('fa-wave-square', 'البث المباشر للـ OBD2', 'استقبال RPM, Coolant, Load, Speed، Battery، Throttle لحظياً'),
            ('fa-passport', 'الجواز الصحي للمركبة', 'سجل كامل لكل عطل + تاريخ الحل لكل VIN'),
            ('fa-route', 'خطط فحص موجَّهة (ISTA)', 'تعليمات تشخيص خطوة-بخطوة لكل كود عطل'),
            ('fa-cogs', 'البحث الذكي عن قطع الغيار', 'ربط مباشر بين DTC والـ OEM Part Numbers في مخزونك'),
            ('fa-cloud-download-alt', '200 فحص API خارجي/شهر', 'CarMD وأمثاله — يتم التجديد آلياً مع الاشتراك'),
            ('fa-microchip', 'عدد لا محدود من أجهزة OBD2', 'سجّل أجهزة لكل مركبة بـ token آمن للـ WebSocket'),
        ],
        'tenant': tenant,
        'shop': getattr(tenant, 'schema_name', '') if tenant else '',
        'already_premium': already_premium,
    })
