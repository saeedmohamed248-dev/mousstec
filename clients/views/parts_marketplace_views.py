"""
🚗 P2P Car-Parts Marketplace Views.

Flow:
  Seller flow:    /marketplace/parts/sell/   → upload photos → publish listing
  Buyer flow:     /marketplace/parts/       → filter by make → detail → checkout (Paymob) → escrow
  Post-purchase:  /marketplace/parts/orders/ → confirm delivery → warranty window → auto-release

  Seller tools:   /marketplace/parts/my-listings/ → track moderation / withdraw a listing
  Wanted flow:    buyer posts /parts/wanted/new/ → sellers answer from
                  /parts/wanted/sellers/ → buyer accepts on /parts/wanted/mine/
                  → a private listing reserved for that buyer → normal checkout

Commission: 8% for individual sellers, 4% for tenant (company) sellers.
Return shipping: never paid by the platform — buyer pays on change-of-mind,
seller pays when the part is defective / wrong / not as described
(see marketplace_b2b.services.escrow.RETURN_REASON_TO_PAYER).
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, F, Q
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
    PartWantedOffer,
    PartWantedRequest,
    PlatformEvent,
)
from clients.views._shared import _marketplace_auth
from marketplace_b2b.services import parts_orders as orders_svc
from erp_core.localization import current_tenant_symbol as _sym

logger = logging.getLogger('mouss_tec_core')

MAX_PHOTO_BYTES = 8 * 1024 * 1024  # 8 MB per listing photo
_ACTIVE_ORDER_STATUSES = ('pending_payment', 'paid_held', 'shipped', 'delivered',
                          'refund_requested', 'disputed')


def _parse_int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_money(value):
    """Decimal > 0 with at most 2 decimals, or None if invalid."""
    from decimal import InvalidOperation
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount <= 0 or amount >= Decimal('10000000000'):
        return None
    return amount.quantize(Decimal('0.01'))


def _validate_photo(photo):
    """Return an error message for an unacceptable upload, else None."""
    if photo.size > MAX_PHOTO_BYTES:
        return 'حجم كل صورة لازم يكون أقل من 8 ميجا.'
    if not (getattr(photo, 'content_type', '') or '').startswith('image/'):
        return 'الملفات المرفوعة لازم تكون صور (JPG / PNG / WEBP).'
    return None


# ─────────────────────────────────────────────────────────────────────
# 1. PUBLIC FEED — anyone can browse, only logged-in customers can buy
# ─────────────────────────────────────────────────────────────────────

def parts_feed(request):
    """Public feed of active part listings, filterable by car make."""
    if getattr(connection, 'schema_name', 'public') != 'public':
        return HttpResponseForbidden('Access from main site only')

    public_q = Q(
        listings__status='active', listings__moderation_status='approved',
        listings__is_deleted=False, listings__reserved_for__isnull=True,
    )
    # 🐛 [FIX]: عدّاد الماركة (listings_count) كان بيزيد مع كل قطعة جديدة حتى
    #    لو لسه مستنية موافقة أو اترفضت، وماكانش بيقل أبداً مع البيع —
    #    دلوقتي بيتحسب لحظياً من القطع المعروضة فعلاً.
    makes = (
        PartCarMake.objects.filter(is_active=True)
        .annotate(live_count=Count('listings', filter=public_q))
        .order_by('sort_order', 'name')
    )
    selected_make_slug = request.GET.get('make', '').strip().lower()
    q = request.GET.get('q', '').strip()[:100]
    condition = request.GET.get('condition', '').strip()
    sort = request.GET.get('sort', 'new')

    listings = PartListing.objects.filter(
        status='active', moderation_status='approved', is_deleted=False,
        reserved_for__isnull=True,  # private (wanted-offer) listings never hit the feed
    ).select_related('car_make').prefetch_related('photos')

    selected_make = None
    if selected_make_slug:
        selected_make = makes.filter(slug=selected_make_slug).first()
        if selected_make:
            listings = listings.filter(car_make=selected_make)
    if q:
        # 🐛 [FIX]: البحث كان على العنوان والوصف بس، رغم إن الخانة بتقول
        #    "اسم قطعة، موديل، رقم OEM".
        listings = listings.filter(
            Q(title__icontains=q) | Q(description__icontains=q)
            | Q(part_number__icontains=q) | Q(car_model__icontains=q)
            | Q(engine_code__icontains=q)
        )
    if condition and condition in dict(PartListing.CONDITION_CHOICES):
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
    customer = _marketplace_auth(request)
    is_owner = bool(customer and listing.seller_customer_id == customer.pk)
    is_buyer = bool(customer and PartOrder.objects.filter(
        listing=listing, buyer_customer=customer,
    ).exists())

    if listing.is_deleted:
        return HttpResponseForbidden('This listing is unavailable.')
    # Sellers may always preview their own listing (draft / pending / rejected /
    # withdrawn) — that's how they check what the admin sees. Buyers keep
    # access to what they bought.
    if not (is_owner or is_buyer):
        if listing.status not in ('active', 'reserved', 'sold'):
            return HttpResponseForbidden('This listing is unavailable.')
        # Hide pending/rejected/suspended listings from the public.
        if listing.moderation_status != 'approved':
            return HttpResponseForbidden('This listing is awaiting admin approval.')
        # A listing created for one buyer's wanted request is private to them.
        if listing.reserved_for_id and not (customer and listing.reserved_for_id == customer.pk):
            return HttpResponseForbidden('This listing is reserved for another buyer.')

    # Count view — atomic F() so concurrent views don't overwrite each other.
    if not is_owner:
        PartListing.objects.filter(pk=listing.pk).update(views_count=F('views_count') + 1)

    # Masked seller view by default. Reveal only if the viewer has an
    # escrow-funded order on this listing OR is the seller themselves.
    from clients.services.trust import contact_view
    reveal_order = None
    if customer and listing.seller_customer_id and not is_owner:
        reveal_order = (
            PartOrder.objects.filter(
                listing=listing,
                buyer_customer=customer,
                status__in=['paid_held', 'shipped', 'delivered', 'released',
                            'refund_requested', 'refunded', 'disputed'],
            )
            .order_by('-created_at').first()
        )
    seller_contact = contact_view(
        listing.seller_customer, viewer=customer, order=reveal_order,
    ) if listing.seller_customer_id else None

    return render(request, 'clients/marketplace/parts_detail.html', {
        'listing': listing,
        'photos': list(listing.photos.all()),
        'customer': customer,
        'is_owner': is_owner,
        'can_buy': listing.can_be_bought_by(customer) if customer else False,
        'seller_contact': seller_contact,
    })


# ─────────────────────────────────────────────────────────────────────
# 2. SELLER FLOW — list a part for sale (customer-side, simplest path)
# ─────────────────────────────────────────────────────────────────────

def create_listing_from_post(request, *, seller_customer=None, seller_tenant=None,
                             default_city='', seller_label=''):
    """
    Validate a "sell a part" form and create the pending listing + photos.
    Shared by customer sellers (/marketplace/parts/sell/) and merchant
    (tenant) sellers (/marketplace/merchant/parts/). Returns (listing, error).
    """
    # ── Validate everything BEFORE touching the DB ────────────────
    # 🐛 [FIX]: أي قيمة غير رقمية (سنة/ضمان/ماركة) كانت بترمي exception
    #    وترجع 500 بنص الخطأ الداخلي للعميل، والحالة ماكانتش بتتحقق.
    make = PartCarMake.objects.filter(
        pk=_parse_int(request.POST.get('car_make'), 0), is_active=True,
    ).first()
    if make is None:
        return None, JsonResponse({'error': 'اختار ماركة العربية.'}, status=400)
    title = (request.POST.get('title') or '').strip()[:200]
    if len(title) < 3:
        return None, JsonResponse({'error': 'اكتب اسم القطعة (3 حروف على الأقل).'}, status=400)
    description = (request.POST.get('description') or '').strip()[:5000]
    if len(description) < 10:
        return None, JsonResponse({'error': 'اكتب وصف للقطعة وحالتها (10 حروف على الأقل).'}, status=400)
    price = _parse_money(request.POST.get('price_egp'))
    if price is None:
        return None, JsonResponse({'error': 'السعر يجب أن يكون أكبر من صفر.'}, status=400)
    warranty = _parse_int(request.POST.get('warranty_days') or 3)
    if warranty is None or warranty < 1 or warranty > 90:
        return None, JsonResponse({'error': 'فترة الضمان لازم بين 1 و 90 يوم.'}, status=400)
    condition = (request.POST.get('condition') or 'used_good').strip()
    if condition not in dict(PartListing.CONDITION_CHOICES):
        return None, JsonResponse({'error': 'حالة القطعة غير صالحة.'}, status=400)

    max_year = timezone.now().year + 1
    year_from = _parse_int(request.POST.get('car_year_from')) if request.POST.get('car_year_from') else None
    year_to = _parse_int(request.POST.get('car_year_to')) if request.POST.get('car_year_to') else None
    for y in (year_from, year_to):
        if y is not None and not (1950 <= y <= max_year):
            return None, JsonResponse({'error': f'سنة الموديل لازم بين 1950 و {max_year}.'}, status=400)
    if (request.POST.get('car_year_from') and year_from is None) or \
            (request.POST.get('car_year_to') and year_to is None):
        return None, JsonResponse({'error': 'سنة الموديل لازم تكون رقم.'}, status=400)
    if year_from and year_to and year_from > year_to:
        year_from, year_to = year_to, year_from

    photos = request.FILES.getlist('photos')
    if len(photos) < 3:
        return None, JsonResponse({
            'error': 'لازم ترفع 3 صور على الأقل لتوثيق حالة القطعة من كل الزوايا.'
        }, status=400)
    photos = photos[:10]  # cap at 10 photos
    for photo in photos:
        err = _validate_photo(photo)
        if err:
            return None, JsonResponse({'error': err}, status=400)

    try:
        with transaction.atomic():
            # Listings stay in `draft` + `pending_approval` until a
            # Super Admin reviews them; approval flips status→active.
            listing = PartListing.objects.create(
                seller_customer=seller_customer,
                seller_tenant=seller_tenant,
                title=title,
                description=description,
                car_make=make,
                car_model=(request.POST.get('car_model') or '').strip()[:100],
                car_year_from=year_from,
                car_year_to=year_to,
                engine_code=(request.POST.get('engine_code') or '').strip().upper()[:30],
                part_number=(request.POST.get('part_number') or '').strip()[:120],
                condition=condition,
                price_egp=price,
                warranty_days=warranty,
                city=(request.POST.get('city') or default_city or '').strip()[:100],
                status='draft',
                moderation_status='pending_approval',
            )
            for idx, photo in enumerate(photos):
                PartListingPhoto.objects.create(
                    listing=listing, image=photo,
                    is_primary=(idx == 0), sort_order=idx,
                )

            PlatformEvent.objects.create(
                event_type='other', tenant_schema='public', tenant_name='parts_market',
                user_name=seller_label,
                description=f"🛒 قطعة جديدة معروضة: «{listing.title}» — {make.name} — {price} {_sym()}",
            )
    except Exception as exc:
        logger.exception("[PARTS] Failed to create listing: %s", exc)
        return None, JsonResponse({'error': 'فشل النشر. حاول مرة أخرى.'}, status=500)
    return listing, None


def parts_create(request):
    """Form to create a new listing as a marketplace customer."""
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/login/')
    if customer.sector != 'automotive':
        return render(request, 'clients/marketplace/parts_unavailable.html', {
            'reason': 'سوق قطع الغيار خاص بقطاع السيارات فقط.',
        }, status=403)

    makes = PartCarMake.objects.filter(is_active=True).order_by('sort_order', 'name')

    if request.method == 'POST':
        listing, err = create_listing_from_post(
            request, seller_customer=customer,
            default_city=customer.city or '', seller_label=customer.full_name,
        )
        if err:
            return err
        return JsonResponse({
            'ok': True,
            'message': 'تم استلام القطعة وهي الآن في انتظار موافقة الإدارة قبل النشر.',
            'listing_code': str(listing.listing_code),
            'detail_url': f'/marketplace/parts/{listing.listing_code}/',
            'my_listings_url': '/marketplace/parts/my-listings/',
            'moderation_status': 'pending_approval',
        })

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


def _checkout_shipping(request, customer):
    """Validate the shipping block of a checkout form → (dict, error_response)."""
    shipping = {
        'shipping_name': (request.POST.get('shipping_name') or customer.full_name or '').strip()[:120],
        'shipping_phone': (request.POST.get('shipping_phone') or customer.phone or '').strip()[:30],
        'shipping_address': (request.POST.get('shipping_address') or '').strip()[:1000],
        'shipping_city': (request.POST.get('shipping_city') or customer.city or '').strip()[:80],
    }
    if len(shipping['shipping_address']) < 10:
        return None, JsonResponse({'error': 'لازم تكتب العنوان كاملاً.'}, status=400)
    if len(''.join(c for c in shipping['shipping_phone'] if c.isdigit())) < 10:
        return None, JsonResponse({'error': 'رقم الموبايل للاستلام غير صحيح.'}, status=400)
    if not shipping['shipping_name']:
        return None, JsonResponse({'error': 'اسم المستلم مطلوب.'}, status=400)
    return shipping, None


def reserve_listing_for_checkout(listing, customer, shipping):
    """
    Lock the listing, reserve it and create the pending PartOrder.
    Shared by the Paymob checkout and the Vodafone-Cash manual checkout.
    Returns (order, None) or (None, JsonResponse error).

    🐛 [FIX]: الشراء كان بيتحقق من status='active' بس — قطعة الإدارة
    علّقتها (suspended) أو محذوفة أو محجوزة لمشترٍ تاني كانت قابلة للشراء
    بالرابط المباشر.
    """
    with transaction.atomic():
        # Lock the listing row to prevent double-buy
        listing = PartListing.objects.select_for_update().get(pk=listing.pk)
        if listing.seller_customer_id == customer.pk:
            return None, JsonResponse({'error': 'لا يمكنك شراء قطعتك الخاصة.'}, status=400)
        if listing.status == 'reserved':
            return None, JsonResponse({'error': 'القطعة محجوزة الآن لمشترٍ آخر.'}, status=400)
        if not listing.can_be_bought_by(customer):
            return None, JsonResponse({'error': 'القطعة لم تعد متاحة.'}, status=400)
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
            **shipping,
        )
    return order, None


@require_POST
def parts_checkout(request, listing_code):
    """Initiate Paymob payment for a listing. Reserves the listing + creates a PartOrder."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'سجل دخول أولاً.'}, status=401)

    listing = get_object_or_404(PartListing, listing_code=listing_code)
    shipping, err = _checkout_shipping(request, customer)
    if err:
        return err
    order, err = reserve_listing_for_checkout(listing, customer, shipping)
    if err:
        return err
    shipping_name = shipping['shipping_name']
    shipping_phone = shipping['shipping_phone']
    shipping_address = shipping['shipping_address']
    shipping_city = shipping['shipping_city']

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
        orders_svc.cancel_unpaid(order, reason='تعذر بدء الدفع الإلكتروني', notify_buyer=False)
        return JsonResponse({'error': err}, status=502)

    PartOrder.objects.filter(pk=order.pk).update(paymob_order_id=paymob_order_id)
    cache.set(f'paymob_part_order_{paymob_order_id}', str(order.order_code), timeout=7200)

    return JsonResponse({'ok': True, 'iframe_url': iframe_url, 'order_code': str(order.order_code)})


