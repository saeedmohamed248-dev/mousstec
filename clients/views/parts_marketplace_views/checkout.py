"""
🚗 سوق قطع الغيار P2P — الشراء: Paymob + بيانات الشحن + حجز القطعة + الـ callback (الفلوس بتتحجز escrow).
"""
from __future__ import annotations

import json
import logging
import os
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from clients.models import (
    PartListing,
    PartOrder,
    PlatformEvent,
)
from clients.views._shared import _marketplace_auth
from marketplace_b2b.services import parts_orders as orders_svc
from billing.services.paymob import paymob_cache

from clients.views._parts_helpers import (
    _parse_int,
)

logger = logging.getLogger('mouss_tec_core')


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
    from django.conf import settings as _s
    if not getattr(_s, 'MARKETPLACE_CARD_PAYMENTS', False):
        # الدفع في السوق بفودافون كاش/إنستاباي بس (manual_pay_parts_start)
        return JsonResponse({'error': 'الدفع في السوق حالياً بفودافون كاش أو إنستاباي بس.'}, status=400)
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
    paymob_cache.set(f'paymob_part_order_{paymob_order_id}', str(order.order_code), timeout=7200)

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
        order_code = paymob_cache.get(f'paymob_part_order_{paymob_order_id}')
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
