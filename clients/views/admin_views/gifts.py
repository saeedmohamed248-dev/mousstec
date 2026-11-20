"""🎁 أدوات السوبر أدمن — هدايا الشركات واشتراكات التشخيص وOBD وشحن التشخيص."""
from __future__ import annotations

import logging
from decimal import Decimal

from clients.permissions import platform_owner_required
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django_tenants.utils import schema_context

from clients.models import (
    AIBonusGrant,
    Client,
    CustomerNotification,
    MarketplaceCustomer,
    PlatformEvent,
)

from erp_core.localization import current_tenant_symbol as _sym

logger = logging.getLogger('mouss_tec_core')
User = get_user_model()


# =====================================================================
# 🎁 API: قائمة الهدايا النشطة لشركة معينة (JSON) — للمودال في السوبر أدمن
# =====================================================================
@login_required
@platform_owner_required
def super_admin_tenant_grants(request, tenant_id):
    if getattr(connection, 'schema_name', 'public') != 'public':
        return HttpResponseForbidden('Access Denied')
    tenant = get_object_or_404(Client, pk=tenant_id)
    grants = AIBonusGrant.objects.filter(tenant=tenant, is_active=True).order_by('granted_at')
    data = []
    for g in grants:
        if not g.is_valid:
            continue
        data.append({
            'id': g.pk,
            'reason': g.reason or '',
            'is_welcome': (g.granted_by_id is None and 'ترحيب' in (g.reason or '')),
            'granted_designs': g.granted_designs,
            'granted_whatsapp': g.granted_whatsapp,
            'granted_watermarks': g.granted_watermarks,
            'remaining_designs': g.remaining_designs,
            'remaining_whatsapp': g.remaining_whatsapp,
            'remaining_watermarks': g.remaining_watermarks,
            'granted_at': g.granted_at.strftime('%Y-%m-%d %H:%M'),
        })
    return JsonResponse({'grants': data})


# ============================================================================
# 🎁 Superadmin — Gift / Renew Diagnostics Subscription
# ============================================================================