# NOTE: HMAC verification used to live here. It is now consolidated in
# clients.services.paymob.verify_paymob_hmac — same fail-closed contract,
# but shared by every Paymob callback view in the project. Use that helper.


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

    # 🛡️ HMAC verification — rejected callbacks return 403 without side effects.
    # Centralized verifier (clients.services.paymob.verify_paymob_hmac) is the
    # single source of truth for HMAC behavior across all Paymob callbacks.
    from clients.services.paymob import verify_paymob_hmac
    ok, reason = verify_paymob_hmac(request, body_data=data)
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

    order = PartOrder.objects.select_related('listing').filter(paymob_order_id=paymob_order_id).first()
    if order is None:
        # Cache fallback (the order-id update may have raced the callback).
        # 🐛 [FIX]: كان بيعمل .get() من غير حماية → 500 لو الطلب مش موجود.
        order_code = cache.get(f'paymob_part_order_{paymob_order_id}')
        if order_code:
            order = PartOrder.objects.select_related('listing').filter(order_code=order_code).first()
    if order is None:
        return JsonResponse({'ok': False, 'error': 'order not found'}, status=404)

    if success:
        # 🛡️ [FIX]: المبلغ المدفوع لازم يطابق سعر الطلب المجمّد — HMAC بيضمن
        #    إن البيانات من Paymob، لكن مش إن المبلغ هو المطلوب.
        amount_cents = data.get('amount_cents') or (obj or {}).get('amount_cents')
        expected_cents = int((order.amount_paid * 100).to_integral_value())
        if amount_cents not in (None, '') and _parse_int(amount_cents, -1) != expected_cents:
            logger.error(
                "[PARTS] Paymob amount mismatch for order %s: got %s expected %s",
                order.order_code, amount_cents, expected_cents,
            )
            return JsonResponse({'ok': False, 'error': 'amount mismatch'}, status=400)
        if order.status == 'pending_payment':
            orders_svc.mark_paid(order, txn_id=paymob_txn_id)
        elif order.status == 'cancelled':
            # Paid after our checkout window closed — money is real, so flag
            # it for a manual refund / re-activation instead of dropping it.
            logger.error("[PARTS] Paymob success on CANCELLED order %s (txn %s) — needs admin action",
                         order.order_code, paymob_txn_id)
            PlatformEvent.objects.create(
                event_type='other', tenant_schema='public', tenant_name='parts_market',
                description=(f"⚠️ دفع Paymob ناجح على طلب قطع ملغي {order.order_code} "
                             f"(txn {paymob_txn_id}) — محتاج مراجعة/استرداد يدوي"),
            )
    elif order.status == 'pending_payment':
        orders_svc.cancel_unpaid(order, reason='فشل الدفع الإلكتروني')

    # The GET variant is Paymob's browser redirect — send the buyer to their
    # orders page instead of showing raw JSON.
    if request.method == 'GET':
        return redirect('/marketplace/parts/orders/?paid=1' if success else '/marketplace/parts/orders/?paid=0')
    return JsonResponse({'ok': True})


