"""
Phase 3 — Escrow hardening tests.

Verifies:
1. who_pays_return — buyer for remorse, seller for fault, never platform.
2. place_hold / release_to_seller / refund_to_buyer / split_settlement
   produce the right amounts and update PartOrder.return_* correctly.
3. EscrowHold lifecycle is one-directional (can't re-release a refunded
   hold, can't double-place a hold on the same order).
4. The DB-level constraint forbids 'platform' as a return-shipping payer.
5. PlatformLiabilityDisclaimer.current() returns the seeded v1.0 row.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from clients.models import (
    MarketplaceCustomer, PartCarMake, PartListing, PartOrder,
    EscrowHold, PlatformLiabilityDisclaimer,
)
from clients.services import escrow as escrow_svc


def _fresh_phone() -> str:
    return f'+2010{uuid.uuid4().int % 100_000_000:08d}'


def _setup_paid_order(price=Decimal('1000.00'), commission=Decimal('80.00')):
    seller = MarketplaceCustomer.objects.create(
        customer_type='individual', full_name='Seller', phone=_fresh_phone(),
        sector='automotive', is_verified=True,
    )
    buyer = MarketplaceCustomer.objects.create(
        customer_type='individual', full_name='Buyer', phone=_fresh_phone(),
        sector='automotive', is_verified=True,
    )
    make = PartCarMake.objects.create(
        name=f'M-{uuid.uuid4().hex[:6]}', slug=f's-{uuid.uuid4().hex[:6]}',
    )
    listing = PartListing.objects.create(
        seller_customer=seller, title='X', description='Y',
        car_make=make, price_egp=price, warranty_days=3,
        status='active', moderation_status='approved',
    )
    order = PartOrder.objects.create(
        listing=listing,
        buyer_customer=buyer,
        amount_paid=price,
        commission_amount=commission,
        seller_payout=price - commission,
        warranty_days=3,
        status='paid_held',
    )
    return order


# ---------------------------------------------------------------------------
# 1. Return liability policy
# ---------------------------------------------------------------------------
class ReturnPayerPolicyTests(TestCase):
    def test_buyer_pays_when_buyer_remorse(self):
        self.assertEqual(escrow_svc.who_pays_return('buyer_remorse'), 'buyer')

    def test_buyer_pays_when_wrong_size(self):
        self.assertEqual(escrow_svc.who_pays_return('wrong_size_or_fit'), 'buyer')

    def test_seller_pays_when_defective(self):
        self.assertEqual(escrow_svc.who_pays_return('defective'), 'seller')

    def test_seller_pays_when_incorrect(self):
        self.assertEqual(escrow_svc.who_pays_return('incorrect'), 'seller')

    def test_seller_pays_when_never_arrived(self):
        self.assertEqual(escrow_svc.who_pays_return('never_arrived'), 'seller')

    def test_unknown_reason_rejected(self):
        with self.assertRaises(ValidationError):
            escrow_svc.who_pays_return('platform_goodwill')

    def test_platform_is_never_a_legal_payer(self):
        # Critical legal invariant — assert no policy row produces 'platform'.
        for reason, payer in escrow_svc.RETURN_REASON_TO_PAYER.items():
            self.assertIn(payer, ('buyer', 'seller'), f"{reason} → {payer}")


# ---------------------------------------------------------------------------
# 2. Hold lifecycle
# ---------------------------------------------------------------------------
class EscrowHoldLifecycleTests(TestCase):
    def test_place_hold_creates_held_record(self):
        order = _setup_paid_order()
        hold = escrow_svc.place_hold(order)
        self.assertEqual(hold.status, 'held')
        self.assertEqual(hold.held_amount, order.amount_paid)
        self.assertEqual(hold.seller_payout_amount, Decimal('0.00'))

    def test_place_hold_is_idempotent(self):
        order = _setup_paid_order()
        h1 = escrow_svc.place_hold(order)
        h2 = escrow_svc.place_hold(order)
        self.assertEqual(h1.pk, h2.pk)

    def test_place_hold_rejects_unpaid_order(self):
        order = _setup_paid_order()
        order.status = 'pending_payment'
        order.save(update_fields=['status'])
        with self.assertRaises(ValidationError):
            escrow_svc.place_hold(order)

    def test_release_moves_money_to_seller(self):
        order = _setup_paid_order()
        escrow_svc.place_hold(order)
        hold = escrow_svc.release_to_seller(order, reason='warranty elapsed')
        self.assertEqual(hold.status, 'released_to_seller')
        self.assertEqual(hold.seller_payout_amount, order.seller_payout)
        self.assertEqual(hold.platform_commission_amount, order.commission_amount)
        self.assertEqual(hold.buyer_refund_amount, Decimal('0.00'))

    def test_full_refund_records_return_payer(self):
        order = _setup_paid_order()
        escrow_svc.place_hold(order)
        hold = escrow_svc.refund_to_buyer(order, return_reason='defective')
        order.refresh_from_db()
        self.assertEqual(hold.status, 'refunded_to_buyer')
        self.assertEqual(hold.buyer_refund_amount, order.amount_paid)
        self.assertEqual(hold.seller_payout_amount, Decimal('0.00'))
        self.assertEqual(order.return_reason, 'defective')
        self.assertEqual(order.return_shipping_payer, 'seller')

    def test_buyer_remorse_full_refund_charges_buyer_for_shipping(self):
        order = _setup_paid_order()
        escrow_svc.place_hold(order)
        escrow_svc.refund_to_buyer(order, return_reason='buyer_remorse')
        order.refresh_from_db()
        self.assertEqual(order.return_shipping_payer, 'buyer')

    def test_cannot_release_after_refund(self):
        order = _setup_paid_order()
        escrow_svc.place_hold(order)
        escrow_svc.refund_to_buyer(order, return_reason='defective')
        with self.assertRaises(ValidationError):
            escrow_svc.release_to_seller(order)

    def test_split_settlement_math(self):
        order = _setup_paid_order(price=Decimal('1000'), commission=Decimal('80'))
        escrow_svc.place_hold(order)
        # Partial refund of 200; seller keeps 800.
        # Commission scales: 8% of 800 = 64; payout = 736.
        hold = escrow_svc.split_settlement(
            order, refund_amount=Decimal('200'),
            return_reason='not_as_described',
        )
        self.assertEqual(hold.status, 'split')
        self.assertEqual(hold.buyer_refund_amount, Decimal('200.00'))
        self.assertEqual(hold.platform_commission_amount, Decimal('64.00'))
        self.assertEqual(hold.seller_payout_amount, Decimal('736.00'))
        order.refresh_from_db()
        self.assertEqual(order.return_shipping_payer, 'seller')

    def test_split_rejects_full_or_zero_amount(self):
        order = _setup_paid_order()
        escrow_svc.place_hold(order)
        with self.assertRaises(ValidationError):
            escrow_svc.split_settlement(
                order, refund_amount=Decimal('0'), return_reason='defective',
            )
        with self.assertRaises(ValidationError):
            escrow_svc.split_settlement(
                order, refund_amount=order.amount_paid, return_reason='defective',
            )


# ---------------------------------------------------------------------------
# 3. DB constraint — platform never as return payer
# ---------------------------------------------------------------------------
class PlatformNeverPaysDbTests(TestCase):
    def test_platform_value_rejected_by_db(self):
        order = _setup_paid_order()
        order.return_shipping_payer = 'platform'  # malicious / buggy code path
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                order.save(update_fields=['return_shipping_payer'])

    def test_buyer_and_seller_values_accepted(self):
        order = _setup_paid_order()
        for v in ('buyer', 'seller', ''):
            order.return_shipping_payer = v
            order.save(update_fields=['return_shipping_payer'])  # must not raise


# ---------------------------------------------------------------------------
# 4. Disclaimer seed
# ---------------------------------------------------------------------------
class DisclaimerTests(TestCase):
    def test_v1_0_disclaimer_is_seeded_and_active(self):
        d = PlatformLiabilityDisclaimer.current()
        self.assertIsNotNone(d)
        self.assertEqual(d.version, 'v1.0')
        self.assertTrue(d.is_active)
        self.assertIn('Escrow', d.body_ar)
        self.assertIn('escrow', d.body_en.lower())
