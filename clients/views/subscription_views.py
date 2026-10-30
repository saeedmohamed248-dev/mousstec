"""
SaaS subscription endpoints — pricing page, Paymob checkout/callback,
self-serve subscription management, addon purchasing, and the public
features page.

Paymob callback is the one csrf_exempt endpoint in this module (external
caller, HMAC-verified). Everything else is csrf-protected.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

from clients.models import (
    Client, DesignPurchase, EscrowLedger, Feature,
    Plan, PlanRevision, PlatformInvoice, TenantSubscription,
)
from clients.models.tenancy import TRIAL_DAYS
from clients.services.plan_mapping import LEGACY_TO_PLAN_SLUG, resolve_plan_slug
from erp_core.localization import current_tenant_symbol as _sym
from billing.services.paymob import paymob_cache

logger = logging.getLogger('mouss_tec_core')
ADMIN_URL = os.getenv('ADMIN_URL', 'secure-portal')


# =====================================================================
# 💳 8. بوابة الاشتراكات والباقات (SaaS Pricing & Retention)
# =====================================================================
_PERIOD_MONTHS = {'monthly': 1, 'quarterly': 3, 'semi_annual': 6, 'annual': 12}


_PAY_TOKEN_SALT = 'pricing-shop-pay'


def pay_token_for(schema_name: str) -> str:
    """توكن موقّع بيسمح لصاحب شركة لسه مسجّلة (ومش داخل على الدومين بتاعها) يكمّل الدفع."""
    from django.core import signing
    return signing.dumps(schema_name, salt=_PAY_TOKEN_SALT)


def shop_pay_allowed(request, shop: str) -> bool:
    """مين يقدر يفتح فاتورة اشتراك لشركة: حد داخل على الدومين بتاعها، أو بتوكن التسجيل،
    أو مالك المنصة.

    🛡️ [FIX]: أي حد كان يقدر يفتح فواتير «مستحقة» وإيصالات لأي شركة بمجرد اسمها (?shop=)
    — بتظهر عندها في «فواتيري»، ودفع بالكارت جديد كان بيلغي طلب الكارت المعلّق بتاعها."""
    from django.core import signing
    from clients.permissions import is_platform_owner
    if not shop:
        return False
    if is_platform_owner(request.user):
        return True
    # صاحب الشركة (superuser جوه سكيمتها) — مش أي موظف/كاشير
    if (request.user.is_authenticated and request.user.is_superuser
            and getattr(getattr(request, 'tenant', None), 'schema_name', '') == shop):
        return True
    token = request.POST.get('pt') or request.GET.get('pt') or ''
    try:
        return signing.loads(token, salt=_PAY_TOKEN_SALT, max_age=7 * 86400) == shop
    except signing.BadSignature:
        return False


def is_uae_tenant(tenant) -> bool:
    return (getattr(tenant, 'country', 'EG') or 'EG').upper() == 'AE'


def paymob_configured() -> bool:
    """زرار «ادفع بالكارت» للاشتراكات: مفتاح التشغيل PAYMOB_CARD_SUBSCRIPTIONS (مقفول
    افتراضياً — المالك بيفتحه بعد ما يجرّب في Test Mode) + المفاتيح الأربعة. من غير
    PAYMOB_HMAC_SECRET كل إشعارات الدفع بتترفض في الإنتاج — يعني فلوس من غير تفعيل."""
    if not getattr(settings, 'PAYMOB_CARD_SUBSCRIPTIONS', False):
        return False

    def _get(name):
        return str(getattr(settings, name, '') or os.getenv(name, '') or '').strip()
    return bool(_get('PAYMOB_API_KEY') and _get('PAYMOB_IFRAME_ID') and _get('PAYMOB_HMAC_SECRET')
                and _get('PAYMOB_INTEGRATION_ID').isdigit())


@ensure_csrf_cookie
def saas_pricing_page(request):
    shop_schema = request.GET.get('shop', '')
    tenant = Client.objects.filter(schema_name=shop_schema).first() if shop_schema else None

    if request.method == 'POST':
        selected_plan = request.POST.get('plan')
        shop_post = request.POST.get('shop', '').strip()
        target_tenant = Client.objects.filter(schema_name=shop_post).first() if shop_post else None

        valid_plans = [c[0] for c in Client.SUBSCRIPTION_CHOICES]

        # سيناريو 1: زائر جديد (مافيش shop) → يروح لصفحة التسجيل مع الباقة محددة مسبقاً
        if not target_tenant and selected_plan in valid_plans:
            return redirect(f"{reverse('saas_customer_signup')}?plan={selected_plan}")

        # سيناريو 2: عميل موجود يجدد أو يغير الباقة
        # حماية أمنية: فقط السوبر أدمن أو مستخدم مصادق عليه يمكنه تغيير الاشتراك
        if target_tenant and selected_plan in valid_plans:
            if not request.user.is_authenticated:
                messages.error(request, "🔒 يجب تسجيل الدخول أولاً لإدارة الاشتراك.")
                return redirect(f"{reverse('client_login_finder')}")
            # 🛡️ [SECURITY FIX]: صاحب الشركة superuser جوه سكيمته والصفحة دي شغالة على
            #    كل الـ subdomains — كان يقدر يمدّ اشتراكه ٣٠ يوم (ويغيّر باقته) ببلاش كل مرة.
            from clients.permissions import is_platform_owner
            if not is_platform_owner(request.user):
                messages.error(request, "🛑 غير مصرح — فقط السوبر أدمن يمكنه تغيير الاشتراك مباشرة.")
                return redirect(reverse('saas_pricing') + f'?shop={shop_post}')
            with transaction.atomic():
                target_tenant.plan, target_tenant.status = selected_plan, 'active'
                base_date = max(target_tenant.subscription_end_date or timezone.localdate(), timezone.localdate())

                # مكافأة ولاء: 5 أيام مجانية عند التجديد المبكر
                bonus_days = 5 if (target_tenant.subscription_end_date and target_tenant.subscription_end_date > timezone.localdate()) else 0
                target_tenant.subscription_end_date = base_date + timedelta(days=30 + bonus_days)

                target_tenant.save()
                from django.conf import settings as _cfg2
                _bd = getattr(_cfg2, 'BASE_DOMAIN', 'mousstec.com')
                return redirect(f"https://{target_tenant.schema_name.replace('_', '-')}.{_bd}/{ADMIN_URL}/")

        messages.error(request, "🛑 فشل تنفيذ عملية الاشتراك.")

    # نظام خصومات الفترات الطويلة — display only; per-plan discounts live on Plan
    billing_discounts = {
        'monthly':      {'label': 'شهري',       'months': 1,  'discount': 0},
        'quarterly':    {'label': 'ربع سنوي',   'months': 3,  'discount': 9},
        'semi_annual':  {'label': 'نصف سنوي',   'months': 6,  'discount': 12.5},
        'annual':       {'label': 'سنوي',       'months': 12, 'discount': 25},
    }

    # 💎 Dynamic plan catalog — single source of truth is the Plan model.
    # Super Admin edits to monthly_price / entitlements / features propagate
    # to this page on the next request (no cache).
    #
    # 🛡️ Defensive: any DB hiccup (missing migration, schema drift, Feature
    # table renamed) used to surface as a generic 500 with no detail. We now
    # degrade gracefully: log the real cause and render the page with whatever
    # rows we managed to load. This keeps signups working even mid-deploy.
    plan_slug_to_legacy = {v: k for k, v in LEGACY_TO_PLAN_SLUG.items()}
    plans_by_industry: dict[str, list[dict]] = {'automotive': [], 'printing': []}

    # 🌍 لغة العرض الحالية — تحدد أسماء الباقات والمزايا (عربي/إنجليزي)
    from django.utils.translation import get_language, gettext as _gettext
    _is_en = (get_language() or 'ar').startswith('en')

    try:
        # على الموقع الإنجليزي نعرض name_en للمزايا (fallback للعربي لو فارغ)
        feature_labels = {}
        for code, name_ar, name_en in Feature.objects.filter(is_active=True).values_list('code', 'name_ar', 'name_en'):
            feature_labels[code] = (name_en or name_ar) if _is_en else name_ar
    except Exception:
        logger.exception("[PRICING] failed to load Feature catalog — rendering without labels")
        feature_labels = {}

    # 🌐 منطقة الموقع (مصري/إماراتي) من الدومين — تحدد عملة وأسعار العرض.
    from erp_core.regions import region_from_request
    region = region_from_request(request)
    _is_ae = region['country'] == 'AE'
    # 🇦🇪 الدفع المتاح (فودافون كاش / إنستاباي / Paymob) بالجنيه بس — العميل الإماراتي كان بيشوف
    #    «65 د.إ» ويتطلب منه 850 ج.م. لحد ما يبقى فيه دفع بالدرهم: الاشتراك عن طريق الدعم.
    ae_contact_only = _is_ae or (tenant is not None and is_uae_tenant(tenant))

    # ⬆️⬇️ تسعيرة التغيير للشركة نفسها بس (من الدومين بتاعها وهي داخلة) — زائر بـ ?shop=
    #    مايشوفش أيام/استخدام شركة تانية. الدفع نفسه بيحسب التسعيرة دايماً على السيرفر.
    quote_tenant = None
    if (tenant is not None and request.user.is_authenticated
            and getattr(getattr(request, 'tenant', None), 'schema_name', '') == tenant.schema_name):
        quote_tenant = tenant
    quote_usage = quote_sub = None

    try:
        plan_qs = Plan.objects.filter(is_active=True).order_by('industry', 'sort_order')
        for p in plan_qs:
            try:
                ents = p.entitlements if isinstance(p.entitlements, dict) else {}
                enabled_codes = [
                    code for code, cfg in ents.items()
                    if isinstance(cfg, dict) and cfg.get('enabled')
                ]
                # سعر العرض: على الموقع الإماراتي نعرض سعر الدرهم إن وُجد،
                # وإلا نسقط للسعر المصري.
                _aed = getattr(p, 'monthly_price_aed', 0) or 0
                display_price = _aed if (_is_ae and _aed > 0) else p.monthly_price
                # اسم الباقة حسب اللغة: name_en على الموقع الإنجليزي، وإلا ترجمة الاسم من
                # ملف الترجمة (الباقات المزروعة بالمهاجرات)، وإلا الاسم نفسه.
                display_name = (p.name_en or _gettext(p.name)) if _is_en else p.name
                plans_by_industry.setdefault(p.industry, []).append({
                    'slug': p.slug,
                    'legacy_slug': plan_slug_to_legacy.get(p.slug, p.slug),
                    'name': display_name,
                    'monthly_price': display_price,
                    'display_price': display_price,
                    # 🐛 [FIX]: الصفحة كانت بتحسب الخصم في JS بنسب ثابتة (9/12.5/25%) والتحصيل
                    #    (كارت/فاتورة) بخصومات الباقة (10/15/20%) — سنوي جولد يظهر 7,650
                    #    ويتحصّل 8,160. دلوقتي الأسعار نفسها من price_for_period.
                    'period_prices': {period: p.price_for_period(m, display_price)
                                      for period, m in _PERIOD_MONTHS.items()},
                    'period_discounts': {period: p.discount_for(m) for period, m in _PERIOD_MONTHS.items()},
                    'max_users': p.max_users,
                    'max_branches': p.max_branches,
                    'max_treasuries': p.max_treasuries,
                    'monthly_ai_designs_quota': p.monthly_ai_designs_quota,
                    # 🐛 [FIX]: المميزات كانت بتظهر عربي في الموقع الإنجليزي — بتترجم من ملف
                    #    الترجمة لو موجودة (أي سطر جديد من لوحة المالك بيفضل زي ما اتكتب).
                    'features': [_gettext(f) if _is_en else f for f in (p.features or [])],
                    'entitlement_labels': [
                        feature_labels[c] for c in enabled_codes if c in feature_labels
                    ],
                    'quote': None,
                })
                if (quote_tenant is not None and not _is_ae
                        and p.industry == getattr(quote_tenant, 'industry', p.industry)):
                    from clients.services.platform_billing import _usage_counts, quote_plan_change
                    if quote_usage is None:   # مرة واحدة للصفحة كلها
                        try:
                            quote_usage = _usage_counts(quote_tenant)
                        except Exception:
                            quote_usage = {}
                        quote_sub = TenantSubscription.objects.filter(
                            tenant=quote_tenant).select_related('plan').first()
                    qs = {period: quote_plan_change(quote_tenant, p, m, usage=quote_usage, sub=quote_sub)
                          for period, m in _PERIOD_MONTHS.items()}
                    first = qs['monthly']
                    plans_by_industry[p.industry][-1]['quote'] = {
                        'kind': first['kind'],
                        'prices': {period: q['total'] for period, q in qs.items()},
                        'remaining_days': first['remaining_days'],
                        'starts': first['period_start'],
                        'blocked': ' '.join(first['blockers']),
                    }
            except Exception:
                logger.exception("[PRICING] skipped Plan id=%s — bad row", getattr(p, 'pk', '?'))
    except Exception:
        logger.exception("[PRICING] failed to load Plan catalog — rendering empty pricing page")

    # خصم كل مدة على زرار الاختيار = أكبر خصم بين الباقات (كلها نفس النسبة حالياً)
    for period in _PERIOD_MONTHS:
        best = max((pl['period_discounts'][period] for pls in plans_by_industry.values() for pl in pls),
                   default=0)
        billing_discounts[period]['discount'] = best

    # convention: middle plan in each industry = "most popular"
    for plans_list in plans_by_industry.values():
        for idx, plan_dict in enumerate(plans_list):
            plan_dict['is_popular'] = (len(plans_list) >= 3 and idx == 1)

    try:
        return render(request, 'clients/pricing.html', {
            'tenant': tenant, 'shop': shop_schema,
            'platform_region': region,
            'plans_by_industry': plans_by_industry,
            'pricing': {
                # Add-on price follows the region's currency (EGP ÷ 13 ≈ AED),
                # so the UAE site doesn't show an Egyptian figure.
                'addon_price': 10 if _is_ae else 125,
                # 🐛 [FIX]: الصفحة كانت بتقول «جرّب 3 أيام» والتسجيل بيدّي TRIAL_DAYS (7)
                'free_trial_days': TRIAL_DAYS,
                'vodafone_cash': '',
                'billing_discounts': billing_discounts,
            },
            # 💳 الدفع بالكارت: لما مفاتيح Paymob تكون متظبطة (والموقع المصري — الحساب بالجنيه)
            'card_payments_enabled': paymob_configured() and not _is_ae,
            'ae_contact_only': ae_contact_only,
            'pay_token': (request.GET.get('pt') or '') if shop_schema else '',
            'support_whatsapp': settings.SUPPORT_WHATSAPP,
        })
    except Exception:
        logger.exception("[PRICING] template render failed shop=%r plans=%s",
                         shop_schema, {k: len(v) for k, v in plans_by_industry.items()})
        raise


# =====================================================================
# 💳 8.5 بوابة الدفع عبر Paymob (Visa / Mastercard)
# =====================================================================
def paymob_checkout(request):
    """
    إنشاء طلب دفع عبر Paymob وتوجيه العميل لصفحة الدفع الآمنة.
    يتطلب تكوين PAYMOB_API_KEY و PAYMOB_INTEGRATION_ID في البيئة.
    """
    if request.method != 'POST':
        return redirect('saas_pricing')

    plan = request.POST.get('plan', '')
    amount = request.POST.get('amount', '0')
    shop = request.POST.get('shop', '')
    billing_period = request.POST.get('billing_period', 'monthly')

    if not paymob_configured():
        messages.error(request, "الدفع بالكارت للاشتراكات مش متاح حالياً — ادفع بفودافون كاش أو إنستاباي.")
        return redirect(reverse('saas_pricing') + (f'?shop={shop}' if shop else ''))

    paymob_api_key = getattr(settings, 'PAYMOB_API_KEY', '') or os.getenv('PAYMOB_API_KEY', '')
    paymob_integration_id = getattr(settings, 'PAYMOB_INTEGRATION_ID', '') or os.getenv('PAYMOB_INTEGRATION_ID', '')
    paymob_iframe_id = getattr(settings, 'PAYMOB_IFRAME_ID', '') or os.getenv('PAYMOB_IFRAME_ID', '')

    if not paymob_api_key:
        logger.error(f"[PAYMOB] API key not configured. Settings: API_KEY={bool(paymob_api_key)}, INT_ID={bool(paymob_integration_id)}, IFRAME={bool(paymob_iframe_id)}")
        messages.error(request, "الدفع الإلكتروني بالفيزا غير متاح حالياً. يرجى الدفع عبر فودافون كاش أو التواصل مع الدعم الفني.")
        return redirect(reverse('saas_pricing') + (f'?shop={shop}' if shop else ''))

    import requests as http_requests

    # 🛡️ تحقق من قيم الإعدادات
    try:
        integration_id_int = int(paymob_integration_id)
    except (TypeError, ValueError):
        logger.error(f"[PAYMOB] PAYMOB_INTEGRATION_ID غير رقمي: {paymob_integration_id!r}")
        messages.error(request, "إعدادات بوابة الدفع غير صحيحة. الدفع بالفيزا متعطل مؤقتاً.")
        return redirect(reverse('saas_pricing') + (f'?shop={shop}' if shop else ''))
    if not paymob_iframe_id:
        logger.error("[PAYMOB] PAYMOB_IFRAME_ID غير مضبوط")
        messages.error(request, "إعدادات بوابة الدفع غير مكتملة. الدفع بالفيزا متعطل مؤقتاً.")
        return redirect(reverse('saas_pricing') + (f'?shop={shop}' if shop else ''))

    # 🛡️ [SECURITY FIX]: المبلغ كان جاي من الفورم في المتصفح — أي حد يقدر
    #    يغيّره لجنيه واحد، والـ callback بيبني الفاتورة من سعر الباقة ويعلّمها
    #    مدفوعة. السعر دلوقتي محسوب على السيرفر من الباقة والمدة.
    months = {'monthly': 1, 'quarterly': 3, 'semi_annual': 6, 'annual': 12}.get(billing_period, 1)
    plan_row = Plan.objects.filter(slug=resolve_plan_slug(plan) or '', is_active=True).first()
    if plan_row is None:
        messages.error(request, "الباقة غير موجودة.")
        return redirect(reverse('saas_pricing') + (f'?shop={shop}' if shop else ''))
    # 🐛 [FIX]: من غير شركة الدفع كان بيتم والـ callback مالوش حاجة يفعّلها
    #    (`if shop:`) — فلوس اتخصمت ومافيش اشتراك. الشركة لازمة قبل البوابة.
    tenant_obj = Client.objects.filter(schema_name=shop).first() if shop else None
    if tenant_obj is None:
        messages.error(request, "سجّل دخولك لشركتك (أو سجّل شركة جديدة) الأول، وبعدين ادفع بالكارت.")
        return redirect(reverse('saas_pricing'))
    if is_uae_tenant(tenant_obj):
        messages.error(request, "الاشتراك من الإمارات عن طريق الدعم الفني — كلّمنا على واتساب.")
        return redirect(reverse('saas_pricing') + f'?shop={shop}')
    if not shop_pay_allowed(request, shop):
        messages.error(request, "🔒 سجّل دخولك لشركتك الأول، وادفع من «إدارة الاشتراك».")
        return redirect(reverse('saas_pricing'))
    # ⬆️⬇️ المبلغ حسب نوع التغيير: ترقية = فرق الأيام الفاضلة، تخفيض = بيبدأ بعد المدة الحالية
    from clients.services.platform_billing import quote_plan_change
    quote = quote_plan_change(tenant_obj, plan_row, months)
    if quote['blockers']:
        messages.error(request, "مينفعش تنزل للباقة دي دلوقتي: " + " ".join(quote['blockers']))
        return redirect(reverse('saas_pricing') + f'?shop={shop}')
    amount_dec = quote['total']
    if amount_dec <= 0:
        messages.error(request, "المبلغ غير صحيح.")
        return redirect(reverse('saas_pricing') + (f'?shop={shop}' if shop else ''))
    amount = str(amount_dec)
    amount_cents = int((amount_dec * 100).to_integral_value())

    try:
        # Step 1: Auth token
        auth_res = http_requests.post('https://accept.paymob.com/api/auth/tokens', json={
            'api_key': paymob_api_key
        }, timeout=15)
        if auth_res.status_code not in (200, 201):
            logger.error(f"[PAYMOB] Auth failed: HTTP {auth_res.status_code} — {auth_res.text[:300]}")
            messages.error(request, "فشل المصادقة مع بوابة الدفع. حاول لاحقاً.")
            return redirect(reverse('saas_pricing') + (f'?shop={shop}' if shop else ''))
        auth_token = auth_res.json().get('token')
        if not auth_token:
            logger.error(f"[PAYMOB] Auth returned no token: {auth_res.text[:300]}")
            messages.error(request, "بوابة الدفع لم ترسل رمز المصادقة.")
            return redirect(reverse('saas_pricing') + (f'?shop={shop}' if shop else ''))

        # Step 2: Create order
        order_res = http_requests.post('https://accept.paymob.com/api/ecommerce/orders', json={
            'auth_token': auth_token,
            'delivery_needed': 'false',
            'amount_cents': amount_cents,
            'currency': 'EGP',
            'items': [{'name': f'Mouss Tec {plan} Plan', 'amount_cents': amount_cents, 'quantity': '1'}],
            'merchant_order_id': f'mousstec_{plan}_{uuid.uuid4().hex[:8]}',
        }, timeout=15)
        if order_res.status_code not in (200, 201):
            logger.error(f"[PAYMOB] Order failed: HTTP {order_res.status_code} — {order_res.text[:300]}")
            messages.error(request, "فشل إنشاء طلب الدفع. حاول لاحقاً.")
            return redirect(reverse('saas_pricing') + (f'?shop={shop}' if shop else ''))
        order_id = order_res.json().get('id')
        if not order_id:
            logger.error(f"[PAYMOB] Order returned no id: {order_res.text[:300]}")
            messages.error(request, "بوابة الدفع لم ترسل رقم الطلب.")
            return redirect(reverse('saas_pricing') + (f'?shop={shop}' if shop else ''))

        # 🧾 الفاتورة بتتسجّل دلوقتي بمرجع رقم الطلب (مش في الكاش بس) — الـ
        #    callback بيلاقيها حتى لو Redis اتمسح أو الإشعار اتأخر.
        from clients.services.platform_billing import open_plan_change_invoice
        open_plan_change_invoice(tenant_obj, plan_row, months, provider='paymob', reference=order_id)

        # Step 3: Payment key — use real tenant contact data when available
        billing_name = (tenant_obj.owner_name if tenant_obj else shop) or 'Customer'
        billing_name_parts = billing_name.split(maxsplit=1)
        billing = {
            'first_name': billing_name_parts[0][:50] or 'Customer',
            'last_name': billing_name_parts[1][:50] if len(billing_name_parts) > 1 else 'MoussTec',
            'email': (tenant_obj.email or 'customer@mousstec.com') if tenant_obj else 'customer@mousstec.com',
            'phone_number': (tenant_obj.phone.lstrip('+') if tenant_obj and tenant_obj.phone else '01000000000'),
            'apartment': 'NA', 'floor': 'NA', 'street': 'NA', 'building': 'NA',
            'shipping_method': 'NA', 'postal_code': 'NA', 'city': 'Cairo',
            'country': 'EG', 'state': 'Cairo',
        }
        key_res = http_requests.post('https://accept.paymob.com/api/acceptance/payment_keys', json={
            'auth_token': auth_token,
            'amount_cents': amount_cents,
            'expiration': 3600,
            'order_id': order_id,
            'billing_data': billing,
            'currency': 'EGP',
            'integration_id': integration_id_int,
            'lock_order_when_paid': 'true',
        }, timeout=15)
        if key_res.status_code not in (200, 201):
            logger.error(f"[PAYMOB] Payment key failed: HTTP {key_res.status_code} — {key_res.text[:300]}")
            messages.error(request, "فشل إصدار رمز الدفع. حاول لاحقاً.")
            return redirect(reverse('saas_pricing') + (f'?shop={shop}' if shop else ''))
        payment_token = key_res.json().get('token')
        if not payment_token:
            logger.error(f"[PAYMOB] Payment key returned no token: {key_res.text[:300]}")
            messages.error(request, "بوابة الدفع لم ترسل رمز الدفع.")
            return redirect(reverse('saas_pricing') + (f'?shop={shop}' if shop else ''))

        # Store plan info in cache for callback
        paymob_cache.set(f'paymob_order_{order_id}', {
            'plan': plan, 'shop': shop, 'amount': amount,
            'billing_period': billing_period,
        }, timeout=7200)

        # Step 4: Redirect to Paymob iframe
        iframe_url = f'https://accept.paymob.com/api/acceptance/iframes/{paymob_iframe_id}?payment_token={payment_token}'
        return redirect(iframe_url)

    except http_requests.Timeout:
        logger.error("[PAYMOB] Paymob timeout")
        messages.error(request, "بوابة الدفع لا تستجيب. حاول لاحقاً أو ادفع عبر فودافون كاش.")
        return redirect(reverse('saas_pricing') + (f'?shop={shop}' if shop else ''))
    except http_requests.RequestException as e:
        logger.error(f"[PAYMOB] Network error: {e}")
        messages.error(request, "خطأ في الاتصال ببوابة الدفع. حاول لاحقاً.")
        return redirect(reverse('saas_pricing') + (f'?shop={shop}' if shop else ''))
    except Exception as e:
        logger.exception(f"Paymob checkout unexpected error: {e}")
        messages.error(request, "حدث خطأ غير متوقع. حاول لاحقاً أو تواصل مع الدعم.")
        return redirect(reverse('saas_pricing') + (f'?shop={shop}' if shop else ''))


def _paymob_token_callback(request, data):
    from clients.services.auto_renew import save_card_from_token_callback
    from clients.services.paymob import verify_paymob_token_hmac
    ok, reason = verify_paymob_token_hmac(request, data)
    if not ok:
        return JsonResponse({'status': 'rejected', 'reason': reason}, status=403)
    obj = data.get('obj') if isinstance(data.get('obj'), dict) else {}
    status, _card = save_card_from_token_callback(obj)
    return JsonResponse({'status': status})


def _bmw_ecu_card_payment(request, order_id, meta):
    """💳 رسوم BMW ECU بالكارت (الشغل اتعمل قبل الدفع) — مفيش حاجة تتفعّل، بس الدفعة
    لازم تتسجّل للمحاسبة والعميل يرجع لصفحة نجاح.

    🐛 [FIX]: الإشعار ماكانش ليه صاحب: الفلوس تتخصم، مفيش أي أثر، والعميل يشوف «اترفض»."""
    from clients.models import PlatformEvent
    data = request.GET.dict() if request.method == 'GET' else (json.loads(request.body or b'{}') or {})
    obj = data.get('obj') if isinstance(data.get('obj'), dict) else {}
    success = str(data.get('success', obj.get('success', 'false'))).lower() == 'true'
    if not success:
        return redirect(reverse('payment_failed') + '?reason=payment_declined')
    if request.method == 'GET':   # رجوع المتصفح — التسجيل بييجي في إشعار الـ POST
        return redirect(reverse('payment_success') + '?type=service')
    try:
        paid_cents = int(obj.get('amount_cents') or 0)
        expected = int((Decimal(str(meta.get('amount') or '0')) * 100).to_integral_value())
    except (TypeError, ValueError, ArithmeticError):
        paid_cents, expected = 0, 1
    ref = f"paymob {obj.get('id', '')} / order {order_id}"
    if PlatformEvent.objects.filter(event_type='payment', description__contains=ref).exists():
        return JsonResponse({'status': 'duplicate'})
    tenant = Client.objects.filter(schema_name=meta.get('tenant_schema') or '').first()
    PlatformEvent.objects.create(
        event_type='payment', tenant_schema=meta.get('tenant_schema') or '',
        tenant_name=getattr(tenant, 'name', ''),
        description=(f"💳 رسوم BMW ECU اتدفعت بالكارت — VIN {meta.get('vin')} "
                     f"({meta.get('authorization_ref')}) — {paid_cents / 100:.2f} ج.م — {ref}"
                     + ('' if paid_cents >= expected else f" ⚠️ أقل من المطلوب ({expected / 100:.2f})")),
        metadata={'source': 'bmw_ecu', **{k: str(v) for k, v in meta.items()}, 'paid_cents': paid_cents},
    )
    return JsonResponse({'status': 'ok'})


def _route_feature_callback(request, order_id):
    """🐛 [FIX]: Paymob بيبعت كل إشعارات الحساب على رابط واحد (المضبوط في الداشبورد —
    PAYMOB_SETUP.md) و create_iframe_url بيتجاهل callback_url. فدفع القطع والتشخيص
    (و Social Studio / Omnichannel) كان بيوصل هنا ويرجع «اترفض» من غير تفعيل —
    الفلوس تتخصم والطلب يفضل مستني. بنسلّم الإشعار لصاحبه حسب رقم الطلب."""
    if not order_id:
        return None
    order_id = str(order_id)
    from clients.models import PartOrder
    if (paymob_cache.get(f'paymob_part_order_{order_id}')
            or PartOrder.objects.filter(paymob_order_id=order_id).exists()):
        from clients.views.parts_marketplace_views import parts_paymob_callback
        return parts_paymob_callback(request)

    if paymob_cache.get(f'paymob_diag_{order_id}'):
        if request.method == 'GET':   # رجوع المتصفح — التفعيل بييجي في إشعار الـ POST
            return redirect('/marketplace/diagnostics/?payment=1')
        from clients.views.customer_diagnostics_views import diagnostics_paymob_callback
        return diagnostics_paymob_callback(request)

    bmw = paymob_cache.get(f'paymob_bmwecu_{order_id}')
    if bmw:
        return _bmw_ecu_card_payment(request, order_id, bmw)

    for prefix, module, page in (('paymob_social', 'social_ads.views', '/social-studio/'),
                                 ('paymob_omni', 'omnichannel.dashboard_views', '/omnichannel/')):
        meta = paymob_cache.get(f'{prefix}_{order_id}')
        if not meta:
            continue
        if request.method == 'GET':
            tenant = Client.objects.filter(pk=meta.get('client_pk')).only('schema_name').first()
            if tenant is None:
                return redirect('/')
            base = getattr(settings, 'BASE_DOMAIN', 'mousstec.com')
            return redirect(f"https://{tenant.schema_name.replace('_', '-')}.{base}{page}")
        from importlib import import_module
        return import_module(module).paymob_callback(request)
    return None


@csrf_exempt
def paymob_callback(request):
    """
    استقبال نتيجة الدفع من Paymob بعد إتمام العملية.
    🛡️ يتحقق من توقيع HMAC لمنع التلاعب بالطلبات.
    """
    data = request.GET.dict() if request.method == 'GET' else (json.loads(request.body) if request.body else {})

    # 💳 «احفظ الكارت»: Paymob بيبعت type=TOKEN على نفس الرابط — مش عملية دفع،
    #    وتوقيعه بحقول تانية (clients/services/auto_renew.py)
    if request.method == 'POST' and isinstance(data, dict) and data.get('type') == 'TOKEN':
        return _paymob_token_callback(request, data)

    # ── 🛡️ التحقق من توقيع HMAC (fail-closed) ──
    from clients.services.paymob import verify_paymob_hmac
    ok, reason = verify_paymob_hmac(request, body_data=data)
    if not ok:
        return redirect(reverse('payment_failed') + f'?reason=signature&detail={reason}')

    success = data.get('success', data.get('obj', {}).get('success', 'false'))
    order_id = data.get('order', data.get('obj', {}).get('order', {}).get('id', ''))

    routed = _route_feature_callback(request, order_id)
    if routed is not None:
        return routed

    if str(success).lower() == 'true' and order_id:
        # Check if this is a design store purchase
        design_info = paymob_cache.get(f'paymob_design_{order_id}')
        purchase = None
        if design_info:
            purchase = DesignPurchase.objects.filter(pk=design_info['purchase_id']).first()
            if purchase is None:
                logger.error(f"[PAYMOB/DESIGN] Purchase {design_info['purchase_id']} not found")
        if purchase is None:
            # 🐛 [FIX]: الكاش ساعتين بس — المرجع المتسجّل على الطلب وقت الـ checkout
            purchase = DesignPurchase.objects.filter(payment_reference=f'paymob-order:{order_id}').first()
        if purchase is not None:
            try:
                paid_cents = int(data.get('amount_cents') or (data.get('obj') or {}).get('amount_cents') or 0)
            except (TypeError, ValueError):
                paid_cents = 0
            # 🛡️ [FIX]: المبلغ المدفوع (موقّع) لازم يغطي سعر الباقة
            if paid_cents < int((purchase.price_paid * 100).to_integral_value()):
                logger.error("🔴 [PAYMOB/DESIGN] amount mismatch purchase=%s paid=%s", purchase.pk, paid_cents)
                return redirect(reverse('payment_failed') + '?reason=amount_mismatch')
            if purchase.status != 'paid':
                purchase.status = 'paid'
                purchase.paid_at = timezone.now()
                purchase.payment_reference = str(order_id)
                purchase.save(update_fields=['status', 'paid_at', 'payment_reference'])
                logger.info(f"[PAYMOB/DESIGN] Purchase #{purchase.pk} paid via card")
            paymob_cache.delete(f'paymob_design_{order_id}')
            return redirect('/marketplace/design-store/my-designs/?payment=success')

        # 🧾 فاتورة الطلب المتسجّلة وقت الـ checkout (clients/services/platform_billing.py)
        try:
            paid_cents = int(data.get('amount_cents') or (data.get('obj') or {}).get('amount_cents') or 0)
        except (TypeError, ValueError):
            paid_cents = 0
        from clients.services.platform_billing import settle_gateway_invoice
        try:
            outcome, inv = settle_gateway_invoice('paymob', order_id, paid_cents=paid_cents)
        except Exception:
            logger.exception("🔴 [PAYMOB] settling the invoice for order %s failed", order_id)
            outcome, inv = 'error', None
        if outcome in ('paid', 'already_paid'):
            paymob_cache.delete(f'paymob_order_{order_id}')
            period = {1: 'monthly', 3: 'quarterly', 6: 'semi_annual', 12: 'annual'}.get(
                inv.billing_cycle_months, 'monthly')
            logger.info("✅ [PAYMOB] order %s → invoice %s (%s)", order_id, inv.invoice_number, outcome)
            return redirect(reverse('payment_success')
                            + f'?shop={inv.tenant.schema_name}&plan={inv.tenant.plan}&period={period}')
        if outcome in ('amount_mismatch', 'bad_status', 'error'):
            logger.error("🔴 [PAYMOB] order %s not applied: %s (paid %s cents)", order_id, outcome, paid_cents)
            shop_q = f'shop={inv.tenant.schema_name}&' if inv else ''
            return redirect(reverse('payment_failed') + f'?{shop_q}reason={outcome}')

        # طلبات اتفتحت قبل ما الفاتورة تتسجّل وقت الـ checkout (الكاش بس)
        order_info = paymob_cache.get(f'paymob_order_{order_id}')
        if order_info:
            plan = order_info.get('plan')
            shop = order_info.get('shop')
            billing_period = order_info.get('billing_period', 'monthly')
            # 🛡️ المبلغ المدفوع فعلاً (موقّع بالـ HMAC) لازم يغطي المبلغ اللي
            #    اتطلب — مايتفعّلش اشتراك على دفعة أقل.
            try:
                paid_cents = int(data.get('amount_cents') or (data.get('obj') or {}).get('amount_cents') or 0)
                expected_cents = int((Decimal(str(order_info.get('amount') or '0')) * 100).to_integral_value())
            except (TypeError, ValueError, ArithmeticError):
                paid_cents, expected_cents = 0, 1
            if paid_cents < expected_cents:
                logger.error("🔴 [PAYMOB] Amount mismatch for order %s: paid %s < expected %s",
                             order_id, paid_cents, expected_cents)
                return redirect(reverse('payment_failed') + f'?shop={shop}&reason=amount_mismatch')
            # ── خريطة أيام الفترة ──
            period_days_map = {
                'monthly': 30, 'quarterly': 90,
                'semi_annual': 180, 'annual': 365,
            }
            days_to_add = period_days_map.get(billing_period, 30)

            if shop:
                # Idempotency: لو PlatformInvoice متعمل بنفس الـ payment_reference،
                # بنرجع نجاح بدون ما نـ re-process. Paymob ممكن يبعت webhook مرتين.
                if PlatformInvoice.objects.filter(
                    payment_provider='paymob',
                    payment_reference=str(order_id),
                    status='paid',
                ).exists():
                    logger.info(f"⚠️ [PAYMOB] Duplicate callback for order {order_id} — already processed; returning success")
                    paymob_cache.delete(f'paymob_order_{order_id}')
                    return redirect(reverse('payment_success') + f'?shop={shop}&plan={plan}&period={billing_period}')

                # Resolve الـ legacy plan string → Plan FK
                target_slug = resolve_plan_slug(plan)
                if not target_slug:
                    logger.error(f"🔴 [PAYMOB] Unknown legacy plan '{plan}' for shop '{shop}' — cannot resolve to Plan.slug")
                    return redirect(reverse('payment_failed') + f'?shop={shop}&reason=plan_unknown')

                try:
                    plan_obj = Plan.objects.get(slug=target_slug)
                except Plan.DoesNotExist:
                    logger.error(f"🔴 [PAYMOB] Plan slug '{target_slug}' not found in DB — run seed migration 0011")
                    return redirect(reverse('payment_failed') + f'?shop={shop}&reason=plan_missing')

                try:
                    with transaction.atomic():
                        tenant = Client.objects.select_for_update().get(schema_name=shop)

                        # Find or create subscription
                        sub, _sub_created = TenantSubscription.objects.get_or_create(
                            tenant=tenant,
                            defaults={'plan': plan_obj, 'is_active': False},
                        )

                        # Latest revision للـ plan (يبقى عندنا revision لأن Phase 2 backfill عمل واحدة لكل Plan)
                        revision = PlanRevision.objects.filter(plan=plan_obj).order_by('-effective_from').first()
                        if revision is None:
                            # Defensive fallback: لو ما فيش revision، نـ create one من الـ plan الحالي
                            revision = PlanRevision.create_from_plan(
                                plan_obj, change_reason='Auto-created on first Paymob payment',
                            )

                        # Period dates
                        period_start = max(tenant.subscription_end_date or timezone.localdate(), timezone.localdate())
                        period_end = period_start + timedelta(days=days_to_add)

                        # Pricing — نستخدم Plan.price_for_period عشان الخصم متطابق مع صفحة الـ pricing
                        months_map = {'monthly': 1, 'quarterly': 3, 'semi_annual': 6, 'annual': 12}
                        months = months_map.get(billing_period, 1)
                        subtotal = (plan_obj.monthly_price * months).quantize(Decimal('0.01'))
                        total = plan_obj.price_for_period(months)
                        discount_amount = (subtotal - total).quantize(Decimal('0.01'))
                        discount_percent = int(round((discount_amount / subtotal) * 100)) if subtotal else 0

                        # Create + mark paid في الـ transaction نفسه
                        invoice = PlatformInvoice.objects.create(
                            tenant=tenant,
                            subscription=sub,
                            plan_revision=revision,
                            period_start=period_start,
                            period_end=period_end,
                            billing_cycle_months=months,
                            subtotal=subtotal,
                            discount_percent=discount_percent,
                            discount_amount=discount_amount,
                            total=total,
                            entitlements_snapshot=dict(plan_obj.entitlements or {}),
                            status='issued',
                            payment_provider='paymob',
                            payment_reference=str(order_id),
                        )
                        invoice.mark_paid()  # يـ trigger snapshot + extend sub + extend tenant

                        # 🪪 Legacy compat: نحدث Client.plan (CharField) عشان الـ
                        # callsites القديمة (middleware.py:96، subscription_views.py:442)
                        # تفضل تشتغل. Phase 5 هيشيل الـ CharField كله.
                        tenant.plan = plan
                        tenant.save(update_fields=['plan'])

                        paymob_cache.delete(f'paymob_order_{order_id}')
                        logger.info(
                            f"✅ Paymob payment success: {shop} → {target_slug} "
                            f"({billing_period}, +{days_to_add} days, invoice={invoice.invoice_number})"
                        )
                except Client.DoesNotExist:
                    logger.error(f"🔴 Paymob callback: tenant {shop} not found")
                except Exception as e:
                    logger.exception(f"🔴 Paymob callback failed for {shop} order={order_id}: {e}")

            return redirect(reverse('payment_success') + f'?shop={shop}&plan={plan}&period={billing_period}')

    return redirect(reverse('payment_failed') + '?reason=payment_declined')


# =====================================================================
# 🧩 9. محرك شراء الإضافات بالتناسب الزمني (Pro-Rated Addon Engine)
# =====================================================================

class _AddonFundsError(Exception):
    pass


def _buy_addon(tenant, addon_type, qty, labels):
    """يشتري إضافة (موظف/فرع/خزينة) ويخصم ثمنها من المحفظة فعلاً.

    🐛 [FIX]: الإضافة كانت بتتفعّل فوراً ويتسجّل «خصم» في الدفتر، لكن
    الـ ledger signal مابيلمسش المحفظة لـ fee_deduction — يعني الإضافات كانت
    مجانية. دلوقتي نفس طريقة Social Studio/Omnichannel: نخصم من المحفظة
    (ولو الرصيد مش كفاية → رسالة اشحن المحفظة، ومفيش تفعيل).
    """
    from django.db.models import F as _F
    prorated = tenant.calculate_prorated_addon_cost()
    total_cost = prorated * qty
    with transaction.atomic():
        t = Client.objects.select_for_update().get(pk=tenant.pk)
        if t.wallet_balance < total_cost:
            raise _AddonFundsError(
                f"رصيد محفظتك ({t.wallet_balance} {_sym()}) مش كفاية لـ {qty} {labels[addon_type]} "
                f"({total_cost} {_sym()}). اشحن المحفظة الأول.")
        if addon_type == 'employee':
            t.extra_users_purchased += qty
        elif addon_type == 'branch':
            t.extra_branches_purchased += qty
        elif addon_type == 'treasury':
            t.extra_treasuries_purchased += qty
        t.save()
        Client.objects.filter(pk=t.pk).update(wallet_balance=_F('wallet_balance') - total_cost)
        EscrowLedger.objects.create(
            client=t, transaction_type='fee_deduction', amount=total_cost,
            description=f"شراء {qty} {labels[addon_type]} إضافي — {prorated} {_sym()}/وحدة (تناسبي)"
        )
    return prorated, total_cost


@login_required(login_url='/login/')
def manage_subscription(request):
    tenant = getattr(request, 'tenant', None)
    if not tenant or tenant.schema_name == 'public':
        return redirect('/')

    addon_labels = {'employee': 'موظف', 'branch': 'فرع', 'treasury': 'خزينة'}
    result_msg = None
    from django.utils.translation import gettext as _gettext
    from clients.services import auto_renew as _auto_renew
    renew_action = request.POST.get('action', '') if request.method == 'POST' else ''

    if renew_action in ('auto_renew_on', 'auto_renew_off', 'remove_card') and request.user.is_superuser:
        if renew_action == 'remove_card':
            _auto_renew.remove_card(tenant)
            result_msg = _gettext("تم حذف الكارت المحفوظ وإيقاف التجديد التلقائي.")
        elif _auto_renew.set_auto_renew(tenant, renew_action == 'auto_renew_on'):
            result_msg = (_gettext("تم تفعيل التجديد التلقائي.") if renew_action == 'auto_renew_on'
                          else _gettext("تم إيقاف التجديد التلقائي."))
        else:
            result_msg = "⚠️ " + _gettext("التجديد التلقائي محتاج باقة وكارت محفوظ.")
    elif request.method == 'POST' and request.user.is_superuser:
        addon_type = request.POST.get('addon_type')
        try:
            qty = int(request.POST.get('quantity', 1))
        except (TypeError, ValueError):
            qty = 0
        if addon_type in addon_labels and 1 <= qty <= 10:
            try:
                _prorated, total_cost = _buy_addon(tenant, addon_type, qty, addon_labels)
            except _AddonFundsError as exc:
                if is_uae_tenant(tenant):   # الإيصال بالجنيه — الإماراتي عن طريق الدعم
                    result_msg = f"⚠️ {exc} كلّمنا على واتساب نفعّلها لك."
                else:
                    return _addon_receipt_redirect(tenant, addon_type, qty)
            else:
                tenant.refresh_from_db()
                result_msg = f"تم إضافة {qty} {addon_labels[addon_type]} بنجاح — اتخصم من المحفظة: {total_cost} {_sym()}"


    prorated_cost = tenant.calculate_prorated_addon_cost()
    remaining_days = 0
    if tenant.subscription_end_date:
        remaining_days = max((tenant.subscription_end_date - timezone.now().date()).days, 0)

    # ── Build available plans for this industry ──
    # 🐛 [FIX]: الأسعار والحدود كانت مكتوبة بإيدينا (550/850/2500…) — أي تعديل سعر من
    #    لوحة المالك مايظهرش هنا، والشركة الإماراتية كانت بتشوف «550 د.إ» بدل 42.
    #    دلوقتي من جدول Plan (سعر الدرهم للشركات الإماراتية) زي صفحة الأسعار.
    industry = getattr(tenant, 'industry', 'automotive')
    plan_style = {
        'silver': ('لمراكز الصيانة وتجار قطع الغيار', 'fa-car', 'slate'),
        'gold': ('لمراكز الصيانة وتجار قطع الغيار الشامل', 'fa-crown', 'yellow'),
        'empire': ('لتجار القطع والشركات الكبيرة', 'fa-gem', 'purple'),
        'print_basic': ('للمطابع الصغيرة واستوديوهات التصميم', 'fa-print', 'pink'),
        'print_pro': ('للمطابع المتوسطة ومكاتب التصميم', 'fa-palette', 'purple'),
        'print_enterprise': ('للمطابع الكبيرة ومجموعات التصميم', 'fa-building', 'amber'),
    }
    from clients.services.plan_mapping import legacy_plan_value
    is_ae = is_uae_tenant(tenant)
    available_plans = []
    for p in Plan.objects.filter(is_active=True, industry=industry).order_by('sort_order'):
        key = legacy_plan_value(p.slug)
        desc, icon, color = plan_style.get(key, ('', 'fa-crown', 'purple'))
        aed = p.monthly_price_aed or 0
        available_plans.append({
            'key': key, 'name': p.name, 'desc': desc,
            'price': aed if (is_ae and aed > 0) else p.monthly_price,
            'users': p.max_users, 'branches': p.max_branches, 'treasuries': p.max_treasuries,
            'icon': icon, 'color': color,
        })

    # ── AI Design packages (one-time purchase from DesignPackage model) ──
    from clients.models import DesignPackage
    customer_ai_pkgs = list(DesignPackage.objects.filter(
        is_active=True, target_audience='customer'
    ).order_by('sort_order'))
    designer_ai_pkgs = list(DesignPackage.objects.filter(
        is_active=True, target_audience='designer'
    ).order_by('sort_order'))

    return render(request, 'clients/manage_subscription.html', {
        'tenant': tenant,
        'prorated_cost': prorated_cost,
        'full_addon_price': float(Client.ADDON_PRICE_PER_MONTH),
        'remaining_days': remaining_days,
        'result_msg': result_msg,
        'available_plans': available_plans,
        'customer_ai_pkgs': customer_ai_pkgs,
        'designer_ai_pkgs': designer_ai_pkgs,
        'current_plan': tenant.plan,
        # 🛟 «دعم فني أولوية» كان مكتوب في Empire/Enterprise ومفيش حاجة بتنفّذه — خط مباشر مميّز
        'priority_support': _has_feature(tenant, 'priority_support'),
        'support_whatsapp': settings.SUPPORT_WHATSAPP,
        'card_payments_enabled': paymob_configured(),
        'ADMIN_URL': os.getenv('ADMIN_URL', 'secure-portal'),
        **_auto_renew_context(tenant, request.user),
    })


def _addon_receipt_redirect(tenant, addon_type, qty):
    """🐛 [FIX]: المحفظة كانت الطريق الوحيد للإضافات ومفيش شحن ليها — دلوقتي الإضافة بتتدفع
    بفودافون كاش / إنستاباي وتتفعّل لما الإيصال يتأكد (ManualPaymentReceipt.confirm)."""
    from clients.models import ManualPaymentReceipt
    receipt = ManualPaymentReceipt.objects.create(
        purchase_type='addon', purchase_id=tenant.pk,
        amount=tenant.calculate_prorated_addon_cost() * qty,
        payment_method='vodafone_cash', tenant=tenant,
        contact_name=tenant.owner_name or tenant.name, contact_phone=tenant.phone or '',
        notes=f'addon={addon_type} qty={qty}', sender_phone='', txn_reference='',
    )
    return redirect(reverse('manual_payment_upload', args=[receipt.receipt_code]))


def _has_feature(tenant, code):
    from clients.services.entitlements import EntitlementService
    try:
        return EntitlementService.has(tenant, code)
    except Exception:
        return False


def _billing_tenant_or_none(request):
    tenant = getattr(request, 'tenant', None)
    if not tenant or tenant.schema_name == 'public' or not request.user.is_superuser:
        return None
    return tenant


@login_required(login_url='/login/')
def subscription_invoices(request):
    """🧾 «فواتيري»: كل فواتير اشتراك الشركة بحالتها — قبل كده كانت ظاهرة لمالك المنصة بس.
    الفاتورة المستحقة بإيصال لسه مارفعش بتوصّل لصفحة رفع الإيصال."""
    tenant = _billing_tenant_or_none(request)
    if tenant is None:
        return redirect('manage_subscription')
    from clients.models import ManualPaymentReceipt
    invoices = list(PlatformInvoice.objects.filter(tenant=tenant)
                    .select_related('plan_revision__plan').order_by('-issued_at')[:200])
    pending = {
        r.purchase_id: r.receipt_code for r in ManualPaymentReceipt.objects.filter(
            purchase_type='subscription', status='pending', txn_reference='',
            purchase_id__in=[i.pk for i in invoices if i.status == 'issued'])
    }
    for inv in invoices:
        inv.upload_code = pending.get(inv.pk)
    return render(request, 'clients/subscription_invoices.html', {
        'tenant': tenant, 'invoices': invoices,
        'ADMIN_URL': os.getenv('ADMIN_URL', 'secure-portal'),
    })


@login_required(login_url='/login/')
def subscription_invoice_detail(request, invoice_number):
    """🧾 فاتورة واحدة للطباعة / الحفظ PDF من المتصفح."""
    tenant = _billing_tenant_or_none(request)
    if tenant is None:
        return redirect('manage_subscription')
    inv = (PlatformInvoice.objects.filter(tenant=tenant, invoice_number=invoice_number)
           .select_related('plan_revision__plan').first())
    if inv is None:
        from django.http import Http404
        raise Http404
    inv.plan_subtotal = inv.subtotal - inv.addons_amount
    return render(request, 'clients/subscription_invoice_detail.html', {'tenant': tenant, 'inv': inv})


def _auto_renew_context(tenant, user):
    """كارت الشركة المحفوظ وحالة التجديد التلقائي (يظهر بس لو PAYMOB_AUTO_RENEW شغّال)."""
    from clients.services.auto_renew import active_card, auto_renew_enabled
    from clients.services.platform_billing import renewal_amount
    if not auto_renew_enabled():
        return {'auto_renew_available': False}
    sub = TenantSubscription.objects.filter(tenant=tenant).select_related('plan').first()
    months = (sub.billing_cycle_months or 1) if sub else 1
    return {
        'auto_renew_available': True,
        'saved_card': active_card(tenant),
        'auto_renew_on': bool(sub and sub.auto_renew),
        'renew_amount': renewal_amount(tenant, sub.plan, months) if sub and sub.plan else None,
        'renew_months': months,
        'can_manage_billing': user.is_superuser,
    }


@login_required(login_url='/login/')
def purchase_addon_api(request):
    if request.method != 'POST':
        return JsonResponse({"error": "POST Only"}, status=400)
    tenant = getattr(request, 'tenant', None)
    if not tenant or tenant.schema_name == 'public':
        return JsonResponse({"error": "متاح للمؤسسات فقط"}, status=403)
    if not request.user.is_superuser:
        return JsonResponse({"error": "فقط المدير المسؤول يمكنه شراء الإضافات"}, status=403)

    try:
        data = json.loads(request.body)
        addon_type = data.get('addon_type')
        qty = int(data.get('quantity', 1))
        addon_labels = {'employee': 'موظف', 'branch': 'فرع', 'treasury': 'خزينة'}

        if addon_type not in addon_labels:
            return JsonResponse({"error": "نوع الإضافة غير صالح"}, status=400)
        if qty < 1 or qty > 10:
            return JsonResponse({"error": "الكمية يجب أن تكون بين 1 و 10"}, status=400)

        try:
            prorated, total_cost = _buy_addon(tenant, addon_type, qty, addon_labels)
        except _AddonFundsError as exc:
            return JsonResponse({"error": str(exc), "needs_topup": True}, status=402)

        remaining_days = 0
        if tenant.subscription_end_date:
            remaining_days = max((tenant.subscription_end_date - timezone.now().date()).days, 0)

        return JsonResponse({
            "status": "success", "addon_type": addon_type, "quantity": qty,
            "cost_per_unit": float(prorated), "total_cost": float(total_cost),
            "remaining_days": remaining_days,
            "message": f"تم إضافة {qty} {addon_labels[addon_type]} بنجاح — التكلفة: {total_cost} {_sym()}"
        })
    except Exception as e:
        logger.error("[ADDON] purchase_addon_api error: %s", e)
        return JsonResponse({"error": "حدث خطأ أثناء شراء الإضافة. حاول مرة أخرى."}, status=500)



# =====================================================================
# ✅ صفحات نتيجة الدفع (Payment Result Pages)
# =====================================================================
def payment_success(request):
    """
    صفحة نجاح الدفع — تُعرض بعد اكتمال أي عملية دفع ناجحة.
    تدعم: اشتراك SaaS، Design Store، Parts Marketplace.
    """
    shop         = request.GET.get('shop', '')
    plan         = request.GET.get('plan', '')
    period       = request.GET.get('period', 'monthly')
    order_code   = request.GET.get('order', '')
    context_type = request.GET.get('type', 'subscription')  # subscription | design | parts

    period_labels = {
        'monthly': 'شهري', 'quarterly': 'ربع سنوي',
        'semi_annual': 'نصف سنوي', 'annual': 'سنوي',
    }

    tenant = None
    if shop:
        tenant = Client.objects.filter(schema_name=shop).only('name', 'subscription_end_date', 'plan').first()

    return render(request, 'clients/payment_success.html', {
        'shop': shop,
        'plan': plan,
        'period_label': period_labels.get(period, period),
        'order_code': order_code,
        'context_type': context_type,
        'tenant': tenant,
        'support_whatsapp': settings.SUPPORT_WHATSAPP,
        # 🐛 [FIX]: «انطلق للوحة التحكم» كان /<shop>/ على الدومين الرئيسي (404) — اللوحة على الـ subdomain
        'dashboard_url': (f"https://{tenant.schema_name.replace('_', '-')}."
                          f"{getattr(settings, 'BASE_DOMAIN', 'mousstec.com')}/{ADMIN_URL}/") if tenant else '/',
    })


def payment_failed(request):
    """
    صفحة فشل الدفع — مع سبب واضح وخيارات المحاولة من جديد.
    """
    reason = request.GET.get('reason', 'unknown')
    shop   = request.GET.get('shop', '')

    reason_messages = {
        'payment_declined':   'تم رفض عملية الدفع من البنك أو بوابة الدفع.',
        'signature':          'فشل التحقق من توقيع العملية — قد تكون العملية مزورة.',
        'plan_unknown':       'الباقة المختارة غير معروفة. تواصل مع الدعم.',
        'plan_missing':       'الباقة غير موجودة في النظام. تواصل مع الدعم.',
        'timeout':            'انتهت مهلة الاتصال ببوابة الدفع. حاول مرة أخرى.',
        # 🧾 الدفع اتسجّل بس ماتطبّقش — ماتدفعش تاني، الدعم بيراجع الفاتورة
        'amount_mismatch':    'المبلغ المدفوع أقل من قيمة الباقة — مافعّلناش الاشتراك. تواصل مع الدعم برقم العملية ومش هتدفع تاني.',
        'bad_status':         'العملية دي متسجّلة على فاتورة اتقفلت قبل كده. تواصل مع الدعم برقم العملية — ماتدفعش تاني.',
        'error':              'الدفع وصلنا بس حصل خطأ وإحنا بنفعّل الاشتراك. فريق الدعم اتبلّغ — ماتدفعش تاني.',
        'unknown':            'حدث خطأ غير متوقع. حاول مرة أخرى أو تواصل مع الدعم.',
    }

    return render(request, 'clients/payment_failed.html', {
        'reason': reason,
        'reason_message': reason_messages.get(reason, reason_messages['unknown']),
        'shop': shop,
        'retry_url': reverse('saas_pricing') + (f'?shop={shop}' if shop else ''),
        'support_whatsapp': settings.SUPPORT_WHATSAPP,
    })


# =====================================================================
# 📚 صفحة المميزات الكاملة
# =====================================================================
def features_page(request):
    return render(request, 'clients/features.html')

