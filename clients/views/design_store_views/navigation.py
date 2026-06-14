"""
🛍️ Design Store — marketplace customer AI-design endpoints.

All 14 design-store endpoints (browse, buy, generate, regenerate, refine,
download, watermark, send-to-print, chat history, send-to-marketplace).
Heavy AI lifting is delegated to ``_ai_pipeline._run_marketplace_image_pipeline``
so C1/C2/C3 all share the unified Brand + Smart Router + Composite +
Quality-Gate pipeline.

Extracted from ``_legacy.py`` (Step 4 of the incremental split). The
package facade (``clients/views/__init__.py``) preserves the public URL
surface — ``erp_core/urls.py`` continues to reference ``client_views.<name>``
unchanged.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

from clients.models import (
    CustomerDesign,
    DesignPackage,
    DesignPurchase,
    MarketplaceCustomer,
)

from .._ai_pipeline import (
    _composite_brand_logo,
    _persist_remote_image,
    _resolve_brand_context,
    _resolve_quality_size,
    _run_marketplace_image_pipeline,
    _upscale_local_image,
)
from .._shared import (
    _build_customer_topup_cards,
    _marketplace_auth,
)

logger = logging.getLogger('mouss_tec_core')



# Storefront navigation: home, buy/payment flow, my orders, my designs.



def _enforce_printing_sector(request, customer, *, json_response=False):
    """Block automotive-sector customers from entering the design store.

    The design store is part of the printing market only — automotive
    customers shouldn't see it from their dashboard, and even if they
    arrive via a hand-crafted URL, this gate sends them back with a
    clear message instead of letting the sector boundary leak.
    """
    if customer is None or customer.sector == 'printing':
        return None
    msg = "متجر التصميمات متاح لعملاء سوق الطباعة والتصميم فقط."
    if json_response:
        return JsonResponse({
            "error": msg,
            "code": "wrong_sector",
            "redirect": "/marketplace/dashboard/",
        }, status=403)
    try:
        messages.warning(request, msg)
    except Exception:
        pass
    return redirect('/marketplace/dashboard/')


# ───────────────────────────────────────────────────────────────────────────
# 🛠️ Shared helper — create Paymob iframe URL for a DesignPurchase
# Used by both `design_store_buy` and `copilot_topup_purchase` so the
# checkout flow is identical regardless of entry point.
# ───────────────────────────────────────────────────────────────────────────
def create_paymob_iframe_for_purchase(request, purchase, customer):
    """
    Build a Paymob iframe URL for the given DesignPurchase.

    Returns (iframe_url, error_message). On success error_message is None.
    On failure iframe_url is None.
    """
    import requests as http_requests

    paymob_api_key        = getattr(settings, 'PAYMOB_API_KEY', '')
    paymob_integration_id = getattr(settings, 'PAYMOB_INTEGRATION_ID', '')
    paymob_iframe_id      = getattr(settings, 'PAYMOB_IFRAME_ID', '')

    if not paymob_api_key:
        return None, "الدفع بالبطاقة غير متاح حالياً"
    try:
        integration_id_int = int(paymob_integration_id)
    except (TypeError, ValueError):
        return None, "إعدادات بوابة الدفع غير صحيحة"
    if not paymob_iframe_id:
        return None, "إعدادات بوابة الدفع غير مكتملة"

    amount_cents = int(float(purchase.price_paid) * 100)
    merchant_order_id = f'design_{purchase.pk}_{uuid.uuid4().hex[:8]}'
    pkg_name = getattr(purchase.package, 'name_ar', None) or 'باقة تصميمات'

    try:
        # Step 1: Auth
        auth_res = http_requests.post(
            'https://accept.paymob.com/api/auth/tokens',
            json={'api_key': paymob_api_key}, timeout=15,
        )
        if auth_res.status_code not in (200, 201):
            logger.error("[PAYMOB/DESIGN-HELPER] Auth failed: %s — %s", auth_res.status_code, auth_res.text[:200])
            return None, "فشل المصادقة مع بوابة الدفع"
        auth_token = auth_res.json().get('token')
        if not auth_token:
            return None, "بوابة الدفع لم ترسل رمز المصادقة"

        # Step 2: Create order
        order_res = http_requests.post(
            'https://accept.paymob.com/api/ecommerce/orders',
            json={
                'auth_token': auth_token, 'delivery_needed': 'false',
                'amount_cents': amount_cents, 'currency': 'EGP',
                'items': [{'name': f'باقة {pkg_name}', 'amount_cents': amount_cents, 'quantity': '1'}],
                'merchant_order_id': merchant_order_id,
            },
            timeout=15,
        )
        if order_res.status_code not in (200, 201):
            logger.error("[PAYMOB/DESIGN-HELPER] Order failed: %s — %s", order_res.status_code, order_res.text[:200])
            return None, "فشل إنشاء طلب الدفع"
        order_id = order_res.json().get('id')
        if not order_id:
            return None, "بوابة الدفع لم ترسل رقم الطلب"

        # Step 3: Payment key
        billing = {
            'first_name': (customer.full_name or 'Customer').split()[0][:50] or 'Customer',
            'last_name': 'Design',
            'email': customer.email or 'customer@mousstec.com',
            'phone_number': customer.phone.lstrip('+') if customer.phone else '01000000000',
            'apartment': 'NA', 'floor': 'NA', 'street': 'NA', 'building': 'NA',
            'shipping_method': 'NA', 'postal_code': 'NA', 'city': 'Cairo',
            'country': 'EG', 'state': 'Cairo',
        }
        key_res = http_requests.post(
            'https://accept.paymob.com/api/acceptance/payment_keys',
            json={
                'auth_token': auth_token, 'amount_cents': amount_cents,
                'expiration': 3600, 'order_id': order_id,
                'billing_data': billing, 'currency': 'EGP',
                'integration_id': integration_id_int,
                'lock_order_when_paid': 'true',
            },
            timeout=15,
        )
        if key_res.status_code not in (200, 201):
            logger.error("[PAYMOB/DESIGN-HELPER] Payment key failed: %s — %s", key_res.status_code, key_res.text[:200])
            return None, "فشل إصدار رمز الدفع"
        payment_token = key_res.json().get('token')
        if not payment_token:
            return None, "بوابة الدفع لم ترسل رمز الدفع"

        # Save purchase reference in cache so the global paymob_callback can route it
        cache.set(f'paymob_design_{order_id}', {
            'purchase_id': purchase.pk,
            'customer_id': customer.pk,
        }, timeout=7200)

        iframe_url = f'https://accept.paymob.com/api/acceptance/iframes/{paymob_iframe_id}?payment_token={payment_token}'
        return iframe_url, None

    except http_requests.Timeout:
        logger.error("[PAYMOB/DESIGN-HELPER] Timeout")
        return None, "بوابة الدفع لا تستجيب"
    except http_requests.RequestException as exc:
        logger.error("[PAYMOB/DESIGN-HELPER] Network error: %s", exc)
        return None, "خطأ في الاتصال ببوابة الدفع"
    except Exception as exc:
        logger.exception("[PAYMOB/DESIGN-HELPER] Unexpected: %s", exc)
        return None, f"خطأ غير متوقع: {type(exc).__name__}"


# ───────────────────────────────────────────────────────────────────────────
# Endpoint implementations (preserved verbatim from _legacy.py)
# ───────────────────────────────────────────────────────────────────────────
@ensure_csrf_cookie
def design_store_home(request):
    """🛍️ صفحة المتجر — يعرض الباقات (عملاء + مصممين).

    🆕 باقات العملاء الآن مصدرها CUSTOMER_TOPUPS catalog مباشرةً (50/100/500)،
    مش الـ DB القديمة (cust_2/4/8 العتيقة). الفلسفة:
    الـ catalog هو single source of truth، والـ DB بتسجّل المشتريات فقط.
    """
    customer = _marketplace_auth(request)
    gate = _enforce_printing_sector(request, customer)
    if gate is not None:
        return gate

    customer_packages = _build_customer_topup_cards()
    designer_packages = DesignPackage.objects.filter(
        is_active=True, target_audience='designer',
    ).order_by('sort_order', 'designs_count')

    user_balance = 0
    free_remaining = 0
    if customer:
        user_balance = sum(p.designs_remaining for p in
                          customer.design_purchases.filter(status='paid')
                          if p.is_usable)
        free_remaining = customer.free_designs_remaining

    return render(request, 'clients/marketplace/design_store.html', {
        'packages': customer_packages,  # backwards compat
        'customer_packages': customer_packages,
        'designer_packages': designer_packages,
        'customer': customer,
        'user_balance': user_balance,
        'free_remaining': free_remaining,
        'total_balance': user_balance + free_remaining,
    })


@csrf_exempt
def design_store_buy(request, package_slug):
    """شراء باقة."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "يجب تسجيل الدخول أولاً", "redirect": "/marketplace/"}, status=401)
    gate = _enforce_printing_sector(request, customer, json_response=True)
    if gate is not None:
        return gate

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    package = get_object_or_404(DesignPackage, slug=package_slug, is_active=True)
    payment_method = request.POST.get('payment_method', 'paymob')

    # Determine designs count — designers (sector=printing with job_title containing design keywords) get more
    is_designer = (customer.sector == 'printing' and
                   any(kw in (customer.job_title or '').lower()
                       for kw in ('مصمم', 'design', 'جرافيك', 'graphic', 'فنان')))
    designs_count = (package.designer_designs_count or package.designs_count) if is_designer else package.designs_count

    # Create the purchase as PENDING — will be marked paid after payment confirmation
    purchase = DesignPurchase.objects.create(
        customer=customer, package=package,
        designs_total=designs_count,
        price_paid=package.price_egp,
        payment_method=payment_method,
        status='pending',
    )
    logger.info(f"[DESIGN STORE] Purchase #{purchase.pk} created — PENDING payment ({payment_method})")

    # Build response based on payment method
    if payment_method in ('vodafone_cash', 'instapay'):
        from clients.models import ManualPaymentReceipt
        receipt = ManualPaymentReceipt.objects.create(
            purchase_type='design',
            purchase_id=purchase.pk,
            amount=purchase.price_paid,
            payment_method=payment_method,
            customer=customer,
            contact_name=customer.full_name,
            contact_phone=customer.phone or '',
            sender_phone='', txn_reference='',
        )
        return JsonResponse({
            "status": "pending_payment",
            "purchase_id": purchase.pk,
            "purchase_code": str(purchase.purchase_code),
            "redirect": f"/payment/manual/upload/{receipt.receipt_code}/",
            "message": "جاري توجيهك لصفحة الدفع...",
        })
    else:
        # paymob / card — redirect to Paymob iframe
        paymob_api_key = getattr(settings, 'PAYMOB_API_KEY', '')
        paymob_integration_id = getattr(settings, 'PAYMOB_INTEGRATION_ID', '')
        paymob_iframe_id = getattr(settings, 'PAYMOB_IFRAME_ID', '')

        if not paymob_api_key:
            logger.error("[PAYMOB/DESIGN] API key not configured")
            return JsonResponse({"error": "الدفع بالبطاقة غير متاح حالياً"}, status=503)

        try:
            import requests as http_requests

            # 🛡️ تحقق من قيم الإعدادات (أرقام صحيحة)
            try:
                integration_id_int = int(paymob_integration_id)
            except (TypeError, ValueError):
                logger.error(f"[PAYMOB/DESIGN] PAYMOB_INTEGRATION_ID غير رقمي: {paymob_integration_id!r}")
                return JsonResponse({"error": "إعدادات بوابة الدفع غير صحيحة. تواصل مع الدعم."}, status=503)
            if not paymob_iframe_id:
                logger.error("[PAYMOB/DESIGN] PAYMOB_IFRAME_ID غير مضبوط")
                return JsonResponse({"error": "إعدادات بوابة الدفع غير مكتملة. تواصل مع الدعم."}, status=503)

            # Step 1: Auth
            auth_res = http_requests.post('https://accept.paymob.com/api/auth/tokens',
                json={'api_key': paymob_api_key}, timeout=15)
            if auth_res.status_code != 201 and auth_res.status_code != 200:
                logger.error(f"[PAYMOB/DESIGN] Auth failed: HTTP {auth_res.status_code} — {auth_res.text[:300]}")
                return JsonResponse({"error": "فشل المصادقة مع بوابة الدفع. حاول لاحقاً."}, status=502)
            auth_token = auth_res.json().get('token')
            if not auth_token:
                logger.error(f"[PAYMOB/DESIGN] Auth returned no token: {auth_res.text[:300]}")
                return JsonResponse({"error": "بوابة الدفع لم ترسل رمز المصادقة. حاول لاحقاً."}, status=502)

            # Step 2: Order
            amount_cents = int(float(package.price_egp) * 100)
            merchant_order_id = f'design_{purchase.pk}_{uuid.uuid4().hex[:8]}'
            order_res = http_requests.post('https://accept.paymob.com/api/ecommerce/orders', json={
                'auth_token': auth_token,
                'delivery_needed': 'false',
                'amount_cents': amount_cents,
                'currency': 'EGP',
                'items': [{'name': f'باقة {package.name_ar}', 'amount_cents': amount_cents, 'quantity': '1'}],
                'merchant_order_id': merchant_order_id,
            }, timeout=15)
            if order_res.status_code not in (200, 201):
                logger.error(f"[PAYMOB/DESIGN] Order failed: HTTP {order_res.status_code} — {order_res.text[:300]}")
                return JsonResponse({"error": "فشل إنشاء طلب الدفع. حاول لاحقاً."}, status=502)
            order_id = order_res.json().get('id')
            if not order_id:
                logger.error(f"[PAYMOB/DESIGN] Order returned no id: {order_res.text[:300]}")
                return JsonResponse({"error": "بوابة الدفع لم ترسل رقم الطلب. حاول لاحقاً."}, status=502)

            # Step 3: Payment key
            # 🌐 callback مرتبط بدومين الموقع (يفضل التحكم منا بدل dashboard Paymob)
            base_url = f"{'https' if request.is_secure() else 'http'}://{request.get_host()}"
            billing = {
                'first_name': (customer.full_name or 'Customer').split()[0][:50] or 'Customer',
                'last_name': 'Design',
                'email': customer.email or 'customer@mousstec.com',
                'phone_number': customer.phone.lstrip('+') if customer.phone else '01000000000',
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
                logger.error(f"[PAYMOB/DESIGN] Payment key failed: HTTP {key_res.status_code} — {key_res.text[:300]}")
                return JsonResponse({"error": "فشل إصدار رمز الدفع. حاول لاحقاً."}, status=502)
            payment_token = key_res.json().get('token')
            if not payment_token:
                logger.error(f"[PAYMOB/DESIGN] Payment key returned no token: {key_res.text[:300]}")
                return JsonResponse({"error": "بوابة الدفع لم ترسل رمز الدفع. حاول لاحقاً."}, status=502)

            # Store purchase info in cache for callback
            cache.set(f'paymob_design_{order_id}', {
                'purchase_id': purchase.pk,
                'customer_id': customer.pk,
            }, timeout=7200)

            iframe_url = f'https://accept.paymob.com/api/acceptance/iframes/{paymob_iframe_id}?payment_token={payment_token}'
            return JsonResponse({
                "status": "redirect_paymob",
                "redirect": iframe_url,
                "message": "جاري توجيهك لبوابة الدفع...",
            })
        except http_requests.Timeout:
            logger.error("[PAYMOB/DESIGN] Paymob timeout")
            return JsonResponse({"error": "بوابة الدفع لا تستجيب. حاول لاحقاً."}, status=504)
        except http_requests.RequestException as e:
            logger.error(f"[PAYMOB/DESIGN] Network error: {e}")
            return JsonResponse({"error": "خطأ في الاتصال ببوابة الدفع. تحقق من الإنترنت."}, status=502)
        except Exception as e:
            logger.exception(f"[PAYMOB/DESIGN] Unexpected error: {e}")
            return JsonResponse({"error": f"خطأ غير متوقع: {type(e).__name__}. تواصل مع الدعم."}, status=500)


@csrf_exempt
def design_store_payment(request, purchase_code):
    """💳 صفحة الدفع — تعليمات التحويل + رفع إيصال."""
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/')

    purchase = get_object_or_404(DesignPurchase, purchase_code=purchase_code, customer=customer)

    if request.method == 'POST':
        # Customer submitting payment proof
        txn_ref = request.POST.get('txn_ref', '').strip()
        sender_phone = request.POST.get('sender_phone', '').strip()
        if not txn_ref:
            return JsonResponse({"error": "لازم تكتب رقم العملية"}, status=400)
        purchase.payment_reference = txn_ref
        purchase.sender_phone = sender_phone
        purchase.status = 'awaiting_confirm'
        purchase.save(update_fields=['payment_reference', 'sender_phone', 'status'])
        logger.info(f"[DESIGN STORE] Purchase #{purchase.pk} payment proof submitted: ref={txn_ref}")
        return JsonResponse({
            "status": "success",
            "message": "تم استلام بيانات الدفع — هيتم التفعيل خلال دقائق بعد التأكيد ✅",
        })

    return render(request, 'clients/marketplace/design_store_payment.html', {
        'customer': customer,
        'purchase': purchase,
    })


def design_store_confirm_payment(request, purchase_id):
    """✅ تأكيد الدفع بواسطة الأدمن."""
    # Only super admin (public schema superuser) can confirm
    if not request.user.is_authenticated or not request.user.is_superuser:
        return JsonResponse({"error": "غير مصرح"}, status=403)

    purchase = get_object_or_404(DesignPurchase, pk=purchase_id)

    if request.method == 'POST':
        action = request.POST.get('action', 'confirm')
        if action == 'confirm':
            purchase.status = 'paid'
            purchase.paid_at = timezone.now()
            purchase.save(update_fields=['status', 'paid_at'])
            logger.info(f"[DESIGN STORE] Purchase #{purchase.pk} CONFIRMED by admin {request.user}")
            return JsonResponse({"status": "success", "message": f"تم تأكيد الدفع — الباقة مفعلة للعميل"})
        elif action == 'reject':
            purchase.status = 'rejected'
            purchase.save(update_fields=['status'])
            logger.info(f"[DESIGN STORE] Purchase #{purchase.pk} REJECTED by admin {request.user}")
            return JsonResponse({"status": "success", "message": "تم رفض الطلب"})

    return JsonResponse({"error": "POST only"}, status=405)


def design_store_my_print_orders(request):
    """🖨️ صفحة "طلباتي للطباعة" — العميل بيتابع حالة كل طلب طباعة بعتته
    للمطبعة (pending → quoted → in_production → shipped → delivered).

    قبل ده العميل كان بيـ submit طلب طباعة عبر "send-to-print" ومش بيشوف
    حالته بعد كده — ده sealing للـ feedback loop وميزة أساسية موجودة في
    أي platform طباعة عالمية (Printful, VistaPrint, Gelato).
    """
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/?next=/marketplace/design-store/print-orders/')
    gate = _enforce_printing_sector(request, customer)
    if gate is not None:
        return gate

    from clients.models import DesignPrintRequest
    orders = (
        DesignPrintRequest.objects
        .filter(customer=customer)
        .select_related('design')
        .order_by('-created_at')[:100]
    )

    # 📊 Status timeline order — للـ template يرسم progress bar
    status_order = ['pending', 'quoted', 'accepted', 'in_production', 'shipped', 'delivered']
    for o in orders:
        try:
            o.status_step = status_order.index(o.status) + 1 if o.status in status_order else 0
        except ValueError:
            o.status_step = 0
        o.status_total = len(status_order)
        o.is_cancelled = o.status == 'cancelled'
        o.is_active = o.status in {'pending', 'quoted', 'accepted', 'in_production', 'shipped'}

    # 📈 Quick counters للـ summary chips
    summary = {
        'total': len(orders),
        'active': sum(1 for o in orders if o.is_active),
        'delivered': sum(1 for o in orders if o.status == 'delivered'),
        'cancelled': sum(1 for o in orders if o.is_cancelled),
    }

    return render(request, 'clients/marketplace/design_store_print_orders.html', {
        'customer': customer,
        'orders': orders,
        'summary': summary,
    })


@ensure_csrf_cookie
def design_store_my_designs(request):
    """📚 صفحة تصاميمي + الرصيد المتبقي.

    🛡️ ensure_csrf_cookie: نضمن إن الـ mt_csrf cookie متضبط في الـ response
    حتى لو الـ template تعديل أو الـ {% csrf_token %} اتشال. ده الـ
    bulletproof source — الـ Universal flow POSTs (analyze/generate/refine)
    بتعتمد عليه."""
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/')
    gate = _enforce_printing_sector(request, customer)
    if gate is not None:
        return gate

    purchases = customer.design_purchases.filter(
        status__in=['paid', 'exhausted']
    ).select_related('package').order_by('-created_at')

    # 💬 Phase N.5 — annotate with `from_conversation` so the template can
    # render a "Generated via Chat" badge on cards that came from the
    # Conversational Design Builder.
    from clients.services.design_chat import annotate_designs_from_chat
    from clients.views.design_chat_views import find_resumable_conversation
    # Hide rows the storage-audit flagged as unrecoverable (provider URL
    # expired before we could persist locally). They still exist in the DB
    # for forensics; just shouldn't appear in the customer's gallery.
    designs = list(
        annotate_designs_from_chat(
            customer.designs
            .exclude(title__startswith='[BROKEN] ')
            .select_related('purchase__package')
            .order_by('-created_at')
        )[:50]
    )

    active_purchase = next((p for p in purchases if p.is_usable), None)
    paid_remaining = sum(p.designs_remaining for p in purchases if p.is_usable)
    free_remaining = customer.free_designs_remaining

    # إضافة معلومات إعادة التوليد لكل تصميم
    for d in designs:
        d.regen_left = max(d.regenerations_allowed - d.regenerations_used, 0)

    # 💬 Resume Conversation banner — find_resumable_conversation() handles
    # the feature-flag check internally (returns None when off) so we can
    # call it unconditionally.
    active_conversation = find_resumable_conversation(customer)

    return render(request, 'clients/marketplace/design_store_my.html', {
        'customer': customer,
        'purchases': purchases,
        'designs': designs,
        'active_purchase': active_purchase,
        'total_remaining': paid_remaining + free_remaining,
        'free_remaining': free_remaining,
        'paid_remaining': paid_remaining,
        'active_conversation': active_conversation,
    })


# ═══════════════════════════════════════════════════════════════════════════
# 🎨 Marketplace AI pipeline helpers (Phase N.6+ — C1/C2/C3 unification)
# ───────────────────────────────────────────────────────────────────────────
# Extracted to ``_ai_pipeline.py`` as part of the _legacy.py split. Re-imported
# here so callers within this module keep working unchanged (zero-downtime).
# When this module is finally retired, callers will import directly from
# ``clients.views._ai_pipeline``.
# ═══════════════════════════════════════════════════════════════════════════
from .._ai_pipeline import (
    _resolve_brand_context,
    _persist_remote_image,
    _composite_brand_logo,
    _run_marketplace_image_pipeline,
)
