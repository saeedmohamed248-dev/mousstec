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

from ._ai_pipeline import (
    _composite_brand_logo,
    _persist_remote_image,
    _resolve_brand_context,
    _run_marketplace_image_pipeline,
)
from ._shared import (
    _build_customer_topup_cards,
    _marketplace_auth,
)

logger = logging.getLogger('mouss_tec_core')


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
    if payment_method == 'vodafone_cash':
        return JsonResponse({
            "status": "pending_payment",
            "purchase_id": purchase.pk,
            "purchase_code": str(purchase.purchase_code),
            "redirect": f"/marketplace/design-store/payment/{purchase.purchase_code}/",
            "message": "جاري توجيهك لصفحة الدفع...",
        })
    elif payment_method == 'instapay':
        return JsonResponse({
            "status": "pending_payment",
            "purchase_id": purchase.pk,
            "purchase_code": str(purchase.purchase_code),
            "redirect": f"/marketplace/design-store/payment/{purchase.purchase_code}/",
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
    designs = list(
        annotate_designs_from_chat(customer.designs.order_by('-created_at'))[:50]
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
from ._ai_pipeline import (
    _resolve_brand_context,
    _persist_remote_image,
    _composite_brand_logo,
    _run_marketplace_image_pipeline,
)


@csrf_exempt
def design_store_generate(request):
    """🎨 توليد تصميم من الباقة."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "يجب تسجيل الدخول"}, status=401)
    gate = _enforce_printing_sector(request, customer, json_response=True)
    if gate is not None:
        return gate

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    # 🛡️ Rate limiting — 5 generations per minute per customer (protects Together AI / FLUX inference costs)
    gen_rate_key = f'design_gen_rate:{customer.pk}'
    gen_count = cache.get(gen_rate_key, 0)
    if gen_count >= 5:
        return JsonResponse({"error": "أنت ترسل طلبات كثيرة. انتظر دقيقة ثم حاول مرة أخرى."}, status=429)
    cache.set(gen_rate_key, gen_count + 1, 60)

    # Check free trial designs first, then paid packages
    using_free_trial = False
    purchase = None

    if customer.has_free_designs:
        using_free_trial = True
    else:
        # Find an active purchase with remaining designs
        purchase = next((p for p in customer.design_purchases.filter(status='paid').order_by('created_at')
                        if p.is_usable), None)
        if not purchase:
            return JsonResponse({
                "error": "رصيدك خلص! اشتري باقة جديدة علشان تكمل تصميمات.",
                "redirect": "/marketplace/design-store/",
                "free_used": customer.free_designs_used,
                "free_total": customer.free_designs_total,
            }, status=403)

    title = request.POST.get('title', '').strip()
    description = request.POST.get('description', '').strip()
    category = request.POST.get('category', 'other')
    size_preset = request.POST.get('size_preset', '1024x1024')
    custom_w = request.POST.get('custom_width_px', '').strip()
    custom_h = request.POST.get('custom_height_px', '').strip()
    weight = request.POST.get('weight_kg', '').strip()
    output_format = request.POST.get('output_format', 'png')
    # Print dimensions from the user (for accurate design)
    print_width_cm = request.POST.get('print_width_cm', '').strip()
    print_height_cm = request.POST.get('print_height_cm', '').strip()
    use_standard_size = request.POST.get('use_standard_size', '')

    if not description or len(description) < 10:
        return JsonResponse({"error": "وصف التصميم قصير جداً (10 أحرف على الأقل)"}, status=400)

    # Smart size selection based on category + user dimensions
    # Category → best default AI size mapping
    CATEGORY_SIZE_MAP = {
        'logo': '1024x1024', 'stamp': '1024x1024',
        'business_card': '1536x1024', 'letterhead': '1024x1536',
        'social_post': '1024x1024', 'story': '1024x1536', 'cover': '1536x1024',
        'flyer': '1024x1536', 'poster': '1024x1536', 'brochure': '1024x1536',
        'banner': '1536x1024', 'sign': '1536x1024',
        'certificate': '1536x1024', 'receipt_form': '1024x1536',
        'tshirt': '1024x1536', 'pants': '1024x1536', 'abaya': '1024x1536',
        'uniform': '1024x1536', 'cap': '1024x1024', 'bag': '1024x1024',
        'shoe': '1024x1024', 'full_body': '1024x1536',
        'mug': '1536x1024', 'mug_design': '1536x1024',
        'sticker': '1024x1024', 'label': '1024x1024',
        'packaging': '1024x1024', 'mockup': '1024x1024',
        'film_poster': '1024x1536', 'book_cover': '1024x1536',
        'album_cover': '1024x1024', 'thumbnail': '1536x1024',
        'pattern': '1024x1024', 'illustration': '1024x1024',
        'infographic': '1024x1536', 'car_wrap': '1536x1024',
        'menu': '1024x1536', 'invitation': '1024x1536',
    }

    # Map user presets → canonical size
    size_map = {
        '1024x1024': '1024x1024', '1024x1536': '1024x1536', '1536x1024': '1536x1024',
        '1024x1792': '1024x1792', '1792x1024': '1792x1024',
        '2048x2048': '1024x1024',
        # مطبوعات
        'a4': '1024x1536', 'a3': '1024x1536', 'a5': '1024x1536',
        'business_card': '1536x1024',
        # يافطات
        'banner_wide': '1536x1024', 'rollup': '1024x1536',
        'sign_square': '1024x1024', 'sign_landscape': '1536x1024',
        # ملابس
        'tshirt_chest': '1024x1536', 'tshirt_full': '1024x1536',
        'pants_pattern': '1024x1536', 'abaya_pattern': '1024x1536',
        'full_body': '1024x1536',
        'mug': '1536x1024', 'bag': '1024x1024',
        # أغلفة
        'book_cover': '1024x1536', 'youtube_thumb': '1536x1024',
        'film_poster': '1024x1536',
        'custom': '1024x1024', 'auto': 'auto',
    }
    # If user chose 'auto' or no size, pick best size by category
    if size_preset in ('auto', '') or size_preset not in size_map:
        canonical_size = CATEGORY_SIZE_MAP.get(category, '1024x1024')
    else:
        canonical_size = size_map.get(size_preset, '1024x1024')

    # If user specified custom print dimensions, determine orientation
    if print_width_cm and print_height_cm:
        try:
            pw, ph = float(print_width_cm), float(print_height_cm)
            if pw > ph:
                canonical_size = '1536x1024'  # landscape
            elif ph > pw:
                canonical_size = '1024x1536'  # portrait
            else:
                canonical_size = '1024x1024'  # square
        except (ValueError, TypeError):
            pass

    # 🧹 [tech-debt cleanup 2026-06-05]: GPT_IMAGE_SIZE_MAP was dead code from
    # the OpenAI era — defined but never read. Removed. FLUX/Ideogram accept
    # the canonical_size value as-is, no per-model remapping needed.

    # Build dimension info string for the prompt
    dim_info = ''
    if print_width_cm and print_height_cm:
        dim_info = f" Actual print size: {print_width_cm}cm x {print_height_cm}cm."
    elif use_standard_size:
        std_sizes = {
            'tshirt_s': '28x38cm', 'tshirt_m': '30x40cm', 'tshirt_l': '32x42cm',
            'business_card': '9x5.5cm', 'a4': '21x29.7cm', 'a3': '29.7x42cm',
            'a5': '14.8x21cm', 'mug_standard': '23x9cm', 'banner_60': '60x160cm',
            'banner_80': '80x180cm', 'instagram_post': '1080x1080px',
            'instagram_story': '1080x1920px', 'facebook_cover': '820x312px',
        }
        if use_standard_size in std_sizes:
            dim_info = f" Standard size: {std_sizes[use_standard_size]}."

    # Learn from best past prompts (few-shot learning)
    learned_suffix = ''
    try:
        from clients.models import DesignPromptLog
        best_examples = DesignPromptLog.get_best_examples(category, limit=2)
        if best_examples:
            learned_suffix = ' Style reference from top-rated designs in this category.'
    except Exception:
        pass

    # ═══════════════════════════════════════════════════════════════════
    # 🎨 MASTER PROMPT ENGINE v3 — Production-grade AI Design Prompts
    # ─────────────────────────────────────────────────────────────────
    # Architecture:
    #   1. QUALITY_FOUNDATION — universal quality/anti-artifact layer
    #   2. CATEGORY_PROMPT    — category-specific master prompt
    #   3. MULTI_ANGLE        — optional multi-view mockup modifier
    #   4. ARABIC_AWARENESS   — RTL text rendering instructions
    #   5. LEARNED_INSIGHTS   — few-shot learning from best past prompts
    # ═══════════════════════════════════════════════════════════════════

    multi_angle = request.POST.get('multi_angle', '') == '1'

    # ── Layer 1: Quality Foundation ──────────────────────────────────
    QUALITY_FOUNDATION = (
        "You are a world-class graphic designer with 20 years of experience in branding, "
        "print design, and visual communication. You create designs that win international "
        "awards. Every design you produce is: (1) perfectly composed with golden-ratio "
        "proportions and visual balance, (2) uses professional typography with proper "
        "kerning, leading, and hierarchy, (3) has a cohesive, intentional color palette "
        "limited to 3-5 harmonious colors, (4) is print-ready at 300 DPI quality with "
        "CMYK-safe colors, (5) has clean edges, no artifacts, no blurriness, no distortion. "
        "CRITICAL RULES: Never produce amateur-looking designs. Never use more than 2 font "
        "families. Never create cluttered layouts — use generous whitespace. Never distort "
        "text or make it unreadable. All text must be crisp and perfectly aligned. "
        "ULTRA-IMPORTANT: The design must look like a premium product from a top-tier design "
        "agency — NOT like a template or clip-art. Use professional lighting, shadows, and depth. "
        "Every element must have purpose and visual weight. "
    )

    # ── Layer 4: Arabic Awareness ────────────────────────────────────
    # Detect if user description contains Arabic
    import re
    has_arabic = bool(re.search(r'[؀-ۿݐ-ݿࢠ-ࣿ]', description))
    ARABIC_LAYER = ''
    if has_arabic:
        ARABIC_LAYER = (
            "ARABIC TEXT RULES: This design contains Arabic text. Arabic reads RIGHT-TO-LEFT. "
            "Use elegant Arabic typography (Naskh or modern Kufi style). Ensure Arabic letters "
            "are properly connected and shaped. Place Arabic text aligned to the RIGHT side. "
            "Use professional Arabic fonts — no broken or disconnected letters. "
        )

    # ── Layer 2: Category-Specific Master Prompts ────────────────────
    enhanced_desc = description
    weight_info = f" Product weight: {weight}kg." if weight else ''

    CATEGORY_PROMPTS = {
        'logo': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Create a world-class LOGO DESIGN. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Design a timeless, iconic logo that communicates the brand's essence "
            f"at a glance. Create a unique symbol/icon paired with custom lettering. The logo "
            f"must be: scalable (works at 16px favicon AND billboard size), memorable (recognizable "
            f"in 2 seconds), versatile (works on white, dark, and colored backgrounds). "
            f"COMPOSITION: Center the logo on a pure white background. Use negative space cleverly. "
            f"Maximum 2-3 brand colors. The icon and text should be perfectly balanced. "
            f"STYLE: Modern, clean, vector-style flat design. No photographic elements, no complex "
            f"gradients, no drop shadows, no 3D effects unless specifically requested. Think Apple, "
            f"Nike, Airbnb level quality. "
            f"OUTPUT: Render the final logo large and centered with ample padding around it.{dim_info}"
        ),

        'business_card': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PREMIUM BUSINESS CARD — render as a flat, top-down photograph of the "
            f"printed card lying on a dark marble or wooden surface. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create an elegant, minimal business card with perfect typography. "
            f"LAYOUT: Include these fields in clear visual hierarchy: Company name/logo (top), "
            f"Person name (prominent), Job title, Phone number, Email, Address (smaller). "
            f"TYPOGRAPHY: Use maximum 2 fonts — one bold display font for the name, one clean "
            f"sans-serif for details. Letter-spacing: slightly expanded for elegance. "
            f"DESIGN: Use one accent color against white/cream card stock. Add a subtle design "
            f"element (thin line, geometric pattern, or embossed texture). Consider a colored edge "
            f"or a minimal pattern on the back. "
            f"QUALITY: The card should look like a $500 Moo.com premium design — thick paper stock "
            f"feel, possibly with foil stamping or letterpress texture.{dim_info or ' Size: 85x55mm (standard).'}"
        ),

        'flyer': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a HIGH-IMPACT FLYER — flat print layout, ready for professional printing. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a flyer that grabs attention in under 1 second. "
            f"LAYOUT STRUCTURE (top to bottom): "
            f"(1) HERO ZONE (top 40%): Dominant visual element + bold headline in large, impactful font. "
            f"(2) BODY ZONE (middle 35%): Key information in organized blocks with icons or bullet points. "
            f"Use subheadings to break content. Keep body text readable (14pt+ equivalent). "
            f"(3) ACTION ZONE (bottom 25%): Strong call-to-action button/banner, contact details "
            f"(phone, address, social media), and company logo. "
            f"COLOR: Use a bold primary color for headlines and CTA, with a complementary secondary color. "
            f"Background should be clean (white or very light tint). "
            f"TYPOGRAPHY: Bold sans-serif for headlines (Impact, Montserrat style), clean body font. "
            f"VISUAL HIERARCHY: Someone should understand the message from 3 feet away just by reading "
            f"the headline and seeing the main visual.{dim_info or ' A4 (210x297mm).'}"
        ),

        'poster': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a DRAMATIC, ATTENTION-GRABBING POSTER — flat print layout. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a poster that commands attention on a wall from across the room. "
            f"COMPOSITION: Use the rule of thirds. Create one dominant focal point that takes up "
            f"at least 50% of the poster. Use dramatic contrast (light vs dark, big vs small). "
            f"TYPOGRAPHY: The headline should be MASSIVE — readable from 10+ feet away. Use extreme "
            f"font weight (ultra-bold/black). Limit to 5-7 words maximum for headline. "
            f"Supporting text should be much smaller, creating dramatic size contrast. "
            f"COLOR: Use high-contrast color scheme — dark background with bright accent, or vice versa. "
            f"IMAGERY: If the content requires imagery, use a single powerful, high-quality visual — "
            f"not multiple small images. "
            f"OVERALL: Think movie poster or museum exhibition quality — bold, artistic, unforgettable.{dim_info}"
        ),

        'social_post': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a VIRAL-WORTHY SOCIAL MEDIA POST that stops the scroll. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a post optimized for maximum engagement and shares. "
            f"COMPOSITION: Bold, simple, high-contrast. The message should be understood in under "
            f"2 seconds of viewing. Use the full frame — no wasted space. "
            f"TYPOGRAPHY: Large, bold text that is readable on a phone screen. Maximum 2 lines of "
            f"text for headline. Use modern trendy fonts (geometric sans-serif). "
            f"COLOR: Vibrant, saturated colors that pop on mobile screens. Use color blocking, "
            f"gradients, or duotone effects for modern appeal. "
            f"STYLE: Follow 2024-2026 design trends — glassmorphism, bold gradients, "
            f"oversized typography, minimalist compositions, neon accents. "
            f"BRANDING: Include a subtle brand logo/watermark in one corner (small, not distracting).{dim_info or ' Square 1080x1080px.'}"
        ),

        'menu': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design an ELEGANT RESTAURANT/CAFE MENU — flat print layout. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a menu that elevates the dining experience. "
            f"LAYOUT: Organized by food categories with clear section headers (appetizers, mains, "
            f"desserts, drinks). Each item has: name (bold), description (italic/light), price (right-aligned). "
            f"Use thin divider lines or subtle spacing between sections. "
            f"TYPOGRAPHY: Pair an elegant serif font (for headers/restaurant name) with a clean "
            f"sans-serif (for items/prices). Ensure prices are perfectly aligned in a right column. "
            f"DESIGN: Add subtle decorative elements — thin borders, small culinary icons, "
            f"ornamental dividers. The background should be cream/off-white or rich dark (leather feel). "
            f"QUALITY: Should look like it belongs in a Michelin-starred restaurant — refined, "
            f"luxurious, but easy to read.{dim_info or ' A4 (210x297mm).'}"
        ),

        'invitation': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a STUNNING INVITATION CARD — flat print layout. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create an invitation that makes recipients excited to attend. "
            f"LAYOUT: Center the event name prominently. Below it: date, time, venue, dress code, "
            f"RSVP details — in clear, elegant hierarchy. "
            f"DESIGN: Use luxurious elements — gold/silver foil effects, embossed textures, "
            f"decorative frames or borders, floral or geometric ornaments. "
            f"TYPOGRAPHY: Elegant script font for event name, clean serif or sans-serif for details. "
            f"Perfect letter spacing and line height. "
            f"COLOR PALETTE: Rich, celebratory colors — deep navy + gold, burgundy + cream, "
            f"blush pink + rose gold — depending on the event type. "
            f"PAPER: Simulate premium card stock — thick, textured, high-end stationery feel.{dim_info}"
        ),

        'banner': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PROFESSIONAL ROLL-UP BANNER — flat, vertical print layout. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a banner optimized for standing display at events/stores. "
            f"LAYOUT (vertical flow, top to bottom): "
            f"(1) TOP: Company logo + brand colors header bar. "
            f"(2) HERO: Main message in VERY LARGE bold text (readable from 3+ meters). "
            f"(3) MIDDLE: 3-4 key features/benefits with icons, clean grid layout. "
            f"(4) BOTTOM: Contact info strip — phone, email, website, QR code area. "
            f"TYPOGRAPHY: Extremely bold headlines (ultra-thick weight). Body text must be "
            f"minimum 24pt equivalent for readability at distance. "
            f"COLOR: Strong brand colors with high contrast. Full-bleed background color.{dim_info}"
        ),

        'sticker': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PROFESSIONAL STICKER/LABEL — die-cut ready. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a sticker that looks amazing at small size (3-10cm). "
            f"DESIGN: Bold, simple shapes with thick outlines. Maximum 3 colors. No fine "
            f"details that disappear when printed small. Vector-style flat illustration. "
            f"SHAPE: Design with a clear die-cut boundary — circle, rounded rectangle, or custom "
            f"contour shape. Show the sticker on white background with a subtle cut-line. "
            f"QUALITY: Should look like a premium vinyl sticker — vibrant, weatherproof, professional.{dim_info}"
        ),

        # ── هوية تجارية ──
        'letterhead': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PROFESSIONAL LETTERHEAD / corporate stationery. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Clean, corporate letterhead with header, footer, and side accents. "
            f"Include space for company logo, address, phone, email. Professional typography. "
            f"STYLE: Modern, minimal, print-ready A4.{dim_info}"
        ),
        'stamp': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a CORPORATE RUBBER STAMP. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a professional stamp design — circular or rectangular. "
            f"Include company name, registration number space, and decorative border. "
            f"STYLE: Traditional stamp aesthetic with clean text, monochrome.{dim_info}"
        ),
        'story': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a vertical SOCIAL MEDIA STORY (9:16 ratio). "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Full-screen mobile-first design. Bold visuals, engaging typography, "
            f"swipe-up ready. Vibrant colors, modern layout.{dim_info}"
        ),
        'cover': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a SOCIAL MEDIA COVER / banner image. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Wide-format cover for Facebook/YouTube/LinkedIn. Professional, branded, "
            f"with clear visual hierarchy. Hero image with text overlay.{dim_info}"
        ),
        'sign': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PROFESSIONAL SIGNAGE / cladding / outdoor sign. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Large-format sign design readable from distance. Bold brand colors, "
            f"clear company name, high contrast. Include mock lighting effects. "
            f"STYLE: Modern storefront/building signage — LED backlit or cladding finish.{dim_info}"
        ),
        'certificate': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a FORMAL CERTIFICATE / award document. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Elegant certificate with decorative borders, gold/silver accents, "
            f"formal typography. Include fields for name, date, signature line. "
            f"STYLE: Premium, official, printable.{dim_info}"
        ),
        'brochure': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PROFESSIONAL BROCHURE panel. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Informative layout with sections, images, and call-to-action. "
            f"STYLE: Corporate, clean, print-ready with bleed marks.{dim_info}"
        ),
        'receipt_form': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PROFESSIONAL BUSINESS FORM / receipt / invoice template. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Clean form with organized fields, lines for handwriting, "
            f"company header, numbered rows, total section. Print-ready. "
            f"STYLE: Professional document design with clear hierarchy.{dim_info}"
        ),

        # ── ملابس ──
        'pants': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design FASHION PANTS / trousers. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a detailed fashion illustration showing pants design from front and back. "
            f"Show fabric pattern, stitching details, pockets, waistband. "
            f"STYLE: Professional fashion sketch / product mockup. Photorealistic rendering.{dim_info}"
        ),
        'abaya': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design an ELEGANT ABAYA / JALABIYA / modest fashion garment. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a beautiful modest fashion design showing the full garment. "
            f"Include embroidery patterns, fabric draping, sleeve details. "
            f"STYLE: High-end fashion illustration. Show flowing fabric, intricate detailing.{dim_info}"
        ),
        'uniform': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PROFESSIONAL UNIFORM / workwear. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Corporate uniform mockup with branding, name tags, "
            f"functional pockets. Show front view. "
            f"STYLE: Clean product mockup, realistic fabric rendering.{dim_info}"
        ),
        'cap': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a BRANDED CAP / HAT. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Cap product mockup with embroidered/printed logo. "
            f"Show front and side angle. Professional product photography style.{dim_info}"
        ),
        'bag': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a BRANDED BAG / TOTE. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Product mockup of a branded bag with print/embroidery. "
            f"Professional product photography, studio lighting.{dim_info}"
        ),
        'shoe': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design BRANDED FOOTWEAR / SHOES. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Detailed shoe/sneaker design mockup. Show branding, "
            f"materials, sole design. Professional product rendering.{dim_info}"
        ),

        # ── تغليف ──
        'label': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PRODUCT LABEL / sticker label. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Print-ready product label with brand, ingredients area, "
            f"barcode space. Professional typography and layout.{dim_info}"
        ),
        'mug_design': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a MUG print — wraparound graphic. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a mug mockup with the design wrapped around it. "
            f"Vibrant colors, high contrast, photorealistic ceramic rendering.{dim_info}"
        ),

        # ── وسائط ──
        'film_poster': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a CINEMATIC MOVIE/SERIES POSTER. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Dramatic composition with key characters/imagery. "
            f"Professional typography, cinematic lighting and color grading. "
            f"STYLE: Hollywood-quality movie poster. Dramatic, compelling.{dim_info}"
        ),
        'book_cover': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PROFESSIONAL BOOK COVER. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Genre-appropriate cover with compelling imagery and typography. "
            f"STYLE: Bestseller-quality cover design. Clear title placement.{dim_info}"
        ),
        'album_cover': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design an ALBUM COVER art. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Creative, artistic album artwork. Square format. "
            f"STYLE: Visually striking, musical, genre-appropriate.{dim_info}"
        ),
        'thumbnail': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a YOUTUBE THUMBNAIL (16:9). "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Ultra bold text, expressive face/image, high contrast colors. "
            f"Designed to stand out in a feed. Click-worthy, attention-grabbing.{dim_info}"
        ),

        # ── أخرى ──
        'pattern': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Create a SEAMLESS REPEATING PATTERN. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Tileable pattern for fabric/textile/wallpaper. "
            f"Ensure edges match perfectly for seamless tiling.{dim_info}"
        ),
        'illustration': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Create a PROFESSIONAL ILLUSTRATION. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Clean lines, vivid colors, detailed artwork. "
            f"STYLE: Modern digital illustration, professional quality.{dim_info}"
        ),
        'infographic': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PROFESSIONAL INFOGRAPHIC. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Data visualization with charts, icons, clean layout. "
            f"STYLE: Modern, informative, visually organized.{dim_info}"
        ),
        'car_wrap': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a VEHICLE WRAP / fleet graphics. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Full vehicle body wrap design with brand identity. "
            f"Show the design on a realistic car/van/truck mockup. "
            f"STYLE: Professional fleet branding, eye-catching on the road.{dim_info}"
        ),
    }

    # ── T-shirt (with multi-angle support) ───────────────────────────
    if category == 'tshirt':
        base_tshirt = (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"CLIENT BRIEF: {description}. "
        )
        if multi_angle:
            enhanced_desc = (
                f"{base_tshirt}"
                f"TASK: Create a PROFESSIONAL T-SHIRT PRODUCT PHOTOGRAPHY showing the shirt from "
                f"THREE ANGLES arranged side by side in ONE image: (1) FRONT VIEW — full front of shirt, "
                f"(2) BACK VIEW — full back of shirt, (3) 3/4 ANGLE VIEW — perspective view showing depth. "
                f"EXECUTION: Photo-realistic product mockup of a premium cotton t-shirt. The graphic design "
                f"should be professionally printed on the shirt (DTG/screen-print quality). "
                f"Show realistic fabric texture, natural folds and wrinkles, proper perspective distortion "
                f"of the artwork following the shirt's contours. "
                f"BACKGROUND: Clean, consistent studio background (light gray gradient). "
                f"LIGHTING: Professional studio lighting — soft key light, fill light, subtle rim light. "
                f"All three views should have identical lighting for consistency. "
                f"QUALITY: E-commerce product photography level — Shopify/Amazon listing quality.{dim_info or ' 30x40cm chest print.'}"
            )
        else:
            enhanced_desc = (
                f"{base_tshirt}"
                f"TASK: Create a STUNNING T-SHIRT PRODUCT MOCKUP — a single, hero product shot. "
                f"EXECUTION: Photo-realistic mockup showing the t-shirt worn by an invisible mannequin "
                f"(ghost mannequin style) or laid flat on a clean surface. The graphic artwork should be "
                f"screen-printed/DTG quality — vibrant, sharp, following the fabric contours naturally. "
                f"Show realistic cotton fabric texture, natural shadow beneath, studio lighting. "
                f"BACKGROUND: Clean studio white or light gray. "
                f"QUALITY: Premium e-commerce product photography — Shopify hero image quality.{dim_info or ' 30x40cm chest print.'}"
            )

    # ── Mug (with multi-angle support) ───────────────────────────────
    elif category == 'mug':
        base_mug = (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"CLIENT BRIEF: {description}. "
        )
        if multi_angle:
            enhanced_desc = (
                f"{base_mug}"
                f"TASK: Create a PHOTO-REALISTIC MUG MOCKUP showing THREE ANGLES in ONE image: "
                f"(1) FRONT — design facing camera, (2) BACK — opposite side, (3) HANDLE SIDE — 3/4 angle. "
                f"EXECUTION: Premium white ceramic 11oz coffee mug. The artwork wraps naturally around the "
                f"curved surface with proper perspective distortion. Show realistic ceramic texture, "
                f"glossy reflections, and a subtle shadow beneath each mug. "
                f"BACKGROUND: Clean white studio background, consistent lighting across all three views.{dim_info or ' Wrap area: 23x9cm.'}"
            )
        else:
            enhanced_desc = (
                f"{base_mug}"
                f"TASK: Create a PREMIUM MUG PRODUCT MOCKUP — single hero shot from a 3/4 angle. "
                f"EXECUTION: Photo-realistic white ceramic 11oz mug with the design wrapped around it. "
                f"Show the handle, realistic ceramic gloss, subtle reflections, and a soft shadow. "
                f"The artwork conforms to the mug's curvature naturally. "
                f"BACKGROUND: Lifestyle setting (wooden desk, coffee beans) OR clean studio white. "
                f"QUALITY: Premium product photography — Amazon/Etsy listing quality.{dim_info or ' Wrap area: 23x9cm.'}"
            )

    # ── Packaging (with multi-angle support) ─────────────────────────
    elif category == 'packaging':
        base_pkg = (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"CLIENT BRIEF: {description}. "
        )
        if multi_angle:
            enhanced_desc = (
                f"{base_pkg}"
                f"TASK: Create a PRODUCT PACKAGING MOCKUP showing THREE ANGLES in ONE image: "
                f"(1) FRONT — main branding panel, (2) BACK — info panel with ingredients/details, "
                f"(3) SIDE — secondary branding. Arrange side by side on a clean white background. "
                f"EXECUTION: Realistic 3D box/bag/bottle mockup with professional label design. "
                f"Include typography, barcode area, brand elements. Studio product photography quality.{dim_info}{weight_info}"
            )
        else:
            enhanced_desc = (
                f"{base_pkg}"
                f"TASK: Design PREMIUM PRODUCT PACKAGING — 3D realistic mockup from an attractive angle. "
                f"EXECUTION: Create packaging that has shelf-appeal — a customer would pick this product "
                f"over competitors. Design a complete package with: front branding panel (logo, product name, "
                f"key visual), info panel (ingredients/specs area), barcode area, regulatory symbols. "
                f"STYLE: Modern, clean packaging with professional typography and intentional color use. "
                f"RENDERING: Photo-realistic 3D mockup with studio lighting, subtle reflections, "
                f"and realistic material textures (matte, glossy, kraft paper, etc.).{dim_info}{weight_info}"
            )

    # ── All other categories from lookup ─────────────────────────────
    elif category in CATEGORY_PROMPTS:
        enhanced_desc = CATEGORY_PROMPTS[category]

    # ── Fallback for unknown categories ──────────────────────────────
    else:
        # Detect if this is a form/document request
        form_keywords = ('استقبال', 'فورم', 'نموذج', 'form', 'استمارة', 'ورقة', 'receipt',
                         'فاتورة', 'invoice', 'كشف', 'تقرير', 'سجل', 'بيان', 'شيت',
                         'checklist', 'صيانة', 'maintenance', 'inspection', 'فحص')
        is_form_request = any(kw in description.lower() for kw in form_keywords)

        if is_form_request:
            enhanced_desc = (
                f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
                f"TASK: Create a PROFESSIONAL BUSINESS FORM / DOCUMENT designed for PRINTING at a real business. "
                f"CLIENT BRIEF: {description}. "
                f"CRITICAL DESIGN RULES FOR THIS FORM: "
                f"1. This is a PRINTABLE PAPER FORM — it must look like a real form used in professional businesses. "
                f"2. Use a CLEAN WHITE BACKGROUND with a professional header area at the top for company logo and name. "
                f"3. Create CLEARLY LABELED FIELDS with horizontal lines for handwriting (like ________). "
                f"4. Use proper GRID LAYOUT with organized sections, each with a clear title/header. "
                f"5. All Arabic text must be RIGHT-ALIGNED, perfectly readable, with proper connected Arabic letters. "
                f"6. Use professional CORPORATE COLORS — navy blue (#1e3a5f) for headers, black for labels, "
                f"light gray (#f5f5f5) for alternating row backgrounds. "
                f"7. Include a footer area with date, signature line, and company info. "
                f"8. The form should look like it was designed by a professional print house — NOT a generic template. "
                f"9. If a logo is provided, place it prominently in the top-right corner (for RTL layout). "
                f"10. Use thin borders and lines, NOT thick boxes. Keep it elegant and clean. "
                f"STYLE REFERENCE: Think of premium auto dealership or professional service center paperwork — "
                f"clean, organized, branded, and easy to fill out by hand.{dim_info}"
            )
        else:
            enhanced_desc = (
                f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
                f"TASK: Create a PROFESSIONAL, PRINT-READY GRAPHIC DESIGN. "
                f"CLIENT BRIEF: {description}. "
                f"EXECUTION: Analyze the client's request and determine the best design approach. "
                f"Create visually stunning artwork with professional composition, color theory, "
                f"and visual hierarchy. Use a cohesive color palette, clean typography, and balanced layout. "
                f"QUALITY: The output should look like it was produced by a top design agency — "
                f"polished, pixel-perfect, ready for professional printing or digital use.{dim_info}"
            )

    # ── Layer 5: Append learned insights ─────────────────────────────
    if learned_suffix:
        enhanced_desc += learned_suffix

    # 🎨 Phase N.6+: marketplace package flow now uses the unified pipeline —
    # Smart Router (FLUX/Ideogram) + brand profile injection + PIL logo
    # composite + quality gate — same as Universal AI and tenant AI Studio.
    # The legacy enhanced_desc (Master Prompt Engine v3) is the engineered
    # prompt; we pass already_engineered=True to compose_mega_prompt so
    # brand_context is still applied but the cinematic LLM rewrite is
    # SKIPPED (M1 fast path) — no double LLM call, ~$0.002 saved per design.
    logo_file = request.FILES.get('logo') if (
        not using_free_trial and purchase and purchase.package.allows_logo_upload
    ) else None
    logo_was_uploaded = bool(logo_file)

    # ── Brand context — surfaces the customer's saved CustomerBrandProfile ──
    brand_context = None
    brand_logo_source = None
    try:
        bp = getattr(customer, 'brand_profile', None)
        if bp and bp.is_active:
            brand_context = bp.as_brand_context()
            if bp.auto_inject_logo and bp.has_logo:
                brand_context['logo_described'] = True
                brand_logo_source = bp.logo_image
    except Exception as e:
        logger.warning(f'[DESIGN STORE] brand profile lookup failed: {e}')

    # If a per-request logo is uploaded but no brand profile, build a minimal
    # brand_context so the LLM still leaves a reserved area for the composite.
    if logo_was_uploaded:
        if brand_context is None:
            brand_context = {
                'brand_name': customer.full_name[:60] or 'Brand',
            }
        brand_context['logo_described'] = True

    # ── Stage A: apply brand to engineered prompt (fast path — no LLM call) ──
    try:
        from erp_core.ai.design_engine import compose_mega_prompt
        mega = compose_mega_prompt(
            raw_idea=enhanced_desc,                  # legacy engineered prompt
            domain=category if category != 'other' else '',
            selections={},
            brand_context=brand_context,
            presentation_category=category if category != 'other' else None,
            already_engineered=True,                 # M1 — skip double rewrite
        )
    except Exception as e:
        logger.exception('[DESIGN STORE] compose_mega_prompt crashed')
        return JsonResponse({
            'error': 'تعذرت صياغة البرومبت. حاول تعدّل الوصف.',
        }, status=502)

    if not mega.get('success'):
        return JsonResponse({
            'error': 'مقدرناش نصيغ البرومبت — جرب توضّح الوصف.',
            'engine_error': mega.get('error', ''),
        }, status=502)

    final_prompt = mega['mega_prompt']
    final_negative = mega.get('negative_prompt', '')
    presentation_category = mega.get('presentation_category') or category
    text_overlay = mega.get('text_overlay')
    has_text = bool(text_overlay and text_overlay.get('text'))
    enhanced_desc = final_prompt  # update for downstream storage (DesignPromptLog etc.)

    try:
        # ── Stage B: generate via Smart Router (FLUX for photo, Ideogram for text) ──
        from erp_core.ai.printing_copilot import generate_design_image
        img = generate_design_image(
            prompt=final_prompt[:1800],
            size=canonical_size,
            negative_prompt=final_negative or (
                'low quality, blurry, watermark, distorted text, fake logo, '
                'duplicated elements, extra fingers, jpeg artifacts'
            ),
            category=presentation_category,
            has_text_content=has_text,
            block_schnell_fallback=True,  # marketplace: dev tier only
        )

        if not img.get('success'):
            err = img.get('error', 'unknown')
            logger.error(
                f'[DESIGN STORE] image gen failed: {err} — '
                f'{(img.get("detail") or "")[:200]}'
            )
            return JsonResponse({
                'error': 'فشل توليد التصميم. حاول تاني.',
                'engine_error': err,
            }, status=502)

        used_engine = img.get('engine', 'flux')
        used_model = img.get('model') or used_engine
        image_url = img.get('url')
        b64 = img.get('b64_json')

        # ── Stage B.1: persist locally (Together URLs expire after ~1h) ──
        import uuid as _uuid
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        if b64:
            import base64 as _b64
            img_bytes = _b64.b64decode(b64)
            filename = f'ai_store/{customer.uid}/{_uuid.uuid4().hex}.png'
            saved_path = default_storage.save(filename, ContentFile(img_bytes))
            image_url = default_storage.url(saved_path)
            if image_url.startswith('/'):
                image_url = request.build_absolute_uri(image_url)
        elif image_url:
            try:
                import requests as _req
                r = _req.get(image_url, timeout=30)
                if r.status_code == 200:
                    filename = f'ai_store/{customer.uid}/{_uuid.uuid4().hex}.png'
                    saved_path = default_storage.save(filename, ContentFile(r.content))
                    local = default_storage.url(saved_path)
                    if local.startswith('/'):
                        local = request.build_absolute_uri(local)
                    image_url = local
            except Exception as e:
                logger.warning(f'[DESIGN STORE] Failed to persist image url: {e}')
        else:
            return JsonResponse({'error': 'محرك التوليد لم يُرجع صورة.'}, status=502)

        # ── Stage C: composite brand/uploaded logo (FLUX only; Ideogram draws it) ──
        logo_source = logo_file if logo_file else brand_logo_source
        logo_composited = False
        if logo_source and used_engine != 'ideogram':
            try:
                from erp_core.ai.logo_overlay import composite_logo_on_image_url
                comp = composite_logo_on_image_url(
                    image_url=image_url,
                    logo_source=logo_source,
                    category=presentation_category or '',
                    text_overlay_position=(
                        (text_overlay or {}).get('position') if has_text else None
                    ),
                )
                if comp.get('success'):
                    new_url = comp['url']
                    if new_url and new_url.startswith('/'):
                        new_url = request.build_absolute_uri(new_url)
                    image_url = new_url
                    logo_composited = True
                elif not comp.get('skipped'):
                    logger.warning(
                        f'[DESIGN STORE] logo composite failed (non-fatal): '
                        f'{comp.get("error")}'
                    )
            except Exception as e:
                logger.warning(f'[DESIGN STORE] logo composite exception: {e}')

        # ── Stage D: optional vision-based quality gate ──
        quality_score = None
        if bool(getattr(settings, 'DESIGN_QUALITY_GATE_ENABLED', True)):
            try:
                from erp_core.ai.design_engine import verify_design_quality
                qr = verify_design_quality(
                    image_url=image_url,
                    raw_idea=description,
                    category=presentation_category,
                )
                if qr.get('success'):
                    quality_score = qr.get('score')
            except Exception as e:
                logger.warning(f'[DESIGN STORE] quality gate failed: {e}')

        # Create design record
        # Free trial: 0 regenerations. Paid: from package settings.
        if using_free_trial:
            regen_limit = 0
        elif purchase:
            regen_limit = purchase.package.free_regenerations_per_design
        else:
            regen_limit = 0

        design = CustomerDesign.objects.create(
            customer=customer,
            purchase=purchase,
            is_free_trial=using_free_trial,
            title=title or description[:60], description=description,
            category=category, size_preset=size_preset,
            custom_width_px=int(custom_w) if custom_w.isdigit() else None,
            custom_height_px=int(custom_h) if custom_h.isdigit() else None,
            weight_kg=Decimal(weight) if weight else None,
            output_format=output_format,
            raw_input=description, engineered_prompt=enhanced_desc[:4000],
            image_url=image_url, model_used=used_model or 'unknown',
            regenerations_allowed=regen_limit,
        )

        # Save logo to design record
        if logo_file:
            logo_file.seek(0)
            design.logo_image = logo_file
            design.save(update_fields=['logo_image'])

        # Log prompt for AI learning
        try:
            from clients.models import DesignPromptLog
            DesignPromptLog.objects.create(
                category=category,
                user_prompt=description[:500],
                engineered_prompt=enhanced_desc[:4000],
                model_used=used_model or '',
                size_used=canonical_size,
                customer_rating=None,
                design=design,
            )
        except Exception:
            pass  # Non-critical

        # Save chat history — initial generation
        try:
            from clients.models import DesignChatMessage
            DesignChatMessage.objects.create(
                design=design, role='user', content=description, image_url=''
            )
            DesignChatMessage.objects.create(
                design=design, role='assistant',
                content=f"تم توليد تصميم: {title or description[:60]}",
                image_url=image_url or ''
            )
        except Exception:
            pass

        # Consume 1 design from the right source
        if using_free_trial:
            customer.consume_free_design()
            remaining = customer.free_designs_remaining
        else:
            purchase.consume_design()
            purchase.refresh_from_db()
            remaining = purchase.designs_remaining

        # Build download URLs for different formats
        download_urls = {
            'png': image_url,
            'pdf': f"/marketplace/design-store/{design.design_code}/download/pdf/",
            'jpg': f"/marketplace/design-store/{design.design_code}/download/jpg/",
        }

        response_payload = {
            "status": "success",
            "design_id": design.pk,
            "design_code": str(design.design_code),
            "image_url": image_url,
            "model_used": used_model,
            "size": design.actual_size_label,
            "remaining_in_package": remaining,
            "regenerations_left": design.regenerations_allowed,
            "is_free_trial": using_free_trial,
            "can_download": not using_free_trial,
            "can_send_whatsapp": not using_free_trial,
            "download_urls": download_urls if not using_free_trial else {},
            # 🆕 Additive fields (Phase N.6+ pipeline migration)
            "engine_used": used_engine,
            "presentation_category": presentation_category,
            "brand_applied": (mega.get('brand_applied') or {}).get('applied', False),
            "logo_composited": logo_composited,
            "quality_score": quality_score,
        }
        if logo_composited:
            response_payload["logo_notice"] = "✅ تم دمج اللوجو في التصميم تلقائياً."
        elif logo_was_uploaded:
            response_payload["logo_notice"] = (
                "اللوجو اتحفظ بس مقدرناش ندمجه (صيغة غير مدعومة أو الـ category لا يقبل composite). "
                "التصميم سايب مكان مناسب ليه — تقدر تدمجه يدوياً."
            )
        return JsonResponse(response_payload)

    except Exception as e:
        logger.error(f"[DESIGN STORE] Generate failed: {e}")
        return JsonResponse({"error": "حدث خطأ أثناء إنشاء التصميم. حاول مرة أخرى."}, status=500)


@csrf_exempt
def design_store_send_whatsapp(request, design_code):
    """📱 إرسال التصميم للعميل على واتساب."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "غير مصرح"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)
    target_phone = request.POST.get('phone', customer.phone).strip()
    custom_message = request.POST.get('message', '').strip()

    if not target_phone:
        return JsonResponse({"error": "رقم الواتساب مطلوب"}, status=400)

    # Build wa.me deep link
    from urllib.parse import quote
    msg = custom_message or f"تصميمك من Mouss Tec AI Store جاهز!\n\nالعنوان: {design.title}\nالمقاس: {design.actual_size_label}\n\n{design.image_url}"
    phone_clean = target_phone.lstrip('+').lstrip('0')
    if not phone_clean.startswith('20'):
        phone_clean = '20' + phone_clean
    wa_url = f"https://wa.me/{phone_clean}?text={quote(msg)}"

    # Mark as sent
    design.sent_to_whatsapp = target_phone
    design.sent_at = timezone.now()
    design.save(update_fields=['sent_to_whatsapp', 'sent_at'])

    return JsonResponse({"status": "success", "whatsapp_url": wa_url})


def design_store_download(request, design_code, fmt):
    """📥 تحميل التصميم بصيغ مختلفة (png, jpg, pdf)."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "غير مصرح"}, status=401)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)

    if design.is_free_trial:
        return JsonResponse({"error": "التحميل غير متاح في التجربة المجانية"}, status=403)

    fmt = fmt.lower()
    if fmt not in ('png', 'jpg', 'jpeg', 'pdf'):
        return JsonResponse({"error": "صيغة غير مدعومة. الصيغ المتاحة: png, jpg, pdf"}, status=400)

    import io
    from django.core.files.storage import default_storage

    # ── Step 1: Load image bytes ──────────────────────────────────
    img_data = None
    if design.image_url:
        url = design.image_url

        # Try local file first (extract path from URL)
        for prefix in ['/media/', 'media/']:
            if prefix in url:
                rel_path = url.split(prefix, 1)[-1]
                try:
                    if default_storage.exists(rel_path):
                        with default_storage.open(rel_path, 'rb') as f:
                            img_data = f.read()
                        break
                except Exception:
                    pass

        # Fallback: download from URL
        if not img_data:
            try:
                import requests as _req
                r = _req.get(url, timeout=30)
                if r.status_code == 200:
                    img_data = r.content
            except Exception as e:
                logger.error(f"[DOWNLOAD] Failed to fetch image: {e}")

    if not img_data:
        return JsonResponse({"error": "تعذر تحميل الصورة — الملف غير موجود"}, status=404)

    # ── Step 2: For PNG, serve raw (no conversion needed) ─────────
    if fmt == 'png':
        # Even if source is WebP, convert to actual PNG
        try:
            from PIL import Image as PILImage
            img = PILImage.open(io.BytesIO(img_data))
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            img_data = buf.getvalue()
        except Exception:
            pass  # Serve raw bytes if PIL fails
        response = HttpResponse(img_data, content_type='image/png')
        response['Content-Disposition'] = f'attachment; filename="design_{design.design_code}.png"'
        design.download_count += 1
        design.save(update_fields=['download_count'])
        return response

    # ── Step 3: Convert to JPG or PDF ────────────────────────────
    try:
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(img_data))
    except Exception as e:
        logger.error(f"[DOWNLOAD] PIL cannot open image: {e}")
        return JsonResponse({"error": "تعذر فتح الصورة للتحويل"}, status=500)

    # Convert to RGB for JPG/PDF (remove alpha channel)
    if img.mode in ('RGBA', 'P', 'LA', 'PA'):
        bg = PILImage.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'P':
            img = img.convert('RGBA')
        try:
            bg.paste(img, mask=img.split()[-1] if 'A' in img.mode else None)
        except Exception:
            bg.paste(img)
        img = bg
    elif img.mode != 'RGB':
        img = img.convert('RGB')

    buf = io.BytesIO()

    if fmt in ('jpg', 'jpeg'):
        img.save(buf, format='JPEG', quality=95)
        response = HttpResponse(buf.getvalue(), content_type='image/jpeg')
        response['Content-Disposition'] = f'attachment; filename="design_{design.design_code}.jpg"'
    elif fmt == 'pdf':
        img.save(buf, format='PDF', resolution=300)
        response = HttpResponse(buf.getvalue(), content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="design_{design.design_code}.pdf"'

    design.download_count += 1
    design.save(update_fields=['download_count'])
    return response


@csrf_exempt
def design_store_regenerate(request, design_code):
    """🔄 إعادة توليد تصميم — مجاناً ضمن الحد المسموح."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "غير مصرح"}, status=401)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)
    if not design.can_regenerate:
        return JsonResponse({
            "error": f"استنفدت إعادة التوليد المسموحة ({design.regenerations_allowed} مرات)",
        }, status=403)

    # 🛡️ Rate limiting — shared with generate (5/min per customer)
    gen_rate_key = f'design_gen_rate:{customer.pk}'
    gen_count = cache.get(gen_rate_key, 0)
    if gen_count >= 5:
        return JsonResponse({"error": "أنت ترسل طلبات كثيرة. انتظر دقيقة ثم حاول مرة أخرى."}, status=429)
    cache.set(gen_rate_key, gen_count + 1, 60)

    # Re-generate with same specs by calling generate endpoint internally
    request.POST = request.POST.copy()
    request.POST['title'] = design.title
    request.POST['description'] = design.description
    request.POST['category'] = design.category
    request.POST['size_preset'] = design.size_preset
    request.POST['output_format'] = design.output_format

    # Don't consume from package — increment regen counter instead
    design.regenerations_used += 1
    design.save(update_fields=['regenerations_used'])

    # 🎨 Phase N.6+: regenerate now uses the SAME unified pipeline as
    # design_store_generate — Brand Profile + Smart Router (FLUX/Ideogram) +
    # PIL logo composite + quality gate. The original engineered_prompt is
    # passed through compose_mega_prompt with already_engineered=True so the
    # legacy prompt tuning is preserved (no double LLM rewrite).
    regen_prompt = design.engineered_prompt or design.description
    if not regen_prompt or len(regen_prompt) < 10:
        regen_prompt = f"Create a professional design: {design.title}. {design.description}"

    SUPPORTED = {'1024x1024', '1024x1536', '1536x1024'}
    sz = design.size_preset if design.size_preset in SUPPORTED else '1024x1024'

    result = _run_marketplace_image_pipeline(
        request, customer,
        engineered_prompt=regen_prompt,
        description=design.description or design.raw_input or '',
        category=design.category or 'other',
        canonical_size=sz,
        prefix='ai_store',
    )
    if not result['ok']:
        # Preserve regen-specific Arabic message while surfacing engine_error.
        payload = dict(result['error_payload'])
        payload.setdefault('error', 'فشل إعادة التوليد. حاول تاني.')
        return JsonResponse(payload, status=result['status'])

    new_url = result['image_url']
    design.image_url = new_url
    design.model_used = result['used_model'] or 'unknown'
    design.save(update_fields=['image_url', 'model_used'])

    # Chat history
    try:
        from clients.models import DesignChatMessage
        DesignChatMessage.objects.create(
            design=design, role='user',
            content='إعادة توليد التصميم بنفس المواصفات',
        )
        DesignChatMessage.objects.create(
            design=design, role='assistant',
            content='تم إعادة توليد التصميم', image_url=new_url,
        )
    except Exception:
        pass

    return JsonResponse({
        "status": "success",
        "image_url": new_url,
        "regenerations_left": design.regenerations_allowed - design.regenerations_used,
        # 🆕 Phase N.6+ parity with generate
        "engine_used": result['used_engine'],
        "brand_applied": result['brand_applied'],
        "logo_composited": result['logo_composited'],
        "quality_score": result['quality_score'],
    })


@csrf_exempt
def design_store_print_request(request, design_code):
    """🖨️ طلب طباعة تصميم — العميل عجبه التصميم وعاوز يطبعه."""
    from clients.models import DesignPrintRequest

    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "يجب تسجيل الدخول"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)

    product_type = request.POST.get('product_type', 'other')
    quantity = request.POST.get('quantity', '1')
    width_cm = request.POST.get('width_cm', '').strip()
    height_cm = request.POST.get('height_cm', '').strip()
    paper_type = request.POST.get('paper_type', '').strip()
    color_mode = request.POST.get('color_mode', 'full_color')
    finishing = request.POST.get('finishing', '').strip()
    notes = request.POST.get('notes', '').strip()
    delivery_address = request.POST.get('delivery_address', '').strip()
    delivery_phone = request.POST.get('delivery_phone', '').strip()

    try:
        qty = int(quantity)
        if qty < 1:
            qty = 1
    except (ValueError, TypeError):
        qty = 1

    print_req = DesignPrintRequest.objects.create(
        design=design,
        customer=customer,
        product_type=product_type,
        quantity=qty,
        width_cm=Decimal(width_cm) if width_cm else None,
        height_cm=Decimal(height_cm) if height_cm else None,
        paper_type=paper_type,
        color_mode=color_mode,
        finishing=finishing,
        notes=notes,
        delivery_address=delivery_address,
        delivery_phone=delivery_phone or customer.phone,
        status='pending',
    )

    logger.info(f"[PRINT REQUEST] #{print_req.pk} — {customer.full_name} wants to print design {design.design_code}")

    return JsonResponse({
        "status": "success",
        "request_id": print_req.pk,
        "request_code": str(print_req.request_code),
        "message": "تم إرسال طلب الطباعة بنجاح! سنتواصل معك قريباً بعرض السعر.",
    })


@csrf_exempt
def design_store_send_to_marketplace(request, design_code):
    """🛒 إرسال التصميم لسوق B2B — ينشئ ServiceRequest للتجار (المطابع) يقدموا عروض."""
    from clients.models import ServiceRequest

    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "يجب تسجيل الدخول"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)

    if design.is_free_trial:
        return JsonResponse({"error": "هذه الميزة غير متاحة في التجربة المجانية"}, status=403)

    # Check if already sent to marketplace
    existing = ServiceRequest.objects.filter(
        customer=customer,
        title__contains=str(design.design_code)[:8],
        status='open',
    ).first()
    if existing:
        return JsonResponse({
            "status": "already_exists",
            "request_code": str(existing.request_code),
            "message": "التصميم موجود بالفعل في السوق وبيستقبل عروض.",
        })

    notes = request.POST.get('notes', '').strip()
    quantity = request.POST.get('quantity', '1').strip()
    urgency = request.POST.get('urgency', 'normal')

    try:
        qty = int(quantity)
        if qty < 1:
            qty = 1
    except (ValueError, TypeError):
        qty = 1

    # Build description for merchants
    desc = (
        f"طلب طباعة تصميم AI — {design.get_category_display()}\n"
        f"المقاس: {design.actual_size_label}\n"
        f"الكمية: {qty}\n"
    )
    if notes:
        desc += f"ملاحظات العميل: {notes}\n"
    desc += f"\nرابط التصميم: {design.image_url}"

    # Create ServiceRequest in B2B marketplace
    from datetime import timedelta
    sr = ServiceRequest.objects.create(
        customer=customer,
        sector='printing',
        title=f"طباعة {design.get_category_display()} — {design.title[:60]} [{str(design.design_code)[:8]}]",
        description=desc,
        urgency=urgency if urgency in ('normal', 'soon', 'urgent') else 'normal',
        customer_city=customer.city or '',
        expires_at=timezone.now() + timedelta(days=7),
    )

    # Attach design image as reference
    if design.image_url:
        try:
            import requests as _req
            from django.core.files.base import ContentFile
            r = _req.get(design.image_url, timeout=15)
            if r.status_code == 200:
                from django.core.files.uploadedfile import InMemoryUploadedFile
                import io
                sr.attachment_1.save(
                    f"design_{design.design_code}.png",
                    ContentFile(r.content),
                    save=True,
                )
        except Exception as e:
            logger.warning(f"[MARKETPLACE] Failed to attach design image: {e}")

    logger.info(f"[MARKETPLACE] Design {design.design_code} sent to B2B by {customer.full_name}")

    return JsonResponse({
        "status": "success",
        "request_code": str(sr.request_code),
        "message": f"تم نشر تصميمك في سوق الطباعة. المطابع هتبدأ تبعتلك عروض أسعار قريباً.",
    })


@csrf_exempt
def design_store_watermark(request, design_code):
    """💧 إضافة / إزالة علامة مائية على التصميم."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "يجب تسجيل الدخول"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)

    if design.is_free_trial:
        return JsonResponse({"error": "العلامة المائية غير متاحة في التجربة المجانية"}, status=403)

    watermark_text = request.POST.get('text', customer.company_name or customer.full_name).strip()
    if not watermark_text:
        watermark_text = 'Mouss Tec AI Design'

    # Get the original image
    from django.core.files.storage import default_storage
    import io
    from PIL import Image as PILImage, ImageDraw, ImageFont

    img_data = None
    # Try local storage first
    if design.image_url:
        url = design.image_url
        for prefix in ['/media/', 'media/']:
            if prefix in url:
                rel_path = url.split(prefix, 1)[-1]
                if default_storage.exists(rel_path):
                    with default_storage.open(rel_path, 'rb') as f:
                        img_data = f.read()
                break

    if not img_data:
        try:
            import requests as _req
            r = _req.get(design.image_url, timeout=30)
            if r.status_code == 200:
                img_data = r.content
        except Exception:
            pass

    if not img_data:
        return JsonResponse({"error": "تعذر تحميل الصورة"}, status=404)

    # Apply watermark
    img = PILImage.open(io.BytesIO(img_data)).convert('RGBA')
    w, h = img.size

    # Create transparent overlay
    overlay = PILImage.new('RGBA', (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Use a large font size relative to image
    font_size = max(w, h) // 15
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()

    # Draw diagonal watermark text multiple times across image
    import math
    diagonal = int(math.sqrt(w**2 + h**2))
    step_y = font_size * 3

    for y_offset in range(-diagonal, diagonal, step_y):
        for x_offset in range(-w, w * 2, len(watermark_text) * font_size):
            draw.text(
                (x_offset, y_offset),
                watermark_text,
                font=font,
                fill=(255, 255, 255, 45),  # Semi-transparent white
            )

    # Rotate overlay
    overlay = overlay.rotate(30, expand=False, center=(w // 2, h // 2))

    # Composite
    watermarked = PILImage.alpha_composite(img, overlay)
    watermarked_rgb = watermarked.convert('RGB')

    # Save watermarked version
    import uuid as _uuid
    from django.core.files.base import ContentFile
    buf = io.BytesIO()
    watermarked_rgb.save(buf, format='PNG', quality=95)
    buf.seek(0)

    filename = f"ai_store/{customer.uid}/wm_{_uuid.uuid4().hex}.png"
    saved_path = default_storage.save(filename, ContentFile(buf.getvalue()))
    wm_url = default_storage.url(saved_path)
    if wm_url.startswith('/'):
        wm_url = request.build_absolute_uri(wm_url)

    return JsonResponse({
        "status": "success",
        "watermarked_url": wm_url,
        "message": "تم إضافة العلامة المائية بنجاح",
    })


@csrf_exempt
def design_store_chat_history(request, design_code):
    """💬 جلب تاريخ المحادثة لتصميم معين."""
    from clients.models import DesignChatMessage
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "غير مصرح"}, status=401)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)
    messages = design.chat_messages.order_by('created_at').values(
        'role', 'content', 'image_url', 'is_refinement', 'created_at'
    )
    return JsonResponse({
        "status": "success",
        "design_code": str(design.design_code),
        "title": design.title,
        "regenerations_used": design.regenerations_used,
        "regenerations_allowed": design.regenerations_allowed,
        "regenerations_left": design.regenerations_allowed - design.regenerations_used,
        "can_refine": design.can_regenerate,
        "messages": [
            {
                "role": m['role'],
                "content": m['content'],
                "image_url": m['image_url'],
                "is_refinement": m['is_refinement'],
                "time": m['created_at'].strftime("%d/%m %H:%M"),
            }
            for m in messages
        ],
    })


@csrf_exempt
def design_store_refine(request, design_code):
    """✏️ تعديل تحسيني — العميل يكتب تعليمات إضافية بدون إعادة توليد كامل."""
    from clients.models import DesignChatMessage
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "غير مصرح"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)

    if not design.can_regenerate:
        return JsonResponse({
            "error": f"استنفدت محاولات التعديل ({design.regenerations_allowed} مرات)",
        }, status=403)

    # Rate limiting
    gen_rate_key = f'design_gen_rate:{customer.pk}'
    gen_count = cache.get(gen_rate_key, 0)
    if gen_count >= 5:
        return JsonResponse({"error": "انتظر دقيقة ثم حاول مرة أخرى."}, status=429)
    cache.set(gen_rate_key, gen_count + 1, 60)

    refinement_text = request.POST.get('refinement', '').strip()
    if not refinement_text or len(refinement_text) < 3:
        return JsonResponse({"error": "اكتب التعديل المطلوب (3 أحرف على الأقل)"}, status=400)

    # Save user refinement message
    DesignChatMessage.objects.create(
        design=design, role='user', content=refinement_text, is_refinement=True
    )

    # 🔄 Placement detection — front/back من الـ refinement_text + الـ raw_input الأصلي
    from erp_core.ai.printing_copilot import detect_placement_from_text
    combined_signals = f"{design.raw_input or ''} {refinement_text}"
    placement = detect_placement_from_text(combined_signals)

    # Build refinement prompt + inject placement instruction لـ FLUX
    base_prompt = design.engineered_prompt or design.description
    placement_instr = ''
    if placement == 'back':
        placement_instr = (
            ' SHOT COMPOSITION: rear/back view of the apparel showing the back panel, '
            'with a clean blank area centered between the shoulder blades (upper-back '
            'area, ~38% from top) ready for text overlay afterward.'
        )
    else:
        placement_instr = (
            ' SHOT COMPOSITION: front view of the apparel with a clean blank area on '
            'the upper-chest region (~32% from top) ready for text overlay afterward.'
        )
    refinement_prompt = (
        f"{base_prompt[:2500]} "
        f"REFINEMENT INSTRUCTION: The client wants to modify the existing design. "
        f"Keep everything from the original design but apply these changes: {refinement_text}. "
        f"Maintain the same style, color palette, and composition. "
        f"Only change what the client specifically asked for."
        f"{placement_instr}"
    )

    # 🎨 Phase N.6+ — C3 fix: inject Brand Profile into the refinement prompt so
    # the full-regenerate fallback path honors the customer's brand colors /
    # aesthetic. already_engineered=True keeps the legacy prompt intact (no
    # double LLM rewrite). Kontext i2i ignores the mega prompt itself, but the
    # logo composite step below still runs for both methods.
    brand_context, brand_logo_source, _ = _resolve_brand_context(customer)
    brand_applied = False
    try:
        from erp_core.ai.design_engine import compose_mega_prompt
        mega = compose_mega_prompt(
            raw_idea=refinement_prompt,
            domain=design.category if design.category != 'other' else '',
            selections={},
            brand_context=brand_context,
            presentation_category=design.category if design.category != 'other' else None,
            already_engineered=True,
        )
        if mega.get('success'):
            refinement_prompt = mega['mega_prompt']
            brand_applied = (mega.get('brand_applied') or {}).get('applied', False)
    except Exception as e:
        logger.warning(f'[REFINE] brand injection failed (non-fatal): {e}')

    # 🧠 Smart Refine: Kontext i2i لو الـ intent يدعمه، fallback لـ full regenerate.
    # ده بيخلي "غيّر اللون" يعدل الصورة فعلاً بدل ما يبني واحدة جديدة من الصفر.
    # الـ classifier يصنف الـ intent + يحدد لو الـ Kontext يقدر يتعامل معاه.
    try:
        from erp_core.ai.printing_copilot import (
            classify_refinement_intent, refine_design_image, generate_flux_image,
        )
        SUPPORTED = {'1024x1024', '1024x1536', '1536x1024'}
        sz = design.size_preset if design.size_preset in SUPPORTED else '1024x1024'

        # 🎯 Classify intent (color/add/remove/style/text/regenerate)
        intent_info = classify_refinement_intent(refinement_text)
        logger.info(
            f'[REFINE] intent={intent_info["intent"]} '
            f'can_kontext={intent_info["can_use_kontext"]} '
            f'confidence={intent_info["confidence"]} '
            f'signals={intent_info["detected_signals"]}'
        )

        # 🌐 لـ Kontext: نـ translate الـ refinement للإنجليزي (Kontext أحسن
        # مع English). نستخدم نفس الـ LLM gateway. لو فشلت الترجمة، نمشي بالعربي.
        refinement_en = refinement_text
        if intent_info['can_use_kontext']:
            try:
                from inventory.ai_services import call_llm_layer
                translated = call_llm_layer([
                    {'role': 'system', 'content': (
                        'You translate Arabic image-editing instructions to '
                        'concise English for FLUX.1-Kontext. Reply with ONE short '
                        'English sentence — no explanation, no quotes.'
                    )},
                    {'role': 'user', 'content': refinement_text},
                ], json_mode=False, max_retries=1)
                if translated and len(translated.strip()) > 2:
                    refinement_en = translated.strip().strip('"\'')[:500]
            except Exception as e:
                logger.warning(f'[REFINE] translation failed (non-fatal): {e}')

        # 🎨 Run the smart refine — Kontext or full regen
        flux_result = refine_design_image(
            image_url=design.image_url,
            refinement_text_en=refinement_en,
            size=sz,
            category=design.category,
            intent=intent_info['intent'],
            fallback_full_prompt=refinement_prompt[:1800],
            negative_prompt="low quality, blurry, watermark, distorted text, inconsistent style",
        )
        if not flux_result.get('success'):
            err = flux_result.get('error', 'unknown')
            logger.error(f"[REFINE] failed: {err} — {flux_result.get('detail', '')[:200]}")
            return JsonResponse({
                "error": "فشل التعديل. حاول تاني بصياغة مختلفة.",
                "engine_error": err,
                "intent": intent_info['intent'],
            }, status=502)

        refinement_method = flux_result.get('refinement_method', 'unknown')
        logger.info(
            f'[REFINE] success — method={refinement_method} '
            f'model={flux_result.get("model")} intent={intent_info["intent"]}'
        )

        new_url = _persist_remote_image(
            request, customer,
            url=flux_result.get('url'), b64=flux_result.get('b64_json'),
            prefix='ai_store',
        )
        if not new_url:
            return JsonResponse({"error": "لم يتم استلام صورة من Together AI"}, status=502)

        # 🅰️ Text overlay — لو الـ design الأصلي كان فيه نص عربي، نـ re-extract
        # ونـ apply على الصورة الجديدة في الـ position المناسب للـ placement.
        overlay_applied = False
        try:
            from erp_core.ai.printing_copilot import _extract_text_overlay_from_brief
            from erp_core.ai.text_overlay import overlay_text_on_image_url
            extracted = _extract_text_overlay_from_brief(
                design.raw_input or design.title or '',
                design.category or 'other',
            )
            if extracted:
                # نـ override الـ position حسب الـ placement
                extracted['position'] = 'back' if placement == 'back' else 'chest'
                ov = overlay_text_on_image_url(
                    image_url=new_url,
                    text=extracted['text'],
                    position=extracted['position'],
                    color=extracted.get('color', '#000000'),
                    font_size_ratio=float(extracted.get('font_ratio', 0.045)),
                )
                if ov.get('success'):
                    final_url = ov['url']
                    if final_url and final_url.startswith('/'):
                        final_url = request.build_absolute_uri(final_url)
                    new_url = final_url
                    overlay_applied = True
                    logger.info(f"[REFINE] Arabic overlay applied at position={extracted['position']}")
        except Exception as e:
            logger.warning(f"[REFINE] overlay step failed (non-fatal): {e}")

        # 🎨 Phase N.6+ — C3 fix: composite the customer's brand logo on top
        # of the refined image (FLUX/Kontext output — Ideogram is not used by
        # refine, so we always attempt). Non-fatal on failure.
        new_url, logo_composited = _composite_brand_logo(
            request, image_url=new_url, logo_source=brand_logo_source,
            used_engine=flux_result.get('engine', 'flux'),
            presentation_category=design.category,
            text_overlay=None, prefix='ai_store',
        )

        # 💾 احفظ الصورة الأصلية قبل التعديل عشان العميل يقارن بعدين
        previous_image_url = design.image_url

        # Update design
        design.image_url = new_url
        design.regenerations_used += 1
        design.engineered_prompt = refinement_prompt[:4000]
        design.model_used = flux_result.get('model', 'unknown')
        design.save(update_fields=['image_url', 'regenerations_used', 'engineered_prompt', 'model_used'])

        # Save AI response message — مع الـ method label عشان UI يعرف يعرض الـ badge
        method_label = {
            'kontext_i2i': '✏️ تعديل دقيق',
            'full_regenerate': '🔄 إعادة توليد كامل',
        }.get(refinement_method, refinement_method)
        placement_label = 'في الضهر 🔄' if placement == 'back' else 'في الصدر'
        DesignChatMessage.objects.create(
            design=design, role='assistant',
            content=f"تم {method_label} ({placement_label}): {refinement_text[:100]}",
            image_url=new_url, is_refinement=True,
        )

        return JsonResponse({
            "status": "success",
            "image_url": new_url,
            "previous_image_url": previous_image_url,    # للـ before/after comparison
            "regenerations_left": design.regenerations_allowed - design.regenerations_used,
            "regenerations_used": design.regenerations_used,
            "regenerations_allowed": design.regenerations_allowed,
            "placement": placement,
            "overlay_applied": overlay_applied,
            "refinement_method": refinement_method,      # kontext_i2i | full_regenerate
            "refinement_intent": intent_info['intent'],   # color_change | style_tweak | ...
            "intent_confidence": intent_info['confidence'],
            "used_edit_api": refinement_method == 'kontext_i2i',
            "model": flux_result.get('model'),
            # 🆕 Phase N.6+ parity with generate
            "brand_applied": brand_applied,
            "logo_composited": logo_composited,
        })
    except Exception as e:
        logger.error(f"[REFINE] Failed: {e}")
        return JsonResponse({"error": f"فشل التعديل: {str(e)[:100]}"}, status=500)