# ─────────────────────────────────────────────────────────────────────
# 4. POST-PURCHASE — orders list, confirm delivery, request refund
# ─────────────────────────────────────────────────────────────────────

def parts_my_orders(request):
    """List the current customer's purchases."""
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/login/')
    orders = list(
        PartOrder.objects.filter(buyer_customer=customer)
        .select_related('listing', 'listing__car_make', 'listing__seller_customer')
        .order_by('-created_at')[:50]
    )
    _decorate_orders(orders, customer, mode='buyer')
    return render(request, 'clients/marketplace/parts_orders.html', {
        'customer': customer, 'orders': orders, 'mode': 'buyer',
        'paid_flag': request.GET.get('paid', ''),
        **_orders_page_choices(),
    })


def parts_my_sales(request):
    """List the current customer's sales."""
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/login/')
    # Unpaid / cancelled checkouts are noise for the seller — they only need
    # orders that were actually paid.
    orders = list(
        PartOrder.objects.filter(listing__seller_customer=customer)
        .exclude(status__in=('pending_payment', 'cancelled'))
        .select_related('listing', 'listing__car_make', 'buyer_customer')
        .order_by('-created_at')[:50]
    )
    _decorate_orders(orders, customer, mode='seller')
    return render(request, 'clients/marketplace/parts_orders.html', {
        'customer': customer, 'orders': orders, 'mode': 'seller',
        **_orders_page_choices(),
    })


