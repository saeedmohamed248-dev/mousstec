"""
🚗 P2P Car-Parts Marketplace Views.

Flow:
  Seller flow:    /marketplace/parts/sell/   → upload photos → publish listing
  Buyer flow:     /marketplace/parts/       → filter by make → detail → checkout (Paymob) → escrow
  Post-purchase:  /marketplace/parts/orders/ → confirm delivery → warranty window → auto-release

Commission: 8% for individual sellers, 4% for tenant (company) sellers.
Return shipping: always paid by the platform out of commission.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from clients.models import (
    CustomerNotification,
    MarketplaceCustomer,
    PartCarMake,
    PartListing,
    PartListingPhoto,
    PartOrder,
    PlatformEvent,
)
from clients.views._shared import _marketplace_auth

logger = logging.getLogger('mouss_tec_core')


# ─────────────────────────────────────────────────────────────────────
# 1. PUBLIC FEED — anyone can browse, only logged-in customers can buy
# ─────────────────────────────────────────────────────────────────────

def parts_feed(request):
    """Public feed of active part listings, filterable by car make."""
    if getattr(connection, 'schema_name', 'public') != 'public':
        return HttpResponseForbidden('Access from main site only')

    makes = PartCarMake.objects.filter(is_active=True).order_by('sort_order', 'name')
    selected_make_slug = request.GET.get('make', '').strip().lower()
    q = request.GET.get('q', '').strip()
    condition = request.GET.get('condition', '').strip()
    sort = request.GET.get('sort', 'new')

    listings = PartListing.objects.filter(status='active').select_related('car_make')

    selected_make = None
    if selected_make_slug:
        selected_make = makes.filter(slug=selected_make_slug).first()
        if selected_make:
            listings = listings.filter(car_make=selected_make)
    if q:
        listings = listings.filter(title__icontains=q) | listings.filter(description__icontains=q)
    if condition:
        listings = listings.filter(condition=condition)
    if sort == 'price_low':
        listings = listings.order_by('price_egp', '-created_at')
    elif sort == 'price_high':
        listings = listings.order_by('-price_egp', '-created_at')
    else:
        listings = listings.order_by('-created_at')

    listings = listings[:60]

    customer = _marketplace_auth(request)
    return render(request, 'clients/marketplace/parts_feed.html', {
        'makes': makes,
        'selected_make': selected_make,
        'selected_make_slug': selected_make_slug,
        'listings': listings,
        'q': q, 'condition': condition, 'sort': sort,
        'customer': customer,
        'condition_choices': PartListing.CONDITION_CHOICES,
    })


def parts_detail(request, listing_code):
    """Public detail page of a listing."""
    if getattr(connection, 'schema_name', 'public') != 'public':
        return HttpResponseForbidden('Access from main site only')

    listing = get_object_or_404(
        PartListing.objects.select_related('car_make', 'seller_customer', 'seller_tenant')
                            .prefetch_related('photos'),
        listing_code=listing_code,
    )
    if listing.status not in ('active', 'reserved', 'sold'):
        return HttpResponseForbidden('This listing is unavailable.')

    # Count view
    PartListing.objects.filter(pk=listing.pk).update(views_count=listing.views_count + 1)

    customer = _marketplace_auth(request)
    is_owner = bool(customer and listing.seller_customer_id == customer.pk)

    return render(request, 'clients/marketplace/parts_detail.html', {
        'listing': listing,
        'photos': list(listing.photos.all()),
        'customer': customer,
        'is_owner': is_owner,
    })


# ─────────────────────────────────────────────────────────────────────
# 2. SELLER FLOW — list a part for sale (customer-side, simplest path)
# ─────────────────────────────────────────────────────────────────────

def parts_create(request):
    """Form to create a new listing (customer-only for now)."""
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/login/')
    if customer.sector != 'automotive':
        return render(request, 'clients/marketplace/parts_unavailable.html', {
            'reason': 'سوق قطع الغيار خاص بقطاع السيارات فقط.',
        }, status=403)

    makes = PartCarMake.objects.filter(is_active=True).order_by('sort_order', 'name')

    if request.method == 'POST':
        try:
            with transaction.atomic():
                make_id = int(request.POST.get('car_make') or 0)
                make = get_object_or_404(PartCarMake, pk=make_id, is_active=True)
                price = Decimal(request.POST.get('price_egp') or '0')
                warranty = int(request.POST.get('warranty_days') or 3)
                if price <= 0:
                    return JsonResponse({'error': 'السعر يجب أن يكون أكبر من صفر.'}, status=400)
                if warranty < 1 or warranty > 90:
                    return JsonResponse({'error': 'فترة الضمان لازم بين 1 و 90 يوم.'}, status=400)

                photos = request.FILES.getlist('photos')
                if len(photos) < 3:
                    return JsonResponse({
                        'error': 'لازم ترفع 3 صور على الأقل لتوثيق حالة القطعة من كل الزوايا.'
                    }, status=400)

                year_from = request.POST.get('car_year_from') or None
                year_to   = request.POST.get('car_year_to') or None

                listing = PartListing.objects.create(
                    seller_customer=customer,
                    title=(request.POST.get('title') or '').strip()[:200],
                    description=(request.POST.get('description') or '').strip(),
                    car_make=make,
                    car_model=(request.POST.get('car_model') or '').strip()[:100],
                    car_year_from=int(year_from) if year_from else None,
                    car_year_to=int(year_to) if year_to else None,
                    part_number=(request.POST.get('part_number') or '').strip()[:120],
                    condition=(request.POST.get('condition') or 'used_good'),
                    price_egp=price,
                    warranty_days=warranty,
                    city=(request.POST.get('city') or customer.city or '').strip()[:100],
                    status='active',
                )
                for idx, photo in enumerate(photos[:10]):  # cap at 10 photos
                    PartListingPhoto.objects.create(
                        listing=listing, image=photo,
                        is_primary=(idx == 0), sort_order=idx,
                    )
                PartCarMake.objects.filter(pk=make.pk).update(listings_count=make.listings_count + 1)

                PlatformEvent.objects.create(
                    event_type='other', tenant_schema='public', tenant_name='parts_market',
                    user_name=customer.full_name,
                    description=f"🛒 قطعة جديدة معروضة: «{listing.title}» — {make.name} — {price} ج.م",
                )
            return JsonResponse({
                'ok': True,
                'message': 'تم نشر القطعة بنجاح.',
                'listing_code': str(listing.listing_code),
                'detail_url': f'/marketplace/parts/{listing.listing_code}/',
            })
        except Exception as exc:
            logger.exception("[PARTS] Failed to create listing: %s", exc)
            return JsonResponse({'error': f'فشل النشر: {exc}'}, status=500)

    return render(request, 'clients/marketplace/parts_create.html', {
        'customer': customer,
        'makes': makes,
        'condition_choices': PartListing.CONDITION_CHOICES,
    })


# ─────────────────────────────────────────────────────────────────────
# 3. BUYER FLOW — checkout via Paymob with escrow hold
# ─────────────────────────────────────────────────────────────────────

def _paymob_create_payment(amount_egp: Decimal, merchant_order_id: str,
                           billing: dict) -> tuple[str | None, str | None, str | None]:
    """Returns (iframe_url, paymob_order_id, error_message)."""
    import requests as http_requests

    api_key = getattr(settings, 'PAYMOB_API_KEY', '') or os.getenv('PAYMOB_API_KEY', '')
    integration_id = getattr(settings, 'PAYMOB_INTEGRATION_ID', '') or os.getenv('PAYMOB_INTEGRATION_ID', '')
    iframe_id = getattr(settings, 'PAYMOB_IFRAME_ID', '') or os.getenv('PAYMOB_IFRAME_ID', '')

    if not api_key or not integration_id or not iframe_id:
        return None, None, "إعدادات الدفع غير مكتملة على الخادم."

    try:
        integration_id_int = int(integration_id)
    except (TypeError, ValueError):
        return None, None, "إعدادات الدفع غير صحيحة."

    amount_cents = int(Decimal(amount_egp) * 100)

    try:
        # Auth
        auth_res = http_requests.post('https://accept.paymob.com/api/auth/tokens',
                                       json={'api_key': api_key}, timeout=15)
        if auth_res.status_code not in (200, 201):
            logger.error("[PAYMOB-PARTS] auth failed: %s — %s", auth_res.status_code, auth_res.text[:200])
            return None, None, "فشل المصادقة مع بوابة الدفع."
        auth_token = auth_res.json().get('token')

        # Create order
        order_res = http_requests.post('https://accept.paymob.com/api/ecommerce/orders', json={
            'auth_token': auth_token, 'delivery_needed': 'false',
            'amount_cents': amount_cents, 'currency': 'EGP',
            'items': [{'name': 'Mouss Tec Parts Purchase',
                       'amount_cents': amount_cents, 'quantity': '1'}],
            'merchant_order_id': merchant_order_id,
        }, timeout=15)
        if order_res.status_code not in (200, 201):
            logger.error("[PAYMOB-PARTS] order failed: %s — %s", order_res.status_code, order_res.text[:200])
            return None, None, "فشل إنشاء طلب الدفع."
        paymob_order_id = str(order_res.json().get('id') or '')

        # Payment key
        key_res = http_requests.post('https://accept.paymob.com/api/acceptance/payment_keys', json={
            'auth_token': auth_token, 'amount_cents': amount_cents,
            'expiration': 3600, 'order_id': paymob_order_id,
            'billing_data': billing, 'currency': 'EGP',
            'integration_id': integration_id_int, 'lock_order_when_paid': 'true',
        }, timeout=15)
        if key_res.status_code not in (200, 201):
            logger.error("[PAYMOB-PARTS] key failed: %s — %s", key_res.status_code, key_res.text[:200])
            return None, None, "فشل إصدار رمز الدفع."
        payment_token = key_res.json().get('token')

        iframe_url = f'https://accept.paymob.com/api/acceptance/iframes/{iframe_id}?payment_token={payment_token}'
        return iframe_url, paymob_order_id, None

    except http_requests.RequestException as exc:
        logger.exception("[PAYMOB-PARTS] network error: %s", exc)
        return None, None, "تعذر الاتصال ببوابة الدفع."


@require_POST
def parts_checkout(request, listing_code):
    """Initiate Paymob payment for a listing. Reserves the listing + creates a PartOrder."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'سجل دخول أولاً.'}, status=401)

    listing = get_object_or_404(PartListing, listing_code=listing_code)
    if listing.status != 'active':
        return JsonResponse({'error': 'القطعة لم تعد متاحة.'}, status=400)
    if listing.seller_customer_id == customer.pk:
        return JsonResponse({'error': 'لا يمكنك شراء قطعتك الخاصة.'}, status=400)

    shipping_name    = (request.POST.get('shipping_name') or customer.full_name).strip()[:120]
    shipping_phone   = (request.POST.get('shipping_phone') or customer.phone).strip()[:30]
    shipping_address = (request.POST.get('shipping_address') or '').strip()
    shipping_city    = (request.POST.get('shipping_city') or customer.city or '').strip()[:80]

    if not shipping_address or len(shipping_address) < 10:
        return JsonResponse({'error': 'لازم تكتب العنوان كاملاً.'}, status=400)

    with transaction.atomic():
        # Lock the listing row to prevent double-buy
        listing = PartListing.objects.select_for_update().get(pk=listing.pk)
        if listing.status != 'active':
            return JsonResponse({'error': 'القطعة محجوزة الآن.'}, status=400)
        listing.status = 'reserved'
        listing.save(update_fields=['status'])

        order = PartOrder.objects.create(
            listing=listing,
            buyer_customer=customer,
            amount_paid=listing.price_egp,
            commission_amount=listing.commission_amount,
            seller_payout=listing.seller_payout,
            warranty_days=listing.warranty_days,
            status='pending_payment',
            shipping_name=shipping_name,
            shipping_phone=shipping_phone,
            shipping_address=shipping_address,
            shipping_city=shipping_city,
        )

    # Build Paymob billing block
    name_parts = shipping_name.split(maxsplit=1)
    first_name = name_parts[0] if name_parts else 'Customer'
    last_name  = name_parts[1] if len(name_parts) > 1 else 'MoussTec'
    billing = {
        'first_name': first_name[:50] or 'Customer',
        'last_name':  last_name[:50] or 'MoussTec',
        'email': customer.email or 'customer@mousstec.com',
        'phone_number': shipping_phone or '01000000000',
        'apartment': 'NA', 'floor': 'NA', 'street': shipping_address[:60] or 'NA',
        'building': 'NA', 'shipping_method': 'NA', 'postal_code': 'NA',
        'city': shipping_city or 'Cairo', 'country': 'EG', 'state': shipping_city or 'Cairo',
    }
    merchant_order_id = f'parts_{order.order_code.hex[:12]}'

    iframe_url, paymob_order_id, err = _paymob_create_payment(
        listing.price_egp, merchant_order_id, billing,
    )
    if err:
        # Roll back: free the listing again
        PartListing.objects.filter(pk=listing.pk, status='reserved').update(status='active')
        order.status = 'cancelled'
        order.save(update_fields=['status'])
        return JsonResponse({'error': err}, status=502)

    PartOrder.objects.filter(pk=order.pk).update(paymob_order_id=paymob_order_id)
    cache.set(f'paymob_part_order_{paymob_order_id}', str(order.order_code), timeout=7200)

    return JsonResponse({'ok': True, 'iframe_url': iframe_url, 'order_code': str(order.order_code)})


