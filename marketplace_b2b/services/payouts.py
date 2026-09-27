"""
Payout service — turns escrow settlements into money actually sent.

Every escrow settlement (release / refund / split) ends with the platform
owing someone a transfer. ``queue_for_hold`` records those transfers as
``MarketplacePayout`` rows; an admin sends the money (Vodafone Cash /
InstaPay / bank) and closes the row with ``mark_paid``.

The escrow service calls ``queue_for_hold`` itself, so no settlement path can
forget it.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')

PAYOUT_WALLET_URL = '/marketplace/parts/wallet/'


def _notify(customer, **kwargs):
    if customer is None:
        return
    from clients.models import CustomerNotification
    try:
        CustomerNotification.objects.create(customer=customer, **kwargs)
    except Exception:
        logger.exception("[PAYOUT] notification failed for customer %s", customer.pk)


def _sym():
    try:
        from erp_core.localization import current_tenant_symbol
        return current_tenant_symbol()
    except Exception:
        return 'ج.م'


def _create(hold, *, kind, amount, customer=None, tenant=None, notify=True):
    from clients.models import MarketplacePayout
    amount = Decimal(amount or 0).quantize(Decimal('0.01'))
    if amount <= 0 or (customer is None and tenant is None):
        return None
    payout, created = MarketplacePayout.objects.get_or_create(
        hold=hold, kind=kind,
        defaults={
            'order_id': hold.order_id, 'amount': amount,
            'customer': customer, 'tenant': None if customer else tenant,
        },
    )
    if created and notify and customer is not None:
        what = 'مستحقاتك من بيع' if kind == 'seller_payout' else 'استرداد مبلغ'
        if customer.has_payout_details:
            body = (f'{what} «{hold.order.listing.title[:60]}»: {amount} {_sym()} — '
                    f'هيتحوّل على {customer.get_payout_method_display()} {customer.payout_account} خلال 1-3 أيام عمل.')
        else:
            body = (f'{what} «{hold.order.listing.title[:60]}»: {amount} {_sym()}. '
                    f'سجّل رقم فودافون كاش أو إنستاباي أو حسابك البنكي عشان نحوّلك الفلوس.')
        _notify(
            customer, title='💸 ليك فلوس عندنا', body=body,
            level='success' if customer.has_payout_details else 'warning',
            icon='fa-wallet', action_url=PAYOUT_WALLET_URL, action_label='محفظتي',
        )
    return payout


@transaction.atomic
def queue_for_hold(hold, *, notify=True):
    """
    Create the payout rows a settled hold implies. Idempotent — safe to call
    again (one row per hold × kind).
    """
    order = hold.order
    listing = order.listing
    created = []
    if hold.seller_payout_amount and hold.seller_payout_amount > 0:
        p = _create(hold, kind='seller_payout', amount=hold.seller_payout_amount,
                    customer=listing.seller_customer, tenant=listing.seller_tenant, notify=notify)
        if p:
            created.append(p)
    if hold.buyer_refund_amount and hold.buyer_refund_amount > 0:
        p = _create(hold, kind='buyer_refund', amount=hold.buyer_refund_amount,
                    customer=order.buyer_customer, tenant=order.buyer_tenant, notify=notify)
        if p:
            created.append(p)
    return created


@transaction.atomic
def mark_paid(payout, *, by_user=None, reference='', notes=''):
    """Admin confirms the transfer was sent. Snapshots the destination."""
    from clients.models import MarketplacePayout
    payout = MarketplacePayout.objects.select_for_update().get(pk=payout.pk)
    if payout.status != 'pending':
        raise ValidationError('التحويل ده مش في انتظار الدفع.')
    reference = (reference or '').strip()
    if len(reference) < 3:
        raise ValidationError('اكتب رقم عملية التحويل.')
    method, account, name = payout.destination
    if not account:
        raise ValidationError('صاحب المستحقات لسه ماسجّلش وسيلة استلام الفلوس.')
    payout.method, payout.account, payout.account_name = method or '', account[:100], (name or '')[:120]
    payout.reference = reference[:200]
    payout.notes = (notes or '')[:2000]
    payout.status = 'paid'
    payout.paid_at = timezone.now()
    payout.paid_by = by_user if (by_user and getattr(by_user, 'is_authenticated', False)) else None
    payout.save(update_fields=['method', 'account', 'account_name', 'reference', 'notes',
                               'status', 'paid_at', 'paid_by'])
    _notify(
        payout.customer if payout.customer_id else None,
        title='✅ تم تحويل فلوسك',
        body=(f'حوّلنا {payout.amount} {_sym()} على {account} — رقم العملية: {payout.reference}. '
              f'({payout.get_kind_display()} — «{payout.order.listing.title[:50]}»)'),
        level='success', icon='fa-circle-check',
        action_url=PAYOUT_WALLET_URL, action_label='محفظتي',
    )
    return payout


def wallet_summary(*, customer=None, tenant=None):
    """
    Numbers for a seller's wallet: money still in escrow, money released and
    waiting for the transfer, and money already transferred.
    """
    from clients.models import MarketplacePayout, PartOrder
    if customer is not None:
        sold = PartOrder.objects.filter(listing__seller_customer=customer)
        payouts = MarketplacePayout.objects.filter(customer=customer)
    else:
        sold = PartOrder.objects.filter(listing__seller_tenant=tenant)
        payouts = MarketplacePayout.objects.filter(tenant=tenant)
    in_escrow = sold.filter(
        status__in=('paid_held', 'shipped', 'delivered', 'refund_requested', 'disputed'),
    ).aggregate(s=Sum('seller_payout'))['s'] or Decimal('0.00')
    pending = payouts.filter(status='pending').aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
    paid = payouts.filter(status='paid').aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
    return {'in_escrow': in_escrow, 'pending': pending, 'paid': paid}