def _orders_page_choices():
    from clients.models import DisputeTicket
    buyer_categories = ('item_not_received', 'item_not_as_described', 'damaged_on_arrival',
                        'wrong_item', 'counterfeit', 'payment_issue', 'other')
    seller_categories = ('buyer_misuse', 'payment_issue', 'other')
    cats = dict(DisputeTicket.CATEGORY_CHOICES)
    return {
        'return_reason_choices': PartOrder.RETURN_REASON_CHOICES,
        'buyer_dispute_choices': [(c, cats[c]) for c in buyer_categories],
        'seller_dispute_choices': [(c, cats[c]) for c in seller_categories],
    }


def _decorate_orders(orders, customer, *, mode):
    """
    Attach per-order UI flags so the template stays dumb:
      * can_dispute      — inside the dispute window and no open ticket yet
      * open_dispute     — the open ticket (if any)
      * receipt_url      — Vodafone-Cash upload page for an unpaid manual order
      * show_shipping    — seller may see the buyer's address once paid
    🐛 [FIX]: البائع ماكانش بيشوف عنوان/تليفون المشتري خالص — مكانش يعرف
       يشحن على فين. والمشتري ماكانش عنده أي زرار لفتح نزاع رغم إن الـ API موجود.
    """
    from clients.models import DisputeTicket, ManualPaymentReceipt
    if not orders:
        return
    ids = [o.pk for o in orders]
    open_tickets = {
        t.order_id: t for t in DisputeTicket.objects.filter(
            order_id__in=ids, status__in=('open', 'under_review'), is_deleted=False,
        )
    }
    receipts = {}
    if mode == 'buyer':
        for r in ManualPaymentReceipt.objects.filter(
            purchase_type='parts', purchase_id__in=ids, status='pending',
        ).order_by('created_at'):
            receipts[r.purchase_id] = r
    for o in orders:
        o.open_dispute = open_tickets.get(o.pk)
        o.can_dispute = (o.open_dispute is None and DisputeTicket.is_within_window(o))
        o.show_shipping = mode == 'seller' and o.status not in ('pending_payment', 'cancelled')
        r = receipts.get(o.pk)
        o.receipt_url = (
            reverse('manual_payment_upload', args=[r.receipt_code])
            if (r and o.status == 'pending_payment') else ''
        )
        o.receipt_uploaded = bool(r and r.txn_reference)