@login_required
@platform_owner_required
def super_admin_gift_diagnostics(request):
    """
    GET  → render the gift-diagnostics wizard (phone lookup + tier/duration picker).
    POST → activate or renew a CustomerDiagnosticsSubscription.
    AJAX GET ?phone=… → return customer + subscription status JSON.
    """
    from clients.models import CustomerDiagnosticsSubscription
    from dateutil.relativedelta import relativedelta

    TIERS = [
        {'key': 'basic',  'label': 'Basic',  'price': 99,  'scans': 30,  'color': '#3b82f6', 'icon': 'fa-circle'},
        {'key': 'pro',    'label': 'Pro',    'price': 199, 'scans': 100, 'color': '#8b5cf6', 'icon': 'fa-star'},
        {'key': 'empire', 'label': 'Empire', 'price': 399, 'scans': None,'color': '#f59e0b', 'icon': 'fa-crown'},
    ]
    DURATIONS = [
        {'months': 1,  'label': 'شهر',       'discount': 0},
        {'months': 3,  'label': '3 أشهر',    'discount': 10},
        {'months': 6,  'label': '6 أشهر',    'discount': 15},
        {'months': 12, 'label': 'سنة كاملة', 'discount': 20},
    ]

    # ── AJAX lookup ──────────────────────────────────────────────────────────
    if request.method == 'GET' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        phone = (request.GET.get('phone') or '').strip()
        if not phone:
            return JsonResponse({'found': False, 'error': 'أدخل رقم الهاتف'})
        with schema_context('public'):
            customer = MarketplaceCustomer.objects.filter(phone=phone).first()
            if not customer:
                return JsonResponse({'found': False})
            sub = CustomerDiagnosticsSubscription.objects.filter(customer=customer).order_by('-paid_until').first()
            now = timezone.now()
            return JsonResponse({
                'found': True,
                'name': customer.full_name,
                'phone': customer.phone,
                'customer_id': customer.pk,
                'tier': sub.tier if sub else None,
                'tier_display': sub.get_tier_display() if sub else 'لا يوجد',
                'is_active': bool(sub and sub.paid_until and sub.paid_until > now),
                'days_remaining': max((sub.paid_until - now).days, 0) if (sub and sub.paid_until and sub.paid_until > now) else 0,
                'paid_until': sub.paid_until.strftime('%Y-%m-%d') if (sub and sub.paid_until) else None,
            })

    # ── POST: activate/renew ─────────────────────────────────────────────────
    if request.method == 'POST':
        phone     = (request.POST.get('phone') or '').strip()
        tier      = (request.POST.get('tier') or '').strip()
        try:
            months = int(request.POST.get('months') or 1)
        except (TypeError, ValueError):
            months = 0  # rejected below as invalid
        note      = (request.POST.get('note') or '').strip()

        valid_tiers    = {t['key'] for t in TIERS}
        valid_months   = {d['months'] for d in DURATIONS}
        discount_map   = {d['months']: d['discount'] for d in DURATIONS}
        price_map      = {t['key']: t['price'] for t in TIERS}

        if tier not in valid_tiers or months not in valid_months:
            return JsonResponse({'ok': False, 'error': 'بيانات غير صحيحة'}, status=400)

        with schema_context('public'):
            customer = MarketplaceCustomer.objects.filter(phone=phone).first()
            if not customer:
                return JsonResponse({'ok': False, 'error': 'العميل غير موجود'}, status=404)

            now        = timezone.now()
            discount   = discount_map[months]
            unit_price = Decimal(str(price_map[tier]))
            total      = (unit_price * months * (Decimal('100') - Decimal(str(discount))) / Decimal('100')).quantize(Decimal('0.01'))

            sub = CustomerDiagnosticsSubscription.objects.filter(customer=customer).order_by('-paid_until').first()
            base = max(sub.paid_until, now) if (sub and sub.paid_until and sub.paid_until > now) else now

            new_expiry = base + relativedelta(months=months)

            if sub and sub.tier == tier:
                sub.paid_until = new_expiry
                sub.save(update_fields=['paid_until'])
            else:
                sub, _ = CustomerDiagnosticsSubscription.objects.update_or_create(
                    customer=customer,
                    defaults={'tier': tier, 'paid_until': new_expiry},
                )

            # In-app notification to the customer
            CustomerNotification.objects.create(
                customer=customer,
                title=f'🎁 هدية اشتراك تشخيص {sub.get_tier_display()}',
                body=(
                    note or
                    f'تم تفعيل باقة {sub.get_tier_display()} لمدة '
                    f'{"شهر" if months == 1 else f"{months} أشهر"} '
                    f'(تنتهي {new_expiry.strftime("%Y-%m-%d")}) — بعناية إدارة Mouss Tec.'
                ),
                level='success',
                icon='fa-gift',
                action_url='/marketplace/diagnostics/',
                action_label='ابدأ التشخيص',
                sent_by=request.user,
            )

            PlatformEvent.objects.create(
                event_type='other', tenant_schema='public', tenant_name='marketplace',
                user_name=request.user.username,
                description=(
                    f"🎁 هدية اشتراك تشخيص «{sub.get_tier_display()}» × {months} شهر "
                    f"لعميل «{customer.full_name}» ({customer.phone}) — قيمة {total} {_sym()}"
                ),
            )

        return JsonResponse({
            'ok': True,
            'message': f'تم تفعيل باقة {sub.get_tier_display()} للعميل «{customer.full_name}» حتى {new_expiry.strftime("%Y-%m-%d")}.',
            'paid_until': new_expiry.strftime('%Y-%m-%d'),
        })

    # ── GET: render page ─────────────────────────────────────────────────────
    now = timezone.now()
    companies = Client.objects.filter(is_deleted=False).order_by('name').values(
        'id', 'name', 'schema_name', 'industry', 'plan',
        'has_obd_access', 'obd_access_expiry', 'subscription_end_date',
    )
    companies_list = []
    for c in companies:
        obd_valid = c['has_obd_access'] and (
            c['obd_access_expiry'] is None or c['obd_access_expiry'] > now
        )
        companies_list.append({**c, 'obd_valid': obd_valid})

    return render(request, 'clients/super_admin_gift_diagnostics.html', {
        'tiers': TIERS,
        'durations': DURATIONS,
        'companies': companies_list,
    })


