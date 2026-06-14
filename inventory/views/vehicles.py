"""
🚀 Mouss Tec Enterprise — Views & MAS Orchestrator Layer
=========================================================
المعمارية: كل وكيل (Agent) عبارة عن دالة نقية (Pure Function) تقبل بيانات وترجع بيانات.
الـ Views هي فقط HTTP adapters تستدعي الوكلاء — لا منطق داخل الـ view نفسه.
الـ Orchestrator يُدار بـ async-safe thread pool مع DB connection management صحيح.
"""

from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponseForbidden, HttpResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Sum, F, Q
from django.utils import timezone
from django.db import connection, transaction, close_old_connections
from django.core.cache import cache
from django.conf import settings
from django_tenants.utils import schema_context
from decimal import Decimal, InvalidOperation

import json
import urllib.parse
import base64
import uuid
import re
import logging
import concurrent.futures

from ..ai_services import predict_parts_from_dtc, scan_invoice_image_ai, call_gemini_layer
from clients.models import GlobalB2BMarketplace, Client, BlindBiddingRequest
from clients.services.entitlements import require_feature

try:
    import qrcode
    from io import BytesIO
except ImportError:
    qrcode = None

from ..models import (
    Product, Inventory, SaleInvoice, SaleInvoiceItem, Branch,
    Customer, Vehicle, ScrapDismantlingJob, ScrapDismantlingYield,
    FinancialTransaction, EmployeeShift, MaintenanceContract, Treasury,
    ChartOfAccount, AccountingEntry, InventoryMovement, StockAlert,
    ImportSession, AuditLog, PurchaseInvoice, Vendor,
)


# Shared utilities live in their own submodule and are re-exported here
# so existing view definitions (defined below) and external imports still see them.
from .utils import *  # noqa: F401, F403
from .utils import _json_response_safe, _get_branch_for_user, _require_tenant  # noqa: F401


# Vehicle history, QR, fleet contract, tech shift, passport (health + share).



# =====================================================================
# 🚗 3. جواز السفر الرقمي للمركبات وعقود الأساطيل
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
def vehicle_history(request, chassis_number):
    vehicle = get_object_or_404(
        Vehicle.objects.select_related('customer'),
        chassis_number=chassis_number,
    )
    history = SaleInvoice.objects.filter(vehicle=vehicle, status='posted').order_by('-date_created')

    # 🔧 Smart Diagnostics entitlement — drives the 'Live Diagnostics' button visibility
    has_live_diagnostics = False
    fault_count = 0
    try:
        from smart_diagnostics.services.quota import (
            DiagnosticsQuotaService, FEATURE_LIVE_DATA,
        )
        from smart_diagnostics.models import FaultLog
        tenant = getattr(request, 'tenant', None)
        has_live_diagnostics = DiagnosticsQuotaService.check_feature(tenant, FEATURE_LIVE_DATA).allowed
        fault_count = FaultLog.objects.filter(vehicle=vehicle, resolved_at__isnull=True).count()
    except Exception:
        pass

    return render(request, 'inventory/vehicle_history.html', {
        'vehicle': vehicle,
        'history': history,
        'has_live_diagnostics': has_live_diagnostics,
        'open_fault_count': fault_count,
    })


@login_required(login_url='/login/')
@tenant_required
def generate_vehicle_qr(request, chassis_number):
    if not qrcode:
        return HttpResponse("مكتبة qrcode غير مثبتة.", status=501)
    vehicle = get_object_or_404(Vehicle, chassis_number=chassis_number)
    url = request.build_absolute_uri(f'/system/vehicle/{chassis_number}/history/')
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill='black', back_color='white')
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    img_b64 = base64.b64encode(buffer.getvalue()).decode()
    return HttpResponse(
        f'<div style="text-align:center;margin-top:50px;font-family:Cairo;">'
        f'<h2>جواز السفر الرقمي: {vehicle.car_plate}</h2>'
        f'<img src="data:image/png;base64,{img_b64}" /></div>'
    )


@login_required(login_url='/login/')
@tenant_required
@require_feature('workshop_fleet_contracts')
def fleet_contract_balance_api(request, contract_code):
    """🔒 Fleet maintenance contract balance — Empire-only feature."""
    contract = get_object_or_404(
        MaintenanceContract, contract_code=contract_code, is_active=True
    )
    consumed = (
        SaleInvoice.objects
            .filter(maintenance_contract=contract, status='posted')
            .aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
    )
    remaining = contract.total_value - consumed
    return _json_response_safe({
        "status": "success",
        "company_name": contract.customer.name,
        "contract_value": float(contract.total_value),
        "consumed_value": float(consumed),
        "remaining_balance": float(remaining),
        "is_valid": remaining > 0 and contract.end_date >= timezone.now().date(),
    })