@require_POST
def parts_mark_shipped(request, order_code):
    """Seller marks an order as shipped."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'unauth'}, status=401)
    order = get_object_or_404(PartOrder, order_code=order_code)
    if order.listing.seller_customer_id != customer.pk:
        return JsonResponse({'error': 'not your order'}, status=403)
    tracking = (request.POST.get('tracking') or '').strip()[:200]
    with transaction.atomic():
        # Lock + conditional update so a double click / concurrent dispute
        # can't overwrite a newer status.
        updated = PartOrder.objects.filter(pk=order.pk, status='paid_held').update(
            status='shipped', shipped_at=timezone.now(), shipping_tracking=tracking,
        )
    if not updated:
        order.refresh_from_db(fields=['status'])
        return JsonResponse({'error': f'الحالة الحالية لا تسمح ({order.get_status_display()}).'}, status=400)
    if order.buyer_customer_id:
        CustomerNotification.objects.create(
            customer=order.buyer_customer,
            title='🚚 شُحنت قطعتك',
            body=(f'البائع شحن «{order.listing.title}».'
                  + (f' بيانات الشحن: {tracking}.' if tracking else '')
                  + f' أكد الاستلام لما توصل عشان يبدأ ضمان {order.warranty_days} يوم.'),
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
    with transaction.atomic():
        order = PartOrder.objects.select_for_update().select_related('listing').get(pk=order.pk)
        if not order.mark_delivered():
            return JsonResponse({'error': 'لا يمكن تأكيد التسليم في هذه الحالة.'}, status=400)
    if order.listing.seller_customer_id:
        CustomerNotification.objects.create(
            customer=order.listing.seller_customer,
            title='📦 المشتري استلم القطعة',
            body=(f'تم تأكيد استلام «{order.listing.title}». المبلغ هيتحوّل لك تلقائياً '
                  f'بعد انتهاء الضمان ({order.warranty_days} يوم) لو مفيش مشكلة.'),
            level='info', icon='fa-box-open',
            action_url='/marketplace/parts/sales/', action_label='مبيعاتي',
        )
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

    reason = (request.POST.get('reason') or '').strip()[:2000]
    if len(reason) < 10:
        return JsonResponse({'error': 'اكتب سبب الإرجاع بالتفصيل (10 حروف على الأقل).'}, status=400)
    # The reason category decides who pays return shipping (never the
    # platform) — recorded now, applied when the admin approves.
    return_reason = (request.POST.get('return_reason') or 'defective').strip()
    if return_reason not in dict(PartOrder.RETURN_REASON_CHOICES):
        return JsonResponse({'error': 'اختار نوع مشكلة الإرجاع.'}, status=400)

    updated = PartOrder.objects.filter(pk=order.pk, status='delivered').update(
        status='refund_requested', refund_reason=reason, return_reason=return_reason,
    )
    if not updated:
        return JsonResponse({'error': 'الإرجاع متاح فقط خلال فترة الضمان.'}, status=400)

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


@require_POST
def parts_cancel_order(request, order_code):
    """Buyer abandons an unpaid checkout → the listing goes back on sale."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'unauth'}, status=401)
    order = get_object_or_404(PartOrder, order_code=order_code)
    if order.buyer_customer_id != customer.pk:
        return JsonResponse({'error': 'not your order'}, status=403)
    from clients.models import ManualPaymentReceipt
    if ManualPaymentReceipt.objects.filter(
        purchase_type='parts', purchase_id=order.pk, status='pending',
    ).exclude(txn_reference='').exists():
        return JsonResponse({
            'error': 'رفعت إيصال تحويل وهو قيد المراجعة — تواصل مع الدعم لو عايز تلغي.',
        }, status=400)
    if not orders_svc.cancel_unpaid(order, reason='ألغاه المشتري', notify_buyer=False):
        return JsonResponse({'error': 'الطلب ده مش في انتظار الدفع — مينفعش يتلغي.'}, status=400)
    ManualPaymentReceipt.objects.filter(
        purchase_type='parts', purchase_id=order.pk, status='pending',
    ).update(status='rejected', reviewed_at=timezone.now(), review_notes='ألغى المشتري الطلب')
    return JsonResponse({'ok': True, 'message': 'تم إلغاء الطلب.'})


# ─────────────────────────────────────────────────────────────────────
# 4a. SELLER TOOLS — my listings + withdraw
# ─────────────────────────────────────────────────────────────────────

def parts_my_listings(request):
    """
    Seller's own listings with their moderation state.
    🐛 [FIX]: البائع بعد ما يعرض قطعة ماكانش ليه أي صفحة يشوف فيها هل
       اتقبلت ولا اترفضت (وسبب الرفض)، ولا يقدر يسحب قطعة اتباعت برّه المنصة.
    """
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/login/?next=/marketplace/parts/my-listings/')
    listings = (
        PartListing.objects.filter(seller_customer=customer, is_deleted=False)
        .select_related('car_make', 'reserved_for')
        .prefetch_related('photos')
        .order_by('-created_at')[:100]
    )
    return render(request, 'clients/marketplace/parts_my_listings.html', {
        'customer': customer,
        'listings': listings,
    })


