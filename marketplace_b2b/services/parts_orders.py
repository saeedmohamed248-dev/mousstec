"""
Parts-order lifecycle service — P2P parts marketplace.

The single place that moves a ``PartOrder`` between its payment states:

  * ``mark_paid``            pending_payment → paid_held  (Paymob or manual receipt)
  * ``cancel_unpaid``        pending_payment → cancelled  (failed/rejected/expired payment)
  * ``expire_stale_orders``  periodic sweep of abandoned checkouts
  * ``expire_wanted_requests`` periodic sweep of old "Part Wanted" posts

Before this module existed each payment path (Paymob callback, Vodafone-Cash
receipt approval) duplicated the transition — and drifted: the manual path
never notified the seller that their part was sold, and a rejected receipt
left the listing ``reserved`` forever so nobody could buy it again.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')

# How long an unpaid checkout may keep a listing reserved.
PAYMOB_PENDING_TTL = timedelta(hours=2)
# A Vodafone-Cash buyer needs time to transfer and upload the screenshot.
MANUAL_PENDING_TTL = timedelta(hours=24)


def _sym():
    try:
        from erp_core.localization import current_tenant_symbol
        return current_tenant_symbol()
    except Exception:
        return 'ج.م'


def _notify(customer, **kwargs):
    if customer is None:
        return
    from clients.models import CustomerNotification
    try:
        CustomerNotification.objects.create(customer=customer, **kwargs)
    except Exception:
        logger.exception("[PARTS] notification failed for customer %s", getattr(customer, 'pk', None))


@transaction.atomic
def mark_paid(order, *, txn_id: str = '') -> bool:
    """
    Flip a pending order to ``paid_held``: listing → sold, escrow hold placed,
    both parties notified, and the originating wanted-request (if any) closed.

    Idempotent — returns False (and does nothing) if the order is no longer
    pending, so Paymob retries and double-approvals are harmless.
    """
    from clients.models import PartListing, PartOrder, PartWantedOffer
    from clients.services import escrow as escrow_svc

    order = PartOrder.objects.select_for_update(of=('self',)).select_related(
        'listing', 'listing__seller_customer', 'buyer_customer',
    ).get(pk=order.pk)
    if order.status != 'pending_payment':
        return False

    now = timezone.now()
    order.status = 'paid_held'
    order.paid_at = now
    if txn_id:
        order.paymob_txn_id = txn_id[:100]
    order.save(update_fields=['status', 'paid_at', 'paymob_txn_id'])
    PartListing.objects.filter(pk=order.listing_id).update(status='sold', sold_at=now)

    # 💰 The escrow hold is the financial record of custody. If it fails the
    # settlement functions self-heal from ``paid_at`` later, so log loudly
    # but don't lose the payment confirmation.
    try:
        escrow_svc.place_hold(order)
    except Exception:
        logger.exception("[PARTS] CRITICAL: place_hold failed for order %s", order.order_code)

    # Close the wanted request this listing was created for (if any).
    offer = (
        PartWantedOffer.objects.filter(linked_listing_id=order.listing_id, status='accepted')
        .select_related('request').first()
    )
    if offer is not None and offer.request.status in ('open', 'matched'):
        req = offer.request
        req.status = 'fulfilled'
        req.fulfilled_at = now
        req.save(update_fields=['status', 'fulfilled_at'])

    listing = order.listing
    sym = _sym()
    _notify(
        listing.seller_customer if listing.seller_customer_id else None,
        title=f'🎉 تم بيع «{listing.title}»',
        body=(
            f'المشتري دفع {order.amount_paid} {sym} — الفلوس في الـ Escrow. '
            f'جهّز القطعة وابعتها على العنوان الموجود في صفحة مبيعاتك. '
            f'هتستلم {order.seller_payout} {sym} بعد {order.warranty_days} يوم من تأكيد التسليم.'
        ),
        level='success', icon='fa-money-check-dollar',
        action_url='/marketplace/parts/sales/', action_label='تفاصيل البيع',
    )
    _notify(
        order.buyer_customer if order.buyer_customer_id else None,
        title='✅ تم استلام دفعتك',
        body=(
            f'فلوسك آمنة في الـ Escrow. هتتحرر للبائع بعد {order.warranty_days} يوم '
            f'من استلامك للقطعة. لو فيها مشكلة، تقدر تطلب إرجاع أو تفتح نزاع خلال فترة الضمان.'
        ),
        level='success', icon='fa-shield-halved',
        action_url='/marketplace/parts/orders/', action_label='طلباتي',
    )
    return True


@transaction.atomic
def cancel_unpaid(order, *, reason: str = '', notify_buyer: bool = True) -> bool:
    """
    Cancel a ``pending_payment`` order and put its listing back on sale.
    Returns False if the order already moved on (e.g. it was paid meanwhile).
    """
    from clients.models import PartListing, PartOrder

    order = PartOrder.objects.select_for_update(of=('self',)).select_related(
        'listing', 'buyer_customer',
    ).get(pk=order.pk)
    if order.status != 'pending_payment':
        return False
    order.status = 'cancelled'
    if reason:
        order.admin_notes = (order.admin_notes + '\n' if order.admin_notes else '') + f'[CANCELLED] {reason}'[:500]
    order.save(update_fields=['status', 'admin_notes'])
    # Only release a reservation this order actually holds — never flip a
    # listing that another order has since bought.
    still_reserved_by_us = not PartOrder.objects.filter(
        listing_id=order.listing_id,
        status__in=('pending_payment', 'paid_held', 'shipped', 'delivered',
                    'released', 'refund_requested', 'disputed'),
    ).exclude(pk=order.pk).exists()
    if still_reserved_by_us:
        PartListing.objects.filter(pk=order.listing_id, status='reserved').update(status='active')
    if notify_buyer and order.buyer_customer_id:
        _notify(
            order.buyer_customer,
            title='⌛ تم إلغاء طلب الشراء',
            body=(
                f'طلب شراء «{order.listing.title}» اتلغى لأن الدفع ماتمّش'
                + (f' ({reason})' if reason else '') + '. تقدر تشتري تاني لو القطعة لسه متاحة.'
            ),
            level='warning', icon='fa-clock',
            action_url=f'/marketplace/parts/{order.listing.listing_code}/', action_label='القطعة',
        )
    return True


def expire_stale_orders(now=None) -> int:
    """
    Cancel abandoned checkouts so their listings go back on sale.

    * Paymob checkouts: 2 hours.
    * Vodafone-Cash checkouts: 24 hours — unless the buyer already uploaded a
      receipt that is waiting for admin review (never expire those; the admin
      decides).
    """
    from clients.models import ManualPaymentReceipt, PartOrder

    now = now or timezone.now()
    candidates = PartOrder.objects.filter(
        status='pending_payment', created_at__lt=now - PAYMOB_PENDING_TTL,
    ).only('pk', 'created_at')[:500]
    n = 0
    for order in candidates:
        receipts = ManualPaymentReceipt.objects.filter(purchase_type='parts', purchase_id=order.pk)
        if receipts.exists():
            if receipts.filter(status__in=('pending', 'confirmed')).exclude(txn_reference='').exists():
                continue  # receipt uploaded — waiting for the admin
            if order.created_at >= now - MANUAL_PENDING_TTL:
                continue  # still inside the manual-transfer window
            reason = 'لم يتم رفع إيصال التحويل خلال 24 ساعة'
        else:
            reason = 'انتهت مهلة الدفع الإلكتروني'
        if cancel_unpaid(order, reason=reason):
            receipts.filter(status='pending', txn_reference='').update(
                status='rejected', reviewed_at=now, review_notes='انتهت المهلة بدون رفع إيصال',
            )
            n += 1
    if n:
        logger.info("[PARTS] expired %d abandoned checkout(s)", n)
    return n


def expire_wanted_requests(now=None) -> int:
    """Flip open 'Part Wanted' requests past their expiry to ``expired``."""
    from clients.models import PartWantedOffer, PartWantedRequest

    now = now or timezone.now()
    ids = list(
        PartWantedRequest.objects.filter(status='open', expires_at__lt=now)
        .values_list('pk', flat=True)[:1000]
    )
    if not ids:
        return 0
    with transaction.atomic():
        n = PartWantedRequest.objects.filter(pk__in=ids, status='open').update(status='expired')
        PartWantedOffer.objects.filter(request_id__in=ids, status='pending').update(status='rejected')
    return n
