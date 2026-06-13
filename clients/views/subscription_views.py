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
from clients.services.plan_mapping import LEGACY_TO_PLAN_SLUG, resolve_plan_slug

logger = logging.getLogger('mouss_tec_core')
ADMIN_URL = os.getenv('ADMIN_URL', 'secure-portal')


# =====================================================================
# 💳 8. بوابة الاشتراكات والباقات (SaaS Pricing & Retention)
# =====================================================================
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
            if not request.user.is_superuser:
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

    try:
        feature_labels = dict(
            Feature.objects.filter(is_active=True).values_list('code', 'name_ar')
        )
    except Exception:
        logger.exception("[PRICING] failed to load Feature catalog — rendering without labels")
        feature_labels = {}

    try:
        plan_qs = Plan.objects.filter(is_active=True).order_by('industry', 'sort_order')
        for p in plan_qs:
            try:
                ents = p.entitlements if isinstance(p.entitlements, dict) else {}
                enabled_codes = [
                    code for code, cfg in ents.items()
                    if isinstance(cfg, dict) and cfg.get('enabled')
                ]
                plans_by_industry.setdefault(p.industry, []).append({
                    'slug': p.slug,
                    'legacy_slug': plan_slug_to_legacy.get(p.slug, p.slug),
                    'name': p.name,
                    'monthly_price': p.monthly_price,
                    'max_users': p.max_users,
                    'max_branches': p.max_branches,
                    'max_treasuries': p.max_treasuries,
                    'monthly_ai_designs_quota': p.monthly_ai_designs_quota,
                    'features': list(p.features or []),
                    'entitlement_labels': [
                        feature_labels[c] for c in enabled_codes if c in feature_labels
                    ],
                })
            except Exception:
                logger.exception("[PRICING] skipped Plan id=%s — bad row", getattr(p, 'pk', '?'))
    except Exception:
        logger.exception("[PRICING] failed to load Plan catalog — rendering empty pricing page")

    # convention: middle plan in each industry = "most popular"
    for plans_list in plans_by_industry.values():
        for idx, plan_dict in enumerate(plans_list):
            plan_dict['is_popular'] = (len(plans_list) >= 3 and idx == 1)

    try:
        return render(request, 'clients/pricing.html', {
            'tenant': tenant, 'shop': shop_schema,
            'plans_by_industry': plans_by_industry,
            'pricing': {
                'addon_price': 125,
                'free_trial_days': 3,
                'vodafone_cash': '',
                'billing_discounts': billing_discounts,
            }
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

    # تحقق من المبلغ
    try:
        amount_value = float(amount)
        if amount_value <= 0:
            raise ValueError("non-positive")
        amount_cents = int(amount_value * 100)
    except (TypeError, ValueError):
        messages.error(request, "المبلغ غير صحيح.")
        return redirect(reverse('saas_pricing') + (f'?shop={shop}' if shop else ''))

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

        # Step 3: Payment key — use real tenant contact data when available
        tenant_obj = Client.objects.filter(schema_name=shop).first() if shop else None
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
        cache.set(f'paymob_order_{order_id}', {
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


@csrf_exempt
def paymob_callback(request):
    """
    استقبال نتيجة الدفع من Paymob بعد إتمام العملية.
    🛡️ يتحقق من توقيع HMAC لمنع التلاعب بالطلبات.
    """
    data = request.GET.dict() if request.method == 'GET' else (json.loads(request.body) if request.body else {})

    # ── 🛡️ التحقق من توقيع HMAC (fail-closed) ──
    from clients.services.paymob import verify_paymob_hmac
    ok, reason = verify_paymob_hmac(request, body_data=data)
    if not ok:
        return redirect(reverse('payment_failed') + f'?reason=signature&detail={reason}')

    success = data.get('success', data.get('obj', {}).get('success', 'false'))
    order_id = data.get('order', data.get('obj', {}).get('order', {}).get('id', ''))

    if str(success).lower() == 'true' and order_id:
        # Check if this is a design store purchase
        design_info = cache.get(f'paymob_design_{order_id}')
        if design_info:
            try:
                purchase = DesignPurchase.objects.get(pk=design_info['purchase_id'])
                if purchase.status != 'paid':
                    purchase.status = 'paid'
                    purchase.payment_reference = str(order_id)
                    purchase.save(update_fields=['status', 'payment_reference'])
                    logger.info(f"[PAYMOB/DESIGN] Purchase #{purchase.pk} paid via card")
                cache.delete(f'paymob_design_{order_id}')
                return redirect(f'/marketplace/design-store/my-designs/?payment=success')
            except DesignPurchase.DoesNotExist:
                logger.error(f"[PAYMOB/DESIGN] Purchase {design_info['purchase_id']} not found")

        order_info = cache.get(f'paymob_order_{order_id}')
        if order_info:
            plan = order_info.get('plan')
            shop = order_info.get('shop')
            billing_period = order_info.get('billing_period', 'monthly')
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
                    cache.delete(f'paymob_order_{order_id}')
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

                        cache.delete(f'paymob_order_{order_id}')
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
@login_required(login_url='/login/')
def manage_subscription(request):
    tenant = getattr(request, 'tenant', None)
    if not tenant or tenant.schema_name == 'public':
        return redirect('/')

    addon_labels = {'employee': 'موظف', 'branch': 'فرع', 'treasury': 'خزينة'}
    result_msg = None

    if request.method == 'POST' and request.user.is_superuser:
        addon_type = request.POST.get('addon_type')
        qty = int(request.POST.get('quantity', 1))
        if addon_type in addon_labels and 1 <= qty <= 10:
            prorated = tenant.calculate_prorated_addon_cost()
            total_cost = prorated * qty
            with transaction.atomic():
                t = Client.objects.select_for_update().get(pk=tenant.pk)
                if addon_type == 'employee':
                    t.extra_users_purchased += qty
                elif addon_type == 'branch':
                    t.extra_branches_purchased += qty
                elif addon_type == 'treasury':
                    t.extra_treasuries_purchased += qty
                t.save()
                EscrowLedger.objects.create(
                    client=t, transaction_type='fee_deduction', amount=total_cost,
                    description=f"شراء {qty} {addon_labels[addon_type]} إضافي — {prorated} ج.م/وحدة (تناسبي)"
                )
            tenant.refresh_from_db()
            result_msg = f"تم إضافة {qty} {addon_labels[addon_type]} بنجاح — التكلفة: {total_cost} ج.م"

    prorated_cost = tenant.calculate_prorated_addon_cost()
    remaining_days = 0
    if tenant.subscription_end_date:
        remaining_days = max((tenant.subscription_end_date - timezone.now().date()).days, 0)

    # ── Build available plans for this industry ──
    industry = getattr(tenant, 'industry', 'automotive')
    if industry == 'printing':
        available_plans = [
            {'key': 'print_basic', 'name': 'Print Starter', 'desc': 'للمطابع الصغيرة واستوديوهات التصميم', 'price': 875, 'users': 1, 'branches': 1, 'treasuries': 1, 'icon': 'fa-print', 'color': 'pink'},
            {'key': 'print_pro', 'name': 'Print Pro', 'desc': 'للمطابع المتوسطة ومكاتب التصميم', 'price': 1250, 'users': 4, 'branches': 2, 'treasuries': 2, 'icon': 'fa-palette', 'color': 'purple'},
            {'key': 'print_enterprise', 'name': 'Print Enterprise', 'desc': 'للمطابع الكبيرة ومجموعات التصميم', 'price': 2000, 'users': 6, 'branches': 4, 'treasuries': 6, 'icon': 'fa-building', 'color': 'amber'},
        ]
    else:
        available_plans = [
            {'key': 'silver', 'name': 'سيلفر', 'desc': 'لمراكز الصيانة وتجار قطع الغيار', 'price': 550, 'users': 1, 'branches': 1, 'treasuries': 1, 'icon': 'fa-car', 'color': 'slate'},
            {'key': 'gold', 'name': 'جولد', 'desc': 'لمراكز الصيانة وتجار قطع الغيار الشامل', 'price': 850, 'users': 4, 'branches': 2, 'treasuries': 2, 'icon': 'fa-crown', 'color': 'yellow'},
            {'key': 'empire', 'name': 'Empire', 'desc': 'لتجار القطع والشركات الكبيرة', 'price': 2500, 'users': 10, 'branches': 5, 'treasuries': 4, 'icon': 'fa-gem', 'color': 'purple'},
        ]

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
        'ADMIN_URL': os.getenv('ADMIN_URL', 'secure-portal'),
    })


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

        prorated = tenant.calculate_prorated_addon_cost()
        total_cost = prorated * qty

        with transaction.atomic():
            t = Client.objects.select_for_update().get(pk=tenant.pk)
            if addon_type == 'employee':
                t.extra_users_purchased += qty
            elif addon_type == 'branch':
                t.extra_branches_purchased += qty
            elif addon_type == 'treasury':
                t.extra_treasuries_purchased += qty
            t.save()
            EscrowLedger.objects.create(
                client=t, transaction_type='fee_deduction', amount=total_cost,
                description=f"شراء {qty} {addon_labels[addon_type]} إضافي — {prorated} ج.م/وحدة (تناسبي)"
            )

        remaining_days = 0
        if tenant.subscription_end_date:
            remaining_days = max((tenant.subscription_end_date - timezone.now().date()).days, 0)

        return JsonResponse({
            "status": "success", "addon_type": addon_type, "quantity": qty,
            "cost_per_unit": float(prorated), "total_cost": float(total_cost),
            "remaining_days": remaining_days,
            "message": f"تم إضافة {qty} {addon_labels[addon_type]} بنجاح — التكلفة: {total_cost} ج.م"
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
        'unknown':            'حدث خطأ غير متوقع. حاول مرة أخرى أو تواصل مع الدعم.',
    }

    return render(request, 'clients/payment_failed.html', {
        'reason': reason,
        'reason_message': reason_messages.get(reason, reason_messages['unknown']),
        'shop': shop,
        'retry_url': reverse('saas_pricing') + (f'?shop={shop}' if shop else ''),
    })


# =====================================================================
# 📚 صفحة المميزات الكاملة
# =====================================================================
def features_page(request):
    return render(request, 'clients/features.html')