@require_POST
def parts_listing_withdraw(request, listing_code):
    """Seller withdraws an unsold listing (sold elsewhere / changed mind)."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'unauth'}, status=401)
    with transaction.atomic():
        listing = get_object_or_404(
            PartListing.objects.select_for_update(),
            listing_code=listing_code, is_deleted=False,
        )
        if listing.seller_customer_id != customer.pk:
            return JsonResponse({'error': 'not your listing'}, status=403)
        if listing.status not in ('draft', 'active'):
            return JsonResponse({
                'error': 'مينفعش تسحب قطعة محجوزة أو اتباعت — فيه طلب شراء عليها.',
            }, status=400)
        listing.status = 'removed'
        listing.save(update_fields=['status', 'updated_at'])
        # A private wanted-offer listing: the buyer's accepted offer is void.
        if listing.reserved_for_id:
            PartWantedOffer.objects.filter(linked_listing=listing, status='accepted').update(status='withdrawn')
            PartWantedRequest.objects.filter(
                offers__linked_listing=listing, status='matched',
            ).update(status='open')
    return JsonResponse({'ok': True, 'message': 'تم سحب القطعة من السوق.'})


# ─────────────────────────────────────────────────────────────────────
# 4b. WANTED REQUESTS — buyer posts what they need, sellers filter by fitment
# ─────────────────────────────────────────────────────────────────────

def parts_wanted_create(request):
    """Buyer posts a 'Part Wanted' request."""
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/login/?next=/marketplace/parts/wanted/new/')
    if customer.sector != 'automotive':
        return JsonResponse({'error': 'هذا الطلب مخصص لقطاع السيارات.'}, status=403)

    if request.method != 'POST':
        return render(request, 'clients/marketplace/parts_wanted_create.html', {
            'customer': customer,
            'makes': PartCarMake.objects.filter(is_active=True).order_by('sort_order', 'name'),
        })

    # 🐛 [FIX]: ميزانية مش رقم أو سنة فاضية كانت بترجع 500 بنص الخطأ الداخلي.
    make = PartCarMake.objects.filter(
        pk=_parse_int(request.POST.get('car_make'), 0), is_active=True,
    ).first()
    if make is None:
        return JsonResponse({'error': 'اختار ماركة العربية.'}, status=400)
    year = _parse_int(request.POST.get('car_year'), 0)
    if year < 1950 or year > timezone.now().year + 1:
        return JsonResponse({'error': 'سنة الصنع غير منطقية.'}, status=400)
    model = (request.POST.get('car_model') or '').strip()
    if not model:
        return JsonResponse({'error': 'الموديل مطلوب — مثال: F30, X5, Civic.'}, status=400)
    name = (request.POST.get('part_name') or '').strip()
    if len(name) < 2:
        return JsonResponse({'error': 'اسم القطعة مطلوب.'}, status=400)
    budget_raw = (request.POST.get('max_budget_egp') or '').strip()
    budget = None
    if budget_raw:
        budget = _parse_money(budget_raw)
        if budget is None:
            return JsonResponse({'error': 'الميزانية لازم تكون رقم أكبر من صفر.'}, status=400)
    if PartWantedRequest.objects.filter(
        buyer_customer=customer, status='open', is_deleted=False,
    ).count() >= 20:
        return JsonResponse({'error': 'عندك 20 طلب مفتوح — اقفل طلبات قديمة الأول.'}, status=429)

    try:
        req = PartWantedRequest.objects.create(
            buyer_customer=customer,
            car_make=make,
            car_model=model[:100],
            car_year=year,
            engine_code=(request.POST.get('engine_code') or '').strip()[:30],
            part_name=name[:200],
            part_number_oem=(request.POST.get('part_number_oem') or '').strip()[:120],
            description=(request.POST.get('description') or '').strip()[:3000],
            max_budget_egp=budget,
        )
    except Exception as exc:
        logger.exception("[WANTED] Failed to create request: %s", exc)
        return JsonResponse({'error': 'فشل النشر. حاول مرة أخرى.'}, status=500)
    return JsonResponse({
        'ok': True,
        'message': 'تم نشر الطلب. هتوصلك عروض البائعين قريباً.',
        'request_code': str(req.request_code),
        'redirect': '/marketplace/parts/wanted/mine/',
    })


def parts_wanted_seller_feed(request):
    """
    Seller-facing feed of open wanted requests, filterable by make/model/year/engine.
    """
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/login/?next=/marketplace/parts/wanted/sellers/')
    if customer.sector != 'automotive':
        return JsonResponse({'error': 'هذا السوق مخصص لقطاع السيارات.'}, status=403)

    from clients.services.fitment import open_wanted_requests

    make_slug = (request.GET.get('make') or '').strip().lower()
    model     = (request.GET.get('model') or '').strip()
    year_str  = (request.GET.get('year') or '').strip()
    engine    = (request.GET.get('engine_code') or '').strip()

    make = PartCarMake.objects.filter(slug=make_slug, is_active=True).first() if make_slug else None
    year = _parse_int(year_str) if year_str else None

    requests_qs = list(
        open_wanted_requests(
            make=make, model=model or None, year=year, engine_code=engine or None,
        ).exclude(buyer_customer=customer)  # never offer on your own request
        .annotate(offers_n=Count('offers', filter=~Q(offers__status='withdrawn')))
        .select_related('car_make')[:100]
    )
    my_offers = {
        o.request_id: o for o in PartWantedOffer.objects.filter(
            seller_customer=customer, request_id__in=[r.pk for r in requests_qs],
        )
    }
    for r in requests_qs:
        r.my_offer = my_offers.get(r.pk)

    return render(request, 'clients/marketplace/parts_wanted_seller_feed.html', {
        'customer': customer,
        'requests': requests_qs,
        'makes': PartCarMake.objects.filter(is_active=True).order_by('sort_order', 'name'),
        'condition_choices': PartListing.CONDITION_CHOICES,
        'filters': {
            'make_slug': make_slug, 'model': model,
            'year': year_str, 'engine_code': engine,
        },
    })


@require_POST
def parts_wanted_offer_submit(request, request_code):
    """
    Seller answers a wanted request with a price.
    🐛 [FIX]: الموديل PartWantedOffer كان موجود لكن مفيش أي طريقة للبائع
       يقدّم عرض — دورة "اطلب قطعة" كانت بتقف عند نشر الطلب.
    """
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'سجل دخول أولاً.'}, status=401)
    if customer.sector != 'automotive':
        return JsonResponse({'error': 'هذا السوق مخصص لقطاع السيارات.'}, status=403)
    req = get_object_or_404(PartWantedRequest, request_code=request_code, is_deleted=False)
    if not req.is_visible_to_sellers:
        return JsonResponse({'error': 'الطلب ده مبقاش متاح.'}, status=400)
    if req.buyer_customer_id == customer.pk:
        return JsonResponse({'error': 'مينفعش تقدّم عرض على طلبك.'}, status=400)

    price = _parse_money(request.POST.get('price_egp'))
    if price is None:
        return JsonResponse({'error': 'السعر لازم يكون أكبر من صفر.'}, status=400)
    warranty = _parse_int(request.POST.get('warranty_days') or 3)
    if warranty is None or warranty < 1 or warranty > 90:
        return JsonResponse({'error': 'فترة الضمان لازم بين 1 و 90 يوم.'}, status=400)
    condition = (request.POST.get('condition') or 'used_good').strip()
    if condition not in dict(PartListing.CONDITION_CHOICES):
        return JsonResponse({'error': 'حالة القطعة غير صالحة.'}, status=400)
    notes = (request.POST.get('notes') or '').strip()[:500]

    try:
        with transaction.atomic():
            offer, created = PartWantedOffer.objects.get_or_create(
                request=req, seller_customer=customer,
                defaults={'price_egp': price, 'condition': condition,
                          'warranty_days': warranty, 'notes': notes},
            )
            if not created:
                if offer.status not in ('pending', 'withdrawn'):
                    return JsonResponse({'error': 'عرضك على الطلب ده اتقفل بالفعل.'}, status=400)
                created = offer.status == 'withdrawn'  # re-offer counts as new for the buyer
                offer.price_egp = price
                offer.condition = condition
                offer.warranty_days = warranty
                offer.notes = notes
                offer.status = 'pending'
                offer.save(update_fields=['price_egp', 'condition', 'warranty_days', 'notes', 'status'])
    except IntegrityError:
        return JsonResponse({'error': 'قدّمت عرض على الطلب ده بالفعل.'}, status=400)

    if created and req.buyer_customer_id:
        CustomerNotification.objects.create(
            customer=req.buyer_customer,
            title=f'💬 عرض جديد على «{req.part_name[:60]}»',
            body=f'بائع عرض {price} {_sym()} — {dict(PartListing.CONDITION_CHOICES)[condition]}، ضمان {warranty} يوم.',
            level='info', icon='fa-tags',
            action_url='/marketplace/parts/wanted/mine/', action_label='شوف العروض',
        )
    return JsonResponse({
        'ok': True,
        'message': 'تم إرسال عرضك للمشتري.' if created else 'تم تحديث عرضك.',
    })


@require_POST
def parts_wanted_offer_withdraw(request, offer_id):
    """Seller withdraws a still-pending offer."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'unauth'}, status=401)
    updated = PartWantedOffer.objects.filter(
        pk=offer_id, seller_customer=customer, status='pending',
    ).update(status='withdrawn')
    if not updated:
        return JsonResponse({'error': 'العرض مش موجود أو اتقفل.'}, status=400)
    return JsonResponse({'ok': True, 'message': 'تم سحب العرض.'})


