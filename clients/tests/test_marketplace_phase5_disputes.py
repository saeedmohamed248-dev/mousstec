"""
Phase 5 — Dispute resolution tests.

Verifies:
1. 3-day inspection window enforced; opening after that fails.
2. Opening a dispute flips order to 'disputed' so auto_release_expired_warranties
   skips it (escrow funds frozen until admin acts).
3. Only the buyer or seller of an order has standing to open a dispute.
4. resolve_with_refund routes through escrow.refund_to_buyer and records
   return-shipping payer correctly.
5. resolve_with_release routes through escrow.release_to_seller.
6. resolve_with_split routes through escrow.split_settlement.
7. cancel_dispute restores the order to its pre-dispute status.
8. A second open dispute on the same order is funneled into the existing one.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from clients.models import (
    MarketplaceCustomer, PartCarMake, PartListing, PartOrder,
    EscrowHold, DisputeTicket,
)
from clients.services import escrow as escrow_svc
from clients.services import disputes as dispute_svc


def _phone() -> str:
    return f'+2010{uuid.uuid4().int % 100_000_000:08d}'


def _customer(name='X') -> MarketplaceCustomer:
    return MarketplaceCustomer.objects.create(
        customer_type='individual', full_name=name, phone=_phone(),
        sector='automotive', is_verified=True,
    )


def _delivered_order(*, hours_since_delivery=1):
    seller = _customer('Seller')
    buyer = _customer('Buyer')
    make = PartCarMake.objects.create(
        name=f'M-{uuid.uuid4().hex[:6]}', slug=f's-{uuid.uuid4().hex[:6]}',
    )
    listing = PartListing.objects.create(
        seller_customer=seller, title='X', description='Y',
        car_make=make, price_egp=Decimal('1000'), warranty_days=3,
        status='active', moderation_status='approved',
    )
    order = PartOrder.objects.create(
        listing=listing, buyer_customer=buyer,
        amount_paid=Decimal('1000'), commission_amount=Decimal('80'),
        seller_payout=Decimal('920'), warranty_days=3,
        status='paid_held',
    )
    escrow_svc.place_hold(order)
    # Walk through the lifecycle.
    order.status = 'delivered'
    order.delivered_at = timezone.now() - timedelta(hours=hours_since_delivery)
    order.warranty_ends_at = order.delivered_at + timedelta(days=3)
    order.save(update_fields=['status', 'delivered_at', 'warranty_ends_at'])
    return seller, buyer, order


# ---------------------------------------------------------------------------
# 1. 3-day window
# ---------------------------------------------------------------------------
class DisputeWindowTests(TestCase):
    def test_within_window_allowed(self):
        seller, buyer, order = _delivered_order(hours_since_delivery=24)
        ticket = dispute_svc.open_dispute(
            order=order, opener=buyer, opener_role='buyer',
            category='damaged_on_arrival', description='Cracked on arrival',
        )
        self.assertEqual(ticket.status, 'open')

    def test_after_window_rejected(self):
        seller, buyer, order = _delivered_order(hours_since_delivery=24 * 5)
        with self.assertRaises(ValidationError):
            dispute_svc.open_dispute(
                order=order, opener=buyer, opener_role='buyer',
                category='damaged_on_arrival', description='Late claim',
            )

    def test_paid_held_allows_dispute_for_never_arrived(self):
        seller = _customer('S')
        buyer = _customer('B')
        make = PartCarMake.objects.create(
            name=f'M-{uuid.uuid4().hex[:6]}', slug=f's-{uuid.uuid4().hex[:6]}',
        )
        listing = PartListing.objects.create(
            seller_customer=seller, title='X', description='Y',
            car_make=make, price_egp=Decimal('500'), warranty_days=3,
            status='active', moderation_status='approved',
        )
        order = PartOrder.objects.create(
            listing=listing, buyer_customer=buyer,
            amount_paid=Decimal('500'), commission_amount=Decimal('40'),
            seller_payout=Decimal('460'), warranty_days=3,
            status='paid_held',
        )
        escrow_svc.place_hold(order)
        ticket = dispute_svc.open_dispute(
            order=order, opener=buyer, opener_role='buyer',
            category='item_not_received', description='Hasn\'t shipped after 2 weeks',
        )
        self.assertEqual(ticket.status, 'open')

    def test_released_order_cannot_be_disputed(self):
        seller, buyer, order = _delivered_order()
        order.status = 'released'
        order.save(update_fields=['status'])
        with self.assertRaises(ValidationError):
            dispute_svc.open_dispute(
                order=order, opener=buyer, opener_role='buyer',
                category='other', description='Too late',
            )


# ---------------------------------------------------------------------------
# 2. Standing
# ---------------------------------------------------------------------------
class DisputeStandingTests(TestCase):
    def test_stranger_cannot_open(self):
        seller, buyer, order = _delivered_order()
        stranger = _customer('Z')
        with self.assertRaises(PermissionDenied):
            dispute_svc.open_dispute(
                order=order, opener=stranger, opener_role='buyer',
                category='other', description='I want money',
            )

    def test_seller_can_open(self):
        seller, buyer, order = _delivered_order()
        ticket = dispute_svc.open_dispute(
            order=order, opener=seller, opener_role='seller',
            category='buyer_misuse',
            description='Buyer is filing chargebacks fraudulently.',
        )
        self.assertEqual(ticket.opened_by_role, 'seller')


# ---------------------------------------------------------------------------
# 3. Escrow auto-pause
# ---------------------------------------------------------------------------
class EscrowAutoPauseTests(TestCase):
    def test_open_dispute_freezes_order_status_to_disputed(self):
        seller, buyer, order = _delivered_order()
        dispute_svc.open_dispute(
            order=order, opener=buyer, opener_role='buyer',
            category='damaged_on_arrival', description='Cracked.',
        )
        order.refresh_from_db()
        self.assertEqual(order.status, 'disputed')

    def test_auto_release_skips_disputed_orders(self):
        seller, buyer, order = _delivered_order()
        # Force warranty into the past so it would otherwise auto-release.
        order.warranty_ends_at = timezone.now() - timedelta(hours=1)
        order.save(update_fields=['warranty_ends_at'])
        dispute_svc.open_dispute(
            order=order, opener=buyer, opener_role='buyer',
            category='wrong_item', description='Sent the wrong throttle body.',
        )
        from clients.views.parts_marketplace_views import auto_release_expired_warranties
        released = auto_release_expired_warranties()
        self.assertEqual(released, 0)
        order.refresh_from_db()
        self.assertEqual(order.status, 'disputed')  # still frozen


# ---------------------------------------------------------------------------
# 4. Resolutions route to escrow correctly
# ---------------------------------------------------------------------------
class ResolutionTests(TestCase):
    def test_refund_routes_to_buyer(self):
        seller, buyer, order = _delivered_order()
        ticket = dispute_svc.open_dispute(
            order=order, opener=buyer, opener_role='buyer',
            category='item_not_as_described', description='Photos lied.',
        )
        dispute_svc.resolve_with_refund(ticket, return_reason='not_as_described')
        ticket.refresh_from_db()
        order.refresh_from_db()
        hold = EscrowHold.objects.get(order=order)
        self.assertEqual(ticket.status, 'resolved_refund')
        self.assertEqual(order.status, 'refunded')
        self.assertEqual(hold.status, 'refunded_to_buyer')
        self.assertEqual(hold.buyer_refund_amount, order.amount_paid)
        self.assertEqual(order.return_shipping_payer, 'seller')

    def test_release_routes_to_seller(self):
        seller, buyer, order = _delivered_order()
        ticket = dispute_svc.open_dispute(
            order=order, opener=seller, opener_role='seller',
            category='buyer_misuse', description='Fraudulent chargeback.',
        )
        dispute_svc.resolve_with_release(ticket)
        ticket.refresh_from_db()
        order.refresh_from_db()
        hold = EscrowHold.objects.get(order=order)
        self.assertEqual(ticket.status, 'resolved_release')
        self.assertEqual(order.status, 'released')
        self.assertEqual(hold.status, 'released_to_seller')
        self.assertEqual(hold.seller_payout_amount, order.seller_payout)

    def test_split_resolution(self):
        seller, buyer, order = _delivered_order()
        ticket = dispute_svc.open_dispute(
            order=order, opener=buyer, opener_role='buyer',
            category='item_not_as_described', description='Minor cosmetic.',
        )
        dispute_svc.resolve_with_split(
            ticket, refund_amount=Decimal('200'), return_reason='not_as_described',
        )
        ticket.refresh_from_db()
        hold = EscrowHold.objects.get(order=order)
        self.assertEqual(ticket.status, 'resolved_split')
        self.assertEqual(hold.status, 'split')
        self.assertEqual(hold.buyer_refund_amount, Decimal('200.00'))


# ---------------------------------------------------------------------------
# 5. Cancel + idempotency
# ---------------------------------------------------------------------------
class CancelAndIdempotencyTests(TestCase):
    def test_cancel_restores_order_status(self):
        seller, buyer, order = _delivered_order()
        ticket = dispute_svc.open_dispute(
            order=order, opener=buyer, opener_role='buyer',
            category='other', description='Sorry, false alarm.',
        )
        dispute_svc.cancel_dispute(ticket, by_role='buyer')
        ticket.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(ticket.status, 'cancelled')
        self.assertEqual(order.status, 'delivered')  # restored

    def test_non_opener_cannot_cancel(self):
        seller, buyer, order = _delivered_order()
        ticket = dispute_svc.open_dispute(
            order=order, opener=buyer, opener_role='buyer',
            category='other', description='...',
        )
        with self.assertRaises(PermissionDenied):
            dispute_svc.cancel_dispute(ticket, by_role='seller')

    def test_second_open_returns_existing_ticket(self):
        seller, buyer, order = _delivered_order()
        t1 = dispute_svc.open_dispute(
            order=order, opener=buyer, opener_role='buyer',
            category='damaged_on_arrival', description='Broken',
        )
        t2 = dispute_svc.open_dispute(
            order=order, opener=buyer, opener_role='buyer',
            category='wrong_item', description='Also wrong',
        )
        self.assertEqual(t1.pk, t2.pk)