def _verify_paymob_hmac(request, body_data: dict) -> tuple[bool, str]:
    """
    🛡️ Verify Paymob HMAC-SHA512 signature.

    Returns (is_valid, reason). When PAYMOB_HMAC_SECRET is unset, logs a
    warning and accepts the callback (dev-mode). In production the env var
    MUST be set or all callbacks are rejected — see PAYMOB_REQUIRE_HMAC.
    """
    secret = os.getenv('PAYMOB_HMAC_SECRET', '')
    received = request.GET.get('hmac', '') or body_data.get('hmac', '')
    require_hmac = os.getenv('PAYMOB_REQUIRE_HMAC', '').lower() in ('1', 'true', 'yes')

    if not secret:
        if require_hmac:
            logger.critical("🚨 [PAYMOB-PARTS] PAYMOB_HMAC_SECRET not set but PAYMOB_REQUIRE_HMAC=1 — rejecting")
            return False, 'hmac_secret_missing'
        logger.warning("⚠️ [PAYMOB-PARTS] PAYMOB_HMAC_SECRET unset — HMAC skipped (dev mode)")
        return True, 'skipped'

    if not received:
        logger.critical("🚨 [PAYMOB-PARTS] No hmac in callback — rejecting")
        return False, 'no_hmac_param'

    # The 20 fields in alphabetical order per Paymob's spec
    obj = body_data.get('obj', {}) if isinstance(body_data.get('obj'), dict) else {}
    if obj:
        source_data = obj.get('source_data', {}) if isinstance(obj.get('source_data'), dict) else {}
        order_obj = obj.get('order', {}) if isinstance(obj.get('order'), dict) else {}
        fields = [
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
    else:
        d = body_data
        fields = [
            str(d.get('amount_cents', '')),
            str(d.get('created_at', '')),
            str(d.get('currency', '')),
            str(d.get('error_occured', '')),
            str(d.get('has_parent_transaction', '')),
            str(d.get('id', '')),
            str(d.get('integration_id', '')),
            str(d.get('is_3d_secure', '')),
            str(d.get('is_auth', '')),
            str(d.get('is_capture', '')),
            str(d.get('is_refunded', '')),
            str(d.get('is_standalone_payment', '')),
            str(d.get('is_voided', '')),
            str(d.get('order', '')),
            str(d.get('owner', '')),
            str(d.get('pending', '')),
            str(d.get('source_data.pan', d.get('source_data_pan', ''))),
            str(d.get('source_data.sub_type', d.get('source_data_sub_type', ''))),
            str(d.get('source_data.type', d.get('source_data_type', ''))),
            str(d.get('success', '')),
        ]
    computed = hmac.new(
        secret.encode('utf-8'),
        ''.join(fields).encode('utf-8'),
        hashlib.sha512,
    ).hexdigest()
    if hmac.compare_digest(computed, received):
        return True, 'ok'
    logger.critical(
        "🚨 [PAYMOB-PARTS] HMAC MISMATCH — IP=%s, possible forgery",
        request.META.get('REMOTE_ADDR', '?'),
    )
    return False, 'mismatch'


@csrf_exempt
def parts_paymob_callback(request):
    """
    Paymob server-to-server callback. On a successful txn → flip the order
    to paid_held (escrow). Verifies HMAC-SHA512 signature.
    """
    if request.method not in ('GET', 'POST'):
        return JsonResponse({'error': 'method not allowed'}, status=405)

    # Build a unified data dict from POST body (JSON or form) or GET query
    if request.method == 'POST':
        if request.body:
            try:
                data = json.loads(request.body)
            except Exception:
                data = request.POST.dict()
        else:
            data = request.POST.dict()
    else:
        data = request.GET.dict()

    # 🛡️ HMAC verification — rejected callbacks return 403 without side effects
    ok, reason = _verify_paymob_hmac(request, data)
    if not ok:
        return JsonResponse({'ok': False, 'error': 'hmac_failed', 'reason': reason}, status=403)

    obj = data.get('obj') or {}
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except Exception:
            obj = {}
    success = (data.get('success') or (obj or {}).get('success'))
    success = str(success).lower() == 'true'
    paymob_order_id = str(
        data.get('order') or (obj or {}).get('order', {}).get('id') or ''
    )
    paymob_txn_id = str(data.get('id') or (obj or {}).get('id') or '')

    if not paymob_order_id:
        return JsonResponse({'ok': False, 'error': 'no order id'}, status=400)

    try:
        order = PartOrder.objects.select_related('listing').get(paymob_order_id=paymob_order_id)
    except PartOrder.DoesNotExist:
        # Try cache fallback
        order_code = cache.get(f'paymob_part_order_{paymob_order_id}')
        if not order_code:
            return JsonResponse({'ok': False, 'error': 'order not found'}, status=404)
        order = PartOrder.objects.select_related('listing').get(order_code=order_code)

    if success and order.status == 'pending_payment':
        with transaction.atomic():
            order.status = 'paid_held'
            order.paid_at = timezone.now()
            order.paymob_txn_id = paymob_txn_id
            order.save(update_fields=['status', 'paid_at', 'paymob_txn_id'])
            # Mark listing as sold
            PartListing.objects.filter(pk=order.listing_id).update(
                status='sold', sold_at=timezone.now(),
            )
            # Notify seller
            if order.listing.seller_customer_id:
                CustomerNotification.objects.create(
                    customer=order.listing.seller_customer,
                    title=f'🎉 تم بيع «{order.listing.title}»',
                    body=(
                        f'المشتري دفع {order.amount_paid} ج.م — الفلوس في الـ Escrow. '
                        f'جهّز القطعة وابعتها للعميل. هتستلم {order.seller_payout} ج.م '
                        f'بعد {order.warranty_days} يوم من تأكيد التسليم.'
                    ),
                    level='success', icon='fa-money-check-dollar',
                    action_url='/marketplace/parts/sales/',
                    action_label='تفاصيل البيع',
                )
            # Notify buyer
            CustomerNotification.objects.create(
                customer=order.buyer_customer,
                title='✅ تم استلام دفعتك',
                body=(
                    f'فلوسك آمنة في الـ Escrow. هتتحرر للبائع بعد '
                    f'{order.warranty_days} يوم من استلامك للقطعة. لو فيها مشكلة، '
                    f'تقدر تطلب إرجاع خلال فترة الضمان.'
                ),
                level='success', icon='fa-shield-halved',
                action_url='/marketplace/parts/orders/',
                action_label='طلباتي',
            )
    elif not success and order.status == 'pending_payment':
        order.status = 'cancelled'
        order.save(update_fields=['status'])
        PartListing.objects.filter(pk=order.listing_id, status='reserved').update(status='active')

    return JsonResponse({'ok': True})


# ─────────────────────────────────────────────────────────────────────
# 4. POST-PURCHASE — orders list, confirm delivery, request refund
# ─────────────────────────────────────────────────────────────────────

def parts_my_orders(request):
    """List the current customer's purchases."""
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/login/')
    orders = (
        PartOrder.objects.filter(buyer_customer=customer)
        .select_related('listing', 'listing__car_make')
        .order_by('-created_at')[:50]
    )
    return render(request, 'clients/marketplace/parts_orders.html', {
        'customer': customer, 'orders': orders, 'mode': 'buyer',
    })


def parts_my_sales(request):
    """List the current customer's sales."""
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/login/')
    orders = (
        PartOrder.objects.filter(listing__seller_customer=customer)
        .select_related('listing', 'listing__car_make', 'buyer_customer')
        .order_by('-created_at')[:50]
    )
    return render(request, 'clients/marketplace/parts_orders.html', {
        'customer': customer, 'orders': orders, 'mode': 'seller',
    })


@require_POST
def parts_mark_shipped(request, order_code):
    """Seller marks an order as shipped."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'unauth'}, status=401)
    order = get_object_or_404(PartOrder, order_code=order_code)
    if order.listing.seller_customer_id != customer.pk:
        return JsonResponse({'error': 'not your order'}, status=403)
    if order.status != 'paid_held':
        return JsonResponse({'error': f'الحالة الحالية لا تسمح ({order.get_status_display()}).'}, status=400)
    order.status = 'shipped'
    order.shipped_at = timezone.now()
    order.save(update_fields=['status', 'shipped_at'])
    if order.buyer_customer_id:
        CustomerNotification.objects.create(
            customer=order.buyer_customer,
            title='🚚 شُحنت قطعتك',
            body=f'البائع شحن «{order.listing.title}». أكد الاستلام لما توصل عشان يبدأ ضمان {order.warranty_days} يوم.',
            level='info', icon='fa-truck',
            action_url='/marketplace/parts/orders/', action_label='تفاصيل',
        )
    return JsonResponse({'ok': True, 'message': 'تم تعليم الطلب كمشحون.'})


@require_POST
def parts_confirm_delivery(request, order_code):
    """Buyer confirms receipt → warranty window starts."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'unauth'}, status=401)
    order = get_object_or_404(PartOrder, order_code=order_code)
    if order.buyer_customer_id != customer.pk:
        return JsonResponse({'error': 'not your order'}, status=403)
    if not order.mark_delivered():
        return JsonResponse({'error': 'لا يمكن تأكيد التسليم في هذه الحالة.'}, status=400)
    return JsonResponse({
        'ok': True,
        'message': f'تم التأكيد. فترة الضمان: {order.warranty_days} يوم.',
        'warranty_ends_at': order.warranty_ends_at.isoformat() if order.warranty_ends_at else None,
    })


@require_POST
def parts_request_refund(request, order_code):
    """Buyer requests refund within warranty window."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'unauth'}, status=401)
    order = get_object_or_404(PartOrder, order_code=order_code)
    if order.buyer_customer_id != customer.pk:
        return JsonResponse({'error': 'not your order'}, status=403)
    if order.status != 'delivered':
        return JsonResponse({'error': 'الإرجاع متاح فقط خلال فترة الضمان.'}, status=400)
    if order.warranty_ends_at and timezone.now() > order.warranty_ends_at:
        return JsonResponse({'error': 'انتهت فترة الضمان — لا يمكن الإرجاع.'}, status=400)

    reason = (request.POST.get('reason') or '').strip()
    if len(reason) < 10:
        return JsonResponse({'error': 'اكتب سبب الإرجاع بالتفصيل (10 حروف على الأقل).'}, status=400)

    order.status = 'refund_requested'
    order.refund_reason = reason
    order.save(update_fields=['status', 'refund_reason'])

    if order.listing.seller_customer_id:
        CustomerNotification.objects.create(
            customer=order.listing.seller_customer,
            title='⚠️ طلب إرجاع جديد',
            body=f'المشتري طلب إرجاع «{order.listing.title}». السبب: {reason[:120]}',
            level='warning', icon='fa-rotate-left',
            action_url='/marketplace/parts/sales/', action_label='عرض الطلب',
        )
    PlatformEvent.objects.create(
        event_type='other', tenant_schema='public', tenant_name='parts_market',
        user_name=customer.full_name,
        description=f"⚠️ طلب إرجاع للطلب {order.order_code}: {reason[:80]}",
    )
    return JsonResponse({'ok': True, 'message': 'تم تسجيل طلب الإرجاع. هنتواصل معك قريباً.'})


# ─────────────────────────────────────────────────────────────────────
# 5. BACKGROUND — auto-release escrow after warranty expires
# ─────────────────────────────────────────────────────────────────────

def auto_release_expired_warranties():
    """
    Run periodically (cron / celery beat). Releases escrow for delivered
    orders whose warranty window has passed. Returns count released.
    """
    now = timezone.now()
    qs = PartOrder.objects.filter(status='delivered', warranty_ends_at__lt=now)
    n = 0
    for order in qs.select_related('listing', 'listing__seller_customer')[:200]:
        if order.release_to_seller():
            n += 1
    if n:
        logger.info("[PARTS] Auto-released %d order(s) from escrow", n)
    return n
