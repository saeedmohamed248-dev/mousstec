"""
SaaS subscription endpoints — pricing page, Paymob checkout/callback,
self-serve subscription management, addon purchasing, and the public
features page.

Paymob callback is the one csrf_exempt endpoint in this module (external
caller, HMAC-verified). Everything else is csrf-protected.
"""
from __future__ import annotations

import hashlib
import hmac
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
from django.views.decorators.csrf import csrf_exempt

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
    feature_labels = dict(
        Feature.objects.filter(is_active=True).values_list('code', 'name_ar')
    )
    plan_slug_to_legacy = {v: k for k, v in LEGACY_TO_PLAN_SLUG.items()}

    plans_by_industry: dict[str, list[dict]] = {'automotive': [], 'printing': []}
    for p in Plan.objects.filter(is_active=True).order_by('industry', 'sort_order'):
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

    # convention: middle plan in each industry = "most popular"
    for plans_list in plans_by_industry.values():
        for idx, plan_dict in enumerate(plans_list):
            plan_dict['is_popular'] = (len(plans_list) >= 3 and idx == 1)

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

        # Step 3: Payment key
        billing = {
            'first_name': shop or 'Customer', 'last_name': 'MoussTec',
            'email': 'customer@mousstec.com', 'phone_number': '01000000000',
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
    data = request.GET.dict() if request.method == 'GET' else json.loads(request.body) if request.body else {}

    # ── 🛡️ التحقق من توقيع HMAC (يمنع تزوير طلبات الدفع) ──
    paymob_hmac_secret = os.getenv('PAYMOB_HMAC_SECRET', '')
    received_hmac = request.GET.get('hmac', '') or data.get('hmac', '')

    if paymob_hmac_secret:
        # Paymob HMAC concatenation order (alphabetical by key name):
        # amount_cents, created_at, currency, error_occured, has_parent_transaction,
        # id, integration_id, is_3d_secure, is_auth, is_capture, is_refunded,
        # is_standalone_payment, is_voided, order.id, owner, pending,
        # source_data.pan, source_data.sub_type, source_data.type, success
        obj = data.get('obj', {})
        if not obj and request.method == 'GET':
            # GET callback — obj fields are flat in query params
            hmac_fields = [
                str(data.get('amount_cents', '')),
                str(data.get('created_at', '')),
                str(data.get('currency', '')),
                str(data.get('error_occured', '')),
                str(data.get('has_parent_transaction', '')),
                str(data.get('id', '')),
                str(data.get('integration_id', '')),
                str(data.get('is_3d_secure', '')),
                str(data.get('is_auth', '')),
                str(data.get('is_capture', '')),
                str(data.get('is_refunded', '')),
                str(data.get('is_standalone_payment', '')),
                str(data.get('is_voided', '')),
                str(data.get('order', '')),
                str(data.get('owner', '')),
                str(data.get('pending', '')),
                str(data.get('source_data.pan', data.get('source_data_pan', ''))),
                str(data.get('source_data.sub_type', data.get('source_data_sub_type', ''))),
                str(data.get('source_data.type', data.get('source_data_type', ''))),
                str(data.get('success', '')),
            ]
        else:
            # POST callback — obj is nested JSON
            source_data = obj.get('source_data', {})
            order_obj = obj.get('order', {})
            hmac_fields = [
                str(obj.get('amount_cents', '')),
                str(obj.get('created_at', '')),
                str(obj.get('currency', '')),
                str(obj.get('error_occured', '')),
                str(obj.get('has_parent_transaction', '')),
                str(obj.get('id', '')),
                str(obj.get('integration_id', '')),
                str(obj.get('is_3d_secure', '')),
                str(obj.get('is_auth', '')),
                str(obj.get('is_capture', '')),
                str(obj.get('is_refunded', '')),
                str(obj.get('is_standalone_payment', '')),
                str(obj.get('is_voided', '')),
                str(order_obj.get('id', '')),
                str(obj.get('owner', '')),
                str(obj.get('pending', '')),
                str(source_data.get('pan', '')),
                str(source_data.get('sub_type', '')),
                str(source_data.get('type', '')),
                str(obj.get('success', '')),
            ]

        concatenated = ''.join(hmac_fields)
        computed_hmac = hmac.new(
            paymob_hmac_secret.encode('utf-8'),
            concatenated.encode('utf-8'),
            hashlib.sha512,
        ).hexdigest()

        if not hmac.compare_digest(computed_hmac, received_hmac):
            logger.critical(f"🚨 [PAYMOB HMAC MISMATCH] IP: {request.META.get('REMOTE_ADDR')} — Possible payment forgery attempt!")
            return redirect(reverse('saas_pricing') + '?payment=failed&reason=signature')
    else:
        logger.warning("⚠️ [PAYMOB] PAYMOB_HMAC_SECRET not configured — HMAC verification skipped!")

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
                    return redirect(reverse('saas_pricing') + f'?shop={shop}&payment=success')

                # Resolve الـ legacy plan string → Plan FK
                target_slug = resolve_plan_slug(plan)
                if not target_slug:
                    logger.error(f"🔴 [PAYMOB] Unknown legacy plan '{plan}' for shop '{shop}' — cannot resolve to Plan.slug")
                    return redirect(reverse('saas_pricing') + f'?shop={shop}&payment=failed&reason=plan_unknown')

                try:
                    plan_obj = Plan.objects.get(slug=target_slug)
                except Plan.DoesNotExist:
                    logger.error(f"🔴 [PAYMOB] Plan slug '{target_slug}' not found in DB — run seed migration 0011")
                    return redirect(reverse('saas_pricing') + f'?shop={shop}&payment=failed&reason=plan_missing')

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

            return redirect(reverse('saas_pricing') + f'?shop={shop}&payment=success')

    return redirect(reverse('saas_pricing') + '?payment=failed')


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
            {'key': 'print_basic', 'name': 'Print Basic', 'desc': 'للمطابع الصغيرة واستوديوهات التصميم', 'price': 550, 'users': 2, 'branches': 1, 'treasuries': 1, 'icon': 'fa-print', 'color': 'pink'},
            {'key': 'print_pro', 'name': 'Print Pro', 'desc': 'للمطابع المتوسطة ومكاتب التصميم', 'price': 880, 'users': 5, 'branches': 2, 'treasuries': 2, 'icon': 'fa-palette', 'color': 'purple'},
            {'key': 'print_enterprise', 'name': 'Print Enterprise', 'desc': 'للمطابع الكبيرة ومجموعات التصميم', 'price': 2000, 'users': 15, 'branches': 5, 'treasuries': 5, 'icon': 'fa-building', 'color': 'amber'},
        ]
    else:
        available_plans = [
            {'key': 'silver', 'name': 'سيلفر', 'desc': 'لمراكز الصيانة وتجار قطع الغيار', 'price': 685, 'users': 1, 'branches': 1, 'treasuries': 1, 'icon': 'fa-car', 'color': 'slate'},
            {'key': 'gold', 'name': 'جولد', 'desc': 'لمراكز الصيانة وتجار قطع الغيار الشامل', 'price': 1185, 'users': 4, 'branches': 2, 'treasuries': 2, 'icon': 'fa-crown', 'color': 'yellow'},
            {'key': 'empire', 'name': 'Empire', 'desc': 'لتجار القطع والشركات الكبيرة', 'price': 3000, 'users': 15, 'branches': 5, 'treasuries': 5, 'icon': 'fa-gem', 'color': 'purple'},
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
# 📚 صفحة المميزات الكاملة
# =====================================================================
def features_page(request):
    return render(request, 'clients/features.html')