@login_required(login_url='/login/')
@tenant_required
def tech_shift_manager_api(request, action):
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)
    try:
        if not hasattr(request.user, 'employee_profile'):
            return _json_response_safe({"error": "غير مصرح"}, 403)
        profile = request.user.employee_profile
        if profile.role != 'tech':
            return _json_response_safe({"error": "مخصص للفنيين فقط."}, 403)

        if action == 'clock_in':
            if EmployeeShift.objects.filter(employee=profile, clock_out__isnull=True).exists():
                return _json_response_safe({"error": "لديك وردية مفتوحة بالفعل!"}, 400)
            EmployeeShift.objects.create(employee=profile, clock_in=timezone.now())
            return _json_response_safe({"status": "success", "message": "تم تسجيل الدخول."})

        elif action == 'clock_out':
            shift = EmployeeShift.objects.filter(employee=profile, clock_out__isnull=True).first()
            if not shift:
                return _json_response_safe({"error": "لا توجد وردية مفتوحة."}, 400)
            shift.clock_out = timezone.now()
            shift.save()
            return _json_response_safe({
                "status": "success",
                "message": "تم إنهاء الوردية.",
                "total_hours": float(shift.total_hours),
            })

        return _json_response_safe({"error": "Invalid action"}, 400)
    except Exception as e:
        logger.error(f"[SHIFT API] {e}")
        return _json_response_safe({"error": "خطأ داخلي"}, 500)


# =====================================================================
# 📄 12.5. تصفية المركبات حسب العميل (Vehicle-Customer Dynamic Filter)
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
def vehicles_by_customer_api(request, customer_id):
    """
    يرجع قائمة مركبات العميل المحدد لتصفية الـ autocomplete في فاتورة البيع.
    """
    vehicles = Vehicle.objects.filter(customer_id=customer_id).values(
        'id', 'chassis_number', 'car_plate', 'brand', 'model_name'
    )
    return JsonResponse({
        'results': [
            {
                'id': v['id'],
                'text': f"{v['car_plate'] or 'بدون لوحة'} - {(v['chassis_number'] or '')[-6:]} ({v['brand']} {v['model_name'] or ''})"
            }
            for v in vehicles
        ]
    })


# ─────────────────────────────────────────────────────────────────────
# 🪪 Vehicle Health Passport — full timeline + AI history per VIN
# ─────────────────────────────────────────────────────────────────────
@login_required(login_url='/login/')
@tenant_required
def vehicle_health_passport(request, chassis_number):
    """The single-source-of-truth view of a vehicle's life with us:
    every visit, every DTC, every part replaced, every photo, every
    diagnostic AI summary — laid out as a reverse-chronological timeline.

    Built for two audiences:
        • The advisor explaining to a customer why a repair is needed
          ("look, your car has come in 4 times for the same code…").
        • The customer themselves on the public share view (future step).

    Anyone in the workshop can view; the share-link variant will be
    public-token gated like the AI diagnostic report.
    """
    vehicle = get_object_or_404(
        Vehicle.objects
            .select_related('customer')
            .prefetch_related(
                'diagnostic_reports__engineer__user',
                'diagnostic_reports__photos',
                'service_nudges__rule',
            ),
        chassis_number=chassis_number.upper(),
    )

    # All Job Cards (posted = paid, others = open work)
    job_cards = list(
        SaleInvoice.objects
        .filter(vehicle=vehicle, invoice_type='maintenance')
        .select_related('branch')
        .prefetch_related(
            'items__product', 'service_items__service',
            'diagnostic_reports', 'repair_logs',
        )
        .order_by('-date_created')
    )

    # All diagnostic reports — sorted newest first
    diag_reports = list(
        vehicle.diagnostic_reports.all().order_by('-scanned_at')
    )

    # Aggregate DTC frequency across the whole life of the vehicle
    from collections import Counter
    dtc_counter = Counter()
    for r in diag_reports:
        for code in (r.fault_codes or []):
            dtc_counter[code.upper()] += 1
    top_dtcs = dtc_counter.most_common(8)

    # Aggregate parts ever installed
    parts_counter = Counter()
    for jc in job_cards:
        for item in jc.items.all():
            parts_counter[item.product.name] += item.quantity or 0
    top_parts = parts_counter.most_common(6)

    # Build a unified timeline: visits + diagnostic-only sessions
    timeline = []
    for jc in job_cards:
        timeline.append({
            'type': 'visit',
            'date': jc.date_created,
            'invoice': jc,
            'total': jc.total_amount,
            'status': jc.status,
            'branch_name': jc.branch.name if jc.branch_id else '',
            'parts_count': jc.items.count(),
            'services_count': jc.service_items.count(),
            'diag_count': jc.diagnostic_reports.count(),
            'photos_count': sum(r.photos.count() for r in jc.diagnostic_reports.all()),
            'ai_summary_excerpt': next(
                (r.ai_summary[:240]
                 for r in jc.diagnostic_reports.all()
                 if r.ai_summary), ''),
        })
    # Orphan diagnostic reports (saved without a Job Card link)
    for r in diag_reports:
        if r.job_card_id is None:
            timeline.append({
                'type': 'orphan_diag',
                'date': r.scanned_at,
                'report': r,
                'fault_codes': r.fault_codes,
                'ai_summary_excerpt': (r.ai_summary or '')[:240],
                'photos_count': r.photos.count(),
            })

    timeline.sort(key=lambda e: e['date'], reverse=True)

    # Predictive nudges for this vehicle (computed live so they reflect
    # the latest job-card timestamps; the daily Celery task does the
    # heavy bulk-compute version)
    nudges = []
    try:
        from inventory.predictive_engine import compute_nudges_for_vehicle
        nudges = compute_nudges_for_vehicle(vehicle, persist=False)
    except Exception:
        nudges = []

    return render(request, 'inventory/vehicle_health_passport.html', {
        'vehicle': vehicle,
        'customer': vehicle.customer,
        'timeline': timeline,
        'diag_reports': diag_reports,
        'top_dtcs': top_dtcs,
        'top_parts': top_parts,
        'nudges': nudges,
        'visit_count': len(job_cards),
        'last_visit': job_cards[0].date_created if job_cards else None,
        'first_visit': job_cards[-1].date_created if job_cards else None,
    })


