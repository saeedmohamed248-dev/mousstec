"""
Escrow service — P2P parts marketplace financial custody.

This module is the *only* place that:
  * Creates / mutates ``EscrowHold`` rows.
  * Decides who pays return shipping (policy table below).
  * Coordinates ``PartOrder`` status transitions with the financial side.

Views must call into here rather than mutating the EscrowHold model directly.
That gives us one chokepoint to audit, log, and (later) replace with a real
payment processor (Paymob payout, Stripe Connect, etc.).

Platform liability principle
----------------------------
The platform never moves its own money to cover a return. That rule is
encoded in two places:

  1. ``PartOrder.return_shipping_payer`` has a check constraint that
     forbids any value other than 'buyer' or 'seller' (DB enforcement).
  2. ``RETURN_REASON_TO_PAYER`` below — application enforcement.

If a future requirement needs the platform to absorb a cost (goodwill
credit, fraud reimbursement, etc.), model it as a separate ``GoodwillCredit``
flow with explicit ledger entries — never bend this rule.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone


# ── Return-shipping liability table ──────────────────────────────────
# Global-marketplace standard:
#   * Buyer's fault / change of mind → buyer pays return shipping.
#   * Seller's fault (defective/wrong/missing) → seller pays return shipping.
#   * Platform → never pays.
RETURN_REASON_TO_PAYER: dict[str, str] = {
    'buyer_remorse':     'buyer',
    'wrong_size_or_fit': 'buyer',
    'defective':         'seller',
    'incorrect':         'seller',
    'not_as_described':  'seller',
    'never_arrived':     'seller',
}


def who_pays_return(reason: str) -> str:
    """
    Pure helper. Returns 'buyer' or 'seller' — never 'platform'.
    Raises ValidationError for unknown reasons so callers can't silently
    drop a return into an undefined liability state.
    """
    payer = RETURN_REASON_TO_PAYER.get(reason)
    if payer not in ('buyer', 'seller'):
        raise ValidationError(
            f"Unknown return reason '{reason}'. "
            f"Allowed: {sorted(RETURN_REASON_TO_PAYER)}"
        )
    return payer


# ── Hold lifecycle ───────────────────────────────────────────────────
@transaction.atomic
def place_hold(order, *, accepted_disclaimer=None):
    """
    Create the EscrowHold record at the moment Paymob confirms payment.

    Idempotent: calling twice on the same order returns the existing hold
    rather than creating a duplicate. The webhook layer can retry safely.
    """
    from clients.models import EscrowHold

    existing = EscrowHold.objects.filter(order=order).first()
    if existing is not None:
        return existing

    if order.status not in ('paid_held',):
        # Don't gate on pending_payment — Paymob webhook flips status first,
        # then calls place_hold(). If status isn't paid_held here, the caller
        # is wrong and we should fail loudly.
        raise ValidationError(
            f"Cannot place escrow hold while order status is '{order.status}'. "
            f"Expected 'paid_held'."
        )

    return EscrowHold.objects.create(
        order=order,
        status='held',
        held_amount=order.amount_paid,
        seller_payout_amount=Decimal('0.00'),
        buyer_refund_amount=Decimal('0.00'),
        platform_commission_amount=Decimal('0.00'),
        accepted_disclaimer=accepted_disclaimer,
    )


@transaction.atomic
def release_to_seller(order, *, by_user=None, reason=''):
    """
    Move the held funds to the seller after the warranty window closes.
    Commission is recorded but not transferred anywhere — the platform
    is a custodian, so commission stays on the platform's books.
    """
    from clients.models import EscrowHold

    hold = EscrowHold.objects.select_for_update().get(order=order)
    if hold.status != 'held':
        raise ValidationError(
            f"Cannot release hold in status '{hold.status}'."
        )
    hold.seller_payout_amount = order.seller_payout
    hold.platform_commission_amount = order.commission_amount
    hold.buyer_refund_amount = Decimal('0.00')
    hold.status = 'released_to_seller'
    hold.settled_at = timezone.now()
    hold.settled_by = by_user if (by_user and getattr(by_user, 'is_authenticated', False)) else None
    hold.settlement_reason = (reason or 'warranty period elapsed')[:255]
    hold.save(update_fields=[
        'status', 'seller_payout_amount', 'buyer_refund_amount',
        'platform_commission_amount', 'settled_at', 'settled_by',
        'settlement_reason',
    ])
    return hold


@transaction.atomic
def refund_to_buyer(order, *, return_reason, by_user=None):
    """
    Full refund — buyer gets 100% back, seller gets 0, commission reversed.
    Records the return-shipping liability on the order.
    """
    from clients.models import EscrowHold

    hold = EscrowHold.objects.select_for_update().get(order=order)
    if hold.status != 'held':
        raise ValidationError(
            f"Cannot refund hold in status '{hold.status}'."
        )
    payer = who_pays_return(return_reason)

    hold.buyer_refund_amount = order.amount_paid
    hold.seller_payout_amount = Decimal('0.00')
    hold.platform_commission_amount = Decimal('0.00')
    hold.status = 'refunded_to_buyer'
    hold.settled_at = timezone.now()
    hold.settled_by = by_user if (by_user and getattr(by_user, 'is_authenticated', False)) else None
    hold.settlement_reason = f'refunded ({return_reason}); return shipping paid by {payer}'[:255]
    hold.save(update_fields=[
        'status', 'buyer_refund_amount', 'seller_payout_amount',
        'platform_commission_amount', 'settled_at', 'settled_by',
        'settlement_reason',
    ])

    order.return_reason = return_reason
    order.return_shipping_payer = payer
    order.save(update_fields=['return_reason', 'return_shipping_payer'])
    return hold


@transaction.atomic
def split_settlement(order, *, refund_amount: Decimal, return_reason, by_user=None):
    """
    Partial refund (e.g. minor cosmetic issue, buyer keeps part with a discount).
    Seller gets `amount_paid - refund_amount - commission`; commission applies
    to the kept portion only.
    """
    from clients.models import EscrowHold

    hold = EscrowHold.objects.select_for_update().get(order=order)
    if hold.status != 'held':
        raise ValidationError(f"Cannot split hold in status '{hold.status}'.")
    refund_amount = Decimal(refund_amount).quantize(Decimal('0.01'))
    if refund_amount <= 0 or refund_amount >= order.amount_paid:
        raise ValidationError(
            "Split refund must be strictly between 0 and the held amount. "
            "Use refund_to_buyer for full refunds, release_to_seller for none."
        )
    payer = who_pays_return(return_reason)

    kept_by_seller = order.amount_paid - refund_amount
    commission_pct = (order.commission_amount / order.amount_paid) if order.amount_paid else Decimal('0')
    new_commission = (kept_by_seller * commission_pct).quantize(Decimal('0.01'))
    new_payout = (kept_by_seller - new_commission).quantize(Decimal('0.01'))

    hold.buyer_refund_amount = refund_amount
    hold.seller_payout_amount = new_payout
    hold.platform_commission_amount = new_commission
    hold.status = 'split'
    hold.settled_at = timezone.now()
    hold.settled_by = by_user if (by_user and getattr(by_user, 'is_authenticated', False)) else None
    hold.settlement_reason = (
        f'split: refund {refund_amount} EGP ({return_reason}); '
        f'return shipping paid by {payer}'
    )[:255]
    hold.save(update_fields=[
        'status', 'buyer_refund_amount', 'seller_payout_amount',
        'platform_commission_amount', 'settled_at', 'settled_by',
        'settlement_reason',
    ])
    order.return_reason = return_reason
    order.return_shipping_payer = payer
    order.save(update_fields=['return_reason', 'return_shipping_payer'])
    return hold