def parts_wanted_my_requests(request):
    """Buyer's wanted requests with every offer received."""
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/login/?next=/marketplace/parts/wanted/mine/')
    reqs = list(
        PartWantedRequest.objects.filter(buyer_customer=customer, is_deleted=False)
        .select_related('car_make')
        .prefetch_related('offers', 'offers__seller_customer', 'offers__linked_listing')
        .order_by('-created_at')[:50]
    )
    for r in reqs:
        r.visible_offers = [o for o in r.offers.all() if o.status != 'withdrawn']
        r.accepted_offer = next((o for o in r.visible_offers if o.status == 'accepted'), None)
    return render(request, 'clients/marketplace/parts_wanted_mine.html', {
        'customer': customer,
        'wanted_requests': reqs,
    })


@require_POST
def parts_wanted_offer_accept(request, offer_id):
    """
    Buyer accepts one offer → we create a *private* listing from it (visible
    and purchasable only by this buyer), reject the competing offers and send
    the buyer to the normal escrow checkout.
    """
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'unauth'}, status=401)

    with transaction.atomic():
        offer = get_object_or_404(
            PartWantedOffer.objects.select_for_update().select_related('request', 'request__car_make'),
            pk=offer_id,
        )
        req = PartWantedRequest.objects.select_for_update().get(pk=offer.request_id)
        if req.buyer_customer_id != customer.pk:
            return JsonResponse({'error': 'not your request'}, status=403)
        if req.status != 'open' or req.is_deleted:
            return JsonResponse({'error': 'الطلب ده مش مفتوح.'}, status=400)
        if offer.status != 'pending':
            return JsonResponse({'error': 'العرض ده مبقاش متاح.'}, status=400)
        if not offer.seller_customer_id:
            return JsonResponse({'error': 'بائع العرض غير صالح.'}, status=400)

        listing = PartListing.objects.create(
            seller_customer_id=offer.seller_customer_id,
            title=req.part_name[:200],
            description=(
                (offer.notes or '').strip()
                or f'عرض على طلبك: {req.part_name} — {req.car_make.name} {req.car_model} {req.car_year}'
            ),
            car_make=req.car_make,
            car_model=req.car_model,
            car_year_from=req.car_year,
            car_year_to=req.car_year,
            engine_code=req.engine_code,
            part_number=req.part_number_oem,
            condition=offer.condition,
            price_egp=offer.price_egp,
            warranty_days=offer.warranty_days,
            status='active',
            # The buyer approved this exact offer for themselves; the listing
            # is private (reserved_for) so it never reaches the public feed.
            moderation_status='approved',
            moderated_at=timezone.now(),
            reserved_for=customer,
        )
        offer.status = 'accepted'
        offer.linked_listing = listing
        offer.save(update_fields=['status', 'linked_listing'])
        PartWantedOffer.objects.filter(request=req, status='pending').exclude(pk=offer.pk).update(status='rejected')
        req.status = 'matched'
        req.save(update_fields=['status'])

    CustomerNotification.objects.create(
        customer_id=offer.seller_customer_id,
        title=f'🤝 المشتري قبل عرضك على «{req.part_name[:60]}»',
        body=(f'اتعملت قطعة خاصة بالمشتري بسعر {offer.price_egp} {_sym()}. '
              f'أول ما يدفع (الفلوس في الـ Escrow) هيوصلك إشعار بعنوان الشحن.'),
        level='success', icon='fa-handshake',
        action_url='/marketplace/parts/my-listings/', action_label='قطعي',
    )
    return JsonResponse({
        'ok': True,
        'message': 'تم قبول العرض — كمّل الدفع عشان البائع يشحن.',
        'redirect': f'/marketplace/parts/{listing.listing_code}/',
    })