# ============================================================================
# 🎁 Superadmin — Quick OBD Grant (AJAX) for gift-diagnostics page
# ============================================================================

@login_required
@platform_owner_required
def super_admin_obd_quick_grant(request, tenant_id):
    """
    AJAX POST — grant OBD access to a tenant directly from the gift page.
    POST body: duration = 1d | 1w | 1m | 3m | 6m | 1y | lifetime
    Returns JSON {ok, message, new_expiry}
    """
    from datetime import timedelta

    _DURATIONS = {
        '1d':       (timedelta(days=1),   'يوم واحد'),
        '1w':       (timedelta(days=7),   'أسبوع'),
        '1m':       (timedelta(days=30),  'شهر'),
        '3m':       (timedelta(days=90),  '3 أشهر'),
        '6m':       (timedelta(days=180), '6 أشهر'),
        '1y':       (timedelta(days=365), 'سنة'),
        'lifetime': (None,                'مدى الحياة'),
    }

    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)

    tenant = get_object_or_404(Client, pk=tenant_id)
    key = (request.POST.get('duration') or '').strip()
    if key not in _DURATIONS:
        return JsonResponse({'ok': False, 'error': 'مدة غير صحيحة'}, status=400)

    delta, label = _DURATIONS[key]
    tenant.grant_obd_access(delta, by_user=request.user)

    new_expiry = None
    if tenant.obd_access_expiry:
        new_expiry = tenant.obd_access_expiry.strftime('%Y-%m-%d')

    PlatformEvent.objects.create(
        event_type='other', tenant_schema='public', tenant_name=tenant.schema_name,
        user_name=request.user.username,
        description=f"🎁 منح OBD لـ «{tenant.name}» لمدة {label} بواسطة {request.user.username}",
    )

    logger.warning("OBD quick-grant tenant=%s duration=%s by=%s", tenant.schema_name, key, request.user.username)

    return JsonResponse({
        'ok': True,
        'message': f'✅ تم منح وصول OBD لـ «{tenant.name}» ({label})',
        'new_expiry': new_expiry or 'مدى الحياة',
        'tenant_id': tenant_id,
    })


# ============================================================================
# 🎁 Superadmin — Quick Diagnostics Top-up Grant (AJAX) for gift-diagnostics page
# ============================================================================

@login_required
@platform_owner_required
def super_admin_diag_topup_grant(request, tenant_id):
    """
    AJAX POST — gift diagnostics scan credits to a tenant.
    POST body: uses = integer (number of scans to add to diag_topup_balance)
    Returns JSON {ok, message, new_balance}

    ده بيحل مشكلة "الحصة منتهية ولا يوجد رصيد شحن" — السوبر أدمن
    يقدر يدي الشركة رصيد فحوصات مجاني من غير ما تدفع.
    """
    from clients.services.diagnostics_quota import add_topup

    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)

    tenant = get_object_or_404(Client, pk=tenant_id)

    try:
        uses = int(request.POST.get('uses') or 0)
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'عدد الفحوصات غير صحيح'}, status=400)

    if uses <= 0 or uses > 1000:
        return JsonResponse({'ok': False, 'error': 'عدد الفحوصات لازم يكون بين 1 و 1000'}, status=400)

    new_balance = add_topup(tenant, uses)

    PlatformEvent.objects.create(
        event_type='other', tenant_schema='public', tenant_name=tenant.schema_name,
        user_name=request.user.username,
        description=(
            f"🎁 شحن رصيد تشخيص لـ «{tenant.name}»: +{uses} فحص "
            f"(الرصيد الجديد {new_balance}) بواسطة {request.user.username}"
        ),
    )

    logger.warning(
        "diag-topup gift tenant=%s uses=+%d new_balance=%d by=%s",
        tenant.schema_name, uses, new_balance, request.user.username,
    )

    return JsonResponse({
        'ok': True,
        'message': f'✅ تم شحن {uses} فحص لـ «{tenant.name}» (الرصيد {new_balance})',
        'new_balance': new_balance,
        'tenant_id': tenant_id,
    })
