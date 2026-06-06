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


# ─────────────────────────────────────────────────────────────────────
# 🤖 AI Diagnostics Room (غرفة تشخيص الأعطال)
# ─────────────────────────────────────────────────────────────────────
import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect


@login_required
def diagnostics_room(request):
    """Workstation page — connects to a Bluetooth ELM327 from the browser
    and streams live data + DTCs to the AI co-pilot.

    Unlike `live_dashboard` (which is bound to a specific VIN coming from
    our hardened OBD ingest), this view is VIN-agnostic: the technician
    might be diagnosing a walk-in car that isn't even in our DB yet.
    """
    if _on_public_schema():
        return HttpResponse(
            "متاحة فقط من نطاق الشركة (tenant).",
            status=403, content_type='text/plain; charset=utf-8',
        )

    tenant = getattr(request, 'tenant', None)
    # 🐛 [Bug #1 FIX] Use the actual service method name (`check_feature`),
    # not the non-existent `can_access`. Also wrap in try/except so a
    # subscription-lookup failure renders the upgrade page instead of a 500.
    try:
        gate = DiagnosticsQuotaService.check_feature(tenant, FEATURE_LIVE_DATA)
    except Exception as e:
        logger.error(f"[diagnostics_room] entitlement check failed: {e}",
                     exc_info=True)
        gate = type('G', (), {
            'allowed': False,
            'reason': 'تعذّر التحقق من الباقة — حاول مرة أخرى.',
        })()

    if not gate.allowed:
        return render(request, 'smart_diagnostics/upgrade.html', {
            'reason': getattr(gate, 'reason', 'الباقة غير مفعّلة'),
            'tenant': tenant,
        }, status=402)

    # 🐛 [Bug #1 FIX] Gracefully degrade when neither Together nor Gemini
    # is configured — show the page in "offline AI" mode rather than
    # crashing on first chat round-trip.
    from django.conf import settings
    ai_enabled = bool(
        getattr(settings, 'ENABLE_AI_PREDICTIONS', False) and (
            getattr(settings, 'TOGETHER_API_KEY', '')
            or getattr(settings, 'GEMINI_API_KEY', '')
        )
    )

    return render(request, 'smart_diagnostics/diagnostics_room.html', {
        'tenant': tenant,
        'tech_name': request.user.get_full_name() or request.user.username,
        'ai_enabled': ai_enabled,
    })


@login_required
@csrf_protect
@require_POST
def diagnostics_room_chat(request):
    """JSON endpoint — receives the latest live-data snapshot, the DTC list,
    and the chat history; returns the AI's next reply.

    Body:
        {
          "history": [{"role":"user|assistant","text":"..."}, ...],
          "user_message": "...",          # optional — empty on first turn
          "snapshot": {"rpm": 820, "coolant_temp_c": 92, ...},
          "dtcs": ["P0171", "P0300"],
          "vehicle_hint": {"model":"BMW 330i","engine":"N20","year":2014}
        }
    """
    if _on_public_schema():
        return JsonResponse({"error": "tenant_required"}, status=403)

    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "invalid_json"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "invalid_payload"}, status=400)

    from erp_core.ai.diagnostic_room_ai import answer_room_turn

    try:
        result = answer_room_turn(
            history=payload.get("history") or [],
            user_message=(payload.get("user_message") or "").strip(),
            snapshot=payload.get("snapshot") or {},
            dtcs=payload.get("dtcs") or [],
            vehicle_hint=payload.get("vehicle_hint") or {},
            vin=(payload.get("vin") or None),
            image_data_url=(payload.get("image") or None),
            tenant=getattr(request, "tenant", None),
            user=request.user,
        )
    except Exception as exc:
        logger.exception("Diagnostics Room AI failed: %s", exc)
        return JsonResponse({"error": "ai_unavailable"}, status=503)

    return JsonResponse(result)


# ─────────────────────────────────────────────────────────────────────
# 💾 Diagnostics Room — Save to Job Card
# ─────────────────────────────────────────────────────────────────────
@login_required
def diagnostics_room_job_cards(request):
    """GET ?vin=XYZ → JSON list of active job cards for the VIN.
    Used by the 'Save to Job Card' modal."""
    if _on_public_schema():
        return JsonResponse({"error": "tenant_required"}, status=403)

    vin = (request.GET.get('vin') or '').strip()
    from smart_diagnostics.services.diag_room_persistence import (
        list_active_job_cards_for_vin,
    )
    return JsonResponse({
        "vin": vin,
        "job_cards": list_active_job_cards_for_vin(vin),
    })


@login_required
@csrf_protect
@require_POST
def diagnostics_room_save(request):
    """POST JSON → persist the session as a VehicleDiagnosticReport
    (+ photos, + optional job-card link).

    Body:
        {
          "vin": "WBA...",                          # required
          "job_card_id": 42 | null,                 # optional; auto-suggest if null
          "dtcs": ["P0102", ...],
          "snapshot": {"rpm": 820, ...},
          "ai_summary": "النص اللي هيشوفه العميل...",
          "photos": ["data:image/jpeg;base64,...", ...],
          "scan_type": "pre_repair" | "ad_hoc" | "post_repair"
        }
    """
    if _on_public_schema():
        return JsonResponse({"error": "tenant_required"}, status=403)

    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "invalid_json"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "invalid_payload"}, status=400)

    # Best-effort: attach the technician's EmployeeProfile if the request
    # user has one in this tenant.
    engineer_profile = None
    try:
        from inventory.models import EmployeeProfile
        engineer_profile = (EmployeeProfile.objects
                            .filter(user=request.user,
                                    role__in=['engineer', 'tech'])
                            .first())
    except Exception:
        pass

    from smart_diagnostics.services.diag_room_persistence import (
        save_diagnostic_session, DiagnosticSaveError,
    )

    try:
        result = save_diagnostic_session(
            vin=payload.get("vin") or "",
            dtcs=payload.get("dtcs") or [],
            live_data=payload.get("snapshot") or {},
            ai_summary=payload.get("ai_summary") or "",
            photos=payload.get("photos") or [],
            job_card_id=payload.get("job_card_id"),
            scan_type=payload.get("scan_type") or 'ad_hoc',
            engineer_profile=engineer_profile,
            created_by_user=request.user,
        )
    except DiagnosticSaveError as exc:
        return JsonResponse(
            {"error": exc.code, "message": exc.message}, status=exc.status,
        )
    except Exception as exc:
        logger.exception("Diagnostics Room save failed: %s", exc)
        return JsonResponse({"error": "save_failed"}, status=500)

    return JsonResponse({"ok": True, **result}, status=201)