@require_POST
def parts_wanted_cancel(request, request_code):
    """Buyer closes their own wanted request."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'unauth'}, status=401)
    with transaction.atomic():
        req = get_object_or_404(
            PartWantedRequest.objects.select_for_update(),
            request_code=request_code, buyer_customer=customer, is_deleted=False,
        )
        if req.status not in ('open', 'matched'):
            return JsonResponse({'error': 'الطلب ده مقفول بالفعل.'}, status=400)
        accepted = req.offers.filter(status='accepted').select_related('linked_listing').first()
        if accepted and accepted.linked_listing_id:
            if PartOrder.objects.filter(
                listing_id=accepted.linked_listing_id,
                status__in=[st for st in _ACTIVE_ORDER_STATUSES if st != 'pending_payment'],
            ).exists():
                return JsonResponse({'error': 'دفعت بالفعل على العرض ده — تابعه من طلباتي.'}, status=400)
            pending = PartOrder.objects.filter(
                listing_id=accepted.linked_listing_id, status='pending_payment',
            )
            for o in pending:
                orders_svc.cancel_unpaid(o, reason='ألغى المشتري طلب القطعة', notify_buyer=False)
            PartListing.objects.filter(
                pk=accepted.linked_listing_id, status__in=('active', 'reserved'),
            ).update(status='removed')
        req.status = 'cancelled'
        req.save(update_fields=['status'])
        req.offers.filter(status__in=('pending', 'accepted')).update(status='rejected')
    return JsonResponse({'ok': True, 'message': 'تم إلغاء الطلب.'})


# ─────────────────────────────────────────────────────────────────────
# 4c. DISPUTES — buyer or seller opens a ticket within the 3-day window
# ─────────────────────────────────────────────────────────────────────

def parts_open_dispute(request, order_code):
    """Buyer or seller opens a dispute on their own order."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'يجب تسجيل الدخول.'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    from clients.services.disputes import open_dispute
    from django.core.exceptions import ValidationError as DjVE, PermissionDenied

    order = get_object_or_404(PartOrder, order_code=order_code)
    # Decide role automatically based on which side this customer is on.
    if order.buyer_customer_id == customer.pk:
        role = 'buyer'
    elif order.listing.seller_customer_id == customer.pk:
        role = 'seller'
    else:
        return JsonResponse({'error': 'هذا الطلب ليس لك.'}, status=403)

    evidence = request.FILES.getlist('evidence')[:5]
    for photo in evidence:
        err = _validate_photo(photo)
        if err:
            return JsonResponse({'error': err}, status=400)

    try:
        ticket = open_dispute(
            order=order, opener=customer, opener_role=role,
            category=(request.POST.get('category') or '').strip(),
            description=(request.POST.get('description') or '').strip()[:5000],
        )
    except PermissionDenied as exc:
        return JsonResponse({'error': str(exc)}, status=403)
    except DjVE as exc:
        return JsonResponse({'error': '; '.join(exc.messages) if hasattr(exc, 'messages') else str(exc)}, status=400)

    from clients.models import DisputeEvidence
    for photo in evidence:
        DisputeEvidence.objects.create(ticket=ticket, image=photo, uploaded_by_role=role)

    # Tell the other side — until now they only found out when money froze.
    other = order.listing.seller_customer if role == 'buyer' else order.buyer_customer
    if other is not None:
        CustomerNotification.objects.create(
            customer=other,
            title=f'⚖️ تم فتح نزاع على «{order.listing.title[:60]}»',
            body=(f'{"المشتري" if role == "buyer" else "البائع"} فتح نزاع '
                  f'({ticket.get_category_display()}). المبلغ مجمّد لحين قرار الإدارة — '
                  f'فريق الدعم هيتواصل معاك.'),
            level='warning', icon='fa-scale-balanced',
            action_url='/marketplace/parts/sales/' if role == 'buyer' else '/marketplace/parts/orders/',
            action_label='تفاصيل الطلب',
        )

    return JsonResponse({
        'ok': True,
        'ticket_code': str(ticket.ticket_code),
        'status': ticket.status,
        'message': 'تم فتح النزاع. المبلغ مجمّد لحين قرار الإدارة.',
    })


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