def _sign_passport_share(chassis_number, tenant_schema):
    """Bind the token to BOTH chassis_number and tenant_schema so a token
    from workshop A can't be replayed against workshop B's vehicle with
    the same VIN — even though VINs are globally unique, a leaked token
    must not cross schema boundaries."""
    from django.core.signing import TimestampSigner
    signer = TimestampSigner(salt=_PASSPORT_SHARE_SALT)
    return signer.sign(f"{tenant_schema}:{chassis_number.upper()}")


def _unsign_passport_share(token, tenant_schema):
    from django.core.signing import TimestampSigner, BadSignature, SignatureExpired
    signer = TimestampSigner(salt=_PASSPORT_SHARE_SALT)
    try:
        raw = signer.unsign(token, max_age=_PASSPORT_SHARE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    try:
        schema, vin = raw.split(':', 1)
        if schema != tenant_schema:
            return None
        return vin
    except (ValueError, TypeError):
        return None


@csrf_exempt
def vehicle_passport_share(request, token):
    """Public, signed access to the Vehicle Health Passport — no login.

    Reuses the existing `vehicle_health_passport` template but passes
    `public_share=True` so internal-only elements (the share-WhatsApp
    button, the link to the Job Card review) are suppressed.

    Token lifetime: 30 days (longer than the Diagnostic share — passports
    are slow-moving artefacts; a customer might come back to it weeks later).
    """
    tenant = getattr(request, 'tenant', None)
    tenant_schema = getattr(tenant, 'schema_name', '') or ''
    vin = _unsign_passport_share(token, tenant_schema)
    if vin is None:
        return HttpResponse(
            "الرابط منتهي الصلاحية أو غير صحيح. اطلب من مركز الصيانة رابطاً جديداً.",
            status=410, content_type='text/html; charset=utf-8',
        )

    vehicle = (Vehicle.objects
               .select_related('customer')
               .prefetch_related(
                   'diagnostic_reports__photos',
                   'diagnostic_reports__engineer__user',
               )
               .filter(chassis_number=vin.upper()).first())
    if vehicle is None:
        return HttpResponse(
            "السيارة غير موجودة.",
            status=404, content_type='text/html; charset=utf-8',
        )

    # Reuse the SAME context shape as the internal view so the template
    # is a single source of truth. We just don't compute the live nudges
    # (engine writes are protected; reads are fine, but predictive
    # surprises shouldn't surface to customers without advisor context).
    from collections import Counter

    job_cards = list(
        SaleInvoice.objects
        .filter(vehicle=vehicle, invoice_type='maintenance', status='posted')
        .select_related('branch')
        .prefetch_related('items__product', 'service_items__service',
                          'diagnostic_reports')
        .order_by('-date_created')
    )
    diag_reports = list(vehicle.diagnostic_reports.all().order_by('-scanned_at'))

    dtc_counter = Counter()
    for r in diag_reports:
        for code in (r.fault_codes or []):
            dtc_counter[code.upper()] += 1

    parts_counter = Counter()
    for jc in job_cards:
        for item in jc.items.all():
            parts_counter[item.product.name] += item.quantity or 0

    timeline = []
    for jc in job_cards:
        timeline.append({
            'type': 'visit',
            'date': jc.date_created,
            'invoice': jc,
            'total': jc.total_amount,
            'status': jc.status,
            'branch_name': jc.branch.name if jc.branch_id else '',
            'parts_count': jc.items.count(),
            'services_count': jc.service_items.count(),
            'diag_count': jc.diagnostic_reports.count(),
            'photos_count': sum(r.photos.count() for r in jc.diagnostic_reports.all()),
            'ai_summary_excerpt': next(
                (r.ai_summary[:240] for r in jc.diagnostic_reports.all() if r.ai_summary),
                '',
            ),
        })
    timeline.sort(key=lambda e: e['date'], reverse=True)

    return render(request, 'inventory/vehicle_health_passport.html', {
        'vehicle': vehicle,
        'customer': vehicle.customer,
        'timeline': timeline,
        'diag_reports': diag_reports,
        'top_dtcs': dtc_counter.most_common(8),
        'top_parts': parts_counter.most_common(6),
        'nudges': [],   # never surface predictive nudges on the public view
        'visit_count': len(job_cards),
        'last_visit': job_cards[0].date_created if job_cards else None,
        'first_visit': job_cards[-1].date_created if job_cards else None,
        'public_share': True,
    })


@login_required(login_url='/login/')
@tenant_required
@csrf_exempt
def vehicle_passport_share_link(request, chassis_number):
    """POST → returns {share_url, wa_url, message_preview} so the
    internal Passport's 'Share via WhatsApp' button can open WA with
    the customer's phone and a pre-filled body."""
    if request.method != 'POST':
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    cookie_token = request.COOKIES.get('mt_csrf')
    if not cookie_token or cookie_token != request.headers.get('X-CSRFToken', ''):
        return JsonResponse({"error": "csrf_failed"}, status=403)

    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'sales', 'cashier', 'accountant', 'admin', 'manager'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return JsonResponse({"error": "role_forbidden"}, status=403)

    vehicle = get_object_or_404(
        Vehicle.objects.select_related('customer'),
        chassis_number=chassis_number.upper(),
    )

    tenant = getattr(request, 'tenant', None)
    tenant_schema = getattr(tenant, 'schema_name', '') or ''
    if not tenant_schema:
        return JsonResponse({"error": "tenant_required"}, status=400)

    token = _sign_passport_share(chassis_number, tenant_schema)
    rel = reverse('inventory:vehicle_passport_share', args=[token])
    share_url = request.build_absolute_uri(rel)

    workshop_name = getattr(tenant, 'name', None) or tenant_schema
    vehicle_label = ' '.join(filter(None, [
        vehicle.brand, vehicle.model_name,
        f"({vehicle.car_plate})" if vehicle.car_plate else '',
    ])) or 'سيارتك'
    customer_name = vehicle.customer.name if vehicle.customer_id else 'عميلنا الكريم'

    body = (
        f"مرحباً {customer_name} 👋\n\n"
        f"إليك الجواز الصحي الكامل لـ *{vehicle_label}* من مركز *{workshop_name}*.\n"
        f"يمكنك الاطلاع على كل الزيارات السابقة، الأعطال، والصور الموثّقة من الرابط:\n"
        f"{share_url}\n\n"
        f"الرابط فعّال لمدة 30 يوماً. نحن في خدمتك دائماً."
    )

    # Build wa.me link if the customer has a phone
    wa_url = ''
    if vehicle.customer_id and vehicle.customer.phone:
        import re as _re, urllib.parse
        digits = _re.sub(r'[\s\-\(\)+]+', '', vehicle.customer.phone)
        if digits.startswith('00'):
            digits = digits[2:]
        elif digits.startswith('0'):
            digits = '20' + digits[1:]
        wa_url = f"https://wa.me/{digits}?text={urllib.parse.quote(body)}"

    return JsonResponse({
        "ok": True,
        "share_url": share_url,
        "wa_url": wa_url,
        "has_phone": bool(wa_url),
        "message_preview": body,
        "customer_name": customer_name,
        "vehicle_label": vehicle_label,
    })


