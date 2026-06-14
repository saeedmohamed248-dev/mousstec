"""Regression coverage for the BlindBiddingRequest money lifecycle.

The three methods on ``BlindBiddingRequest`` orchestrate every money
movement on the B2B auction side:

* ``trigger_escrow_hold()`` — open auction → freeze buyer's wallet
  into escrow_held (creates a ``hold`` ledger row).
* ``trigger_release_to_seller()`` — shipped auction → pay seller
  ``winning_price - fee``, deduct the platform commission, mark
  the auction completed (creates ``release`` + ``fee_deduction``
  ledger rows).
* ``trigger_refund_to_buyer()`` — disputed/cancelled auction →
  unfreeze escrow back into buyer's wallet (creates a ``refund``
  ledger row).

The downstream EscrowLedger signals are already pinned by
test_escrow_signals.py. **This file pins the orchestrator above
them**: the status guards, the fee calculation, the side effects
on trust scores, and the snapshot of ``platform_fee_collected`` on
the BlindBiddingRequest row itself.

A regression in any of these methods is a direct revenue or
reputation hit:

* trigger_escrow_hold fails its status guard → money frozen on an
  already-awarded auction (double-hold).
* trigger_release_to_seller fee math drifts → sellers paid the
  gross amount and the platform earns nothing.
* trigger_refund_to_buyer fires from the wrong status → escrow
  drained on a delivered auction (buyer refunded AND seller paid).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TransactionTestCase
from django.utils import timezone

from clients.models import BlindBiddingRequest, Client, EscrowLedger


def _make_client(*, wallet=Decimal('0'), held=Decimal('0'), fee_rate=Decimal('2.50'), suffix='x'):
    """Public-schema Client with no tenant schema provisioning."""
    c = Client(
        schema_name=f'bid_{suffix}',
        name=f'Bid {suffix}',
        owner_name='Owner',
        phone='01000000000',
        wallet_balance=wallet,
        escrow_held=held,
        platform_fee_rate=fee_rate,
    )
    c.auto_create_schema = False
    c.save()
    return c


def _make_open_bid(*, buyer, winning_price=Decimal('1000')):
    return BlindBiddingRequest.objects.create(
        buyer=buyer,
        part_number='P-001',
        required_qty=1,
        winning_price=winning_price,
        status='open',
        expires_at=timezone.now() + timedelta(days=1),
    )


class TriggerEscrowHoldTests(TransactionTestCase):
    """``trigger_escrow_hold`` should freeze winning_price from the
    buyer's wallet, but only when status == 'open' and a winning
    price is set."""

    def test_hold_moves_wallet_to_escrow(self):
        buyer = _make_client(wallet=Decimal('5000'), suffix='hold_ok')
        bid = _make_open_bid(buyer=buyer, winning_price=Decimal('1000'))

        bid.trigger_escrow_hold()

        buyer.refresh_from_db()
        bid.refresh_from_db()
        self.assertEqual(buyer.wallet_balance, Decimal('4000.00'))
        self.assertEqual(buyer.escrow_held, Decimal('1000.00'))
        self.assertEqual(bid.status, 'escrow_held')
        # One hold row exists, linked to the auction.
        self.assertEqual(
            EscrowLedger.objects.filter(
                bidding_request=bid, transaction_type='hold',
            ).count(),
            1,
        )

    def test_hold_rejected_when_already_held(self):
        """Double-hold protection. Without this, an admin retry would
        freeze the buyer's money twice for the same auction."""
        buyer = _make_client(wallet=Decimal('5000'), suffix='hold_dup')
        bid = _make_open_bid(buyer=buyer)
        bid.trigger_escrow_hold()

        with self.assertRaises(ValidationError):
            bid.trigger_escrow_hold()

    def test_hold_rejected_without_winning_price(self):
        """A bid that never awarded shouldn't be able to enter
        escrow — there's no amount to freeze."""
        buyer = _make_client(wallet=Decimal('5000'), suffix='hold_nopr')
        bid = BlindBiddingRequest.objects.create(
            buyer=buyer, part_number='P-002', required_qty=1,
            status='open', winning_price=None,
            expires_at=timezone.now() + timedelta(days=1),
        )
        with self.assertRaises(ValidationError):
            bid.trigger_escrow_hold()

    def test_hold_rejected_when_buyer_underfunded(self):
        """The escrow signal's pre_save guard should fire before the
        status flip lands. trigger_escrow_hold must propagate the
        rejection — silently swallowing it would leave the auction
        in a ``hold``-claiming status with no actual hold."""
        buyer = _make_client(wallet=Decimal('100'), suffix='hold_poor')
        bid = _make_open_bid(buyer=buyer, winning_price=Decimal('1000'))
        with self.assertRaises(ValidationError):
            bid.trigger_escrow_hold()
        bid.refresh_from_db()
        self.assertEqual(bid.status, 'open', 'Status must NOT flip if the hold failed.')


class TriggerReleaseToSellerTests(TransactionTestCase):
    """The release-to-seller method is the **revenue capture point**.
    It computes the platform fee, snapshots it on the bidding request,
    and creates the ledger rows that move money buyer-escrow →
    seller-wallet (minus fee)."""

    def _seller_and_shipped_bid(
        self, *, winning_price=Decimal('1000'),
        buyer_held=Decimal('1000'),
        buyer_fee_rate=Decimal('2.50'),
        seller_wallet=Decimal('0'),
    ):
        buyer = _make_client(
            wallet=Decimal('0'), held=buyer_held,
            fee_rate=buyer_fee_rate, suffix=f'rel_buy_{winning_price}',
        )
        seller = _make_client(
            wallet=seller_wallet,
            fee_rate=Decimal('2.50'), suffix=f'rel_sell_{winning_price}',
        )
        bid = BlindBiddingRequest.objects.create(
            buyer=buyer, part_number='P-RR',
            required_qty=1, winning_price=winning_price,
            winner=seller, status='shipped',
            expires_at=timezone.now() + timedelta(days=1),
        )
        return buyer, seller, bid

    def test_release_calculates_fee_from_buyer_rate(self):
        """fee = winning_price * buyer.platform_fee_rate / 100. The
        seller's payout is winning_price - fee."""
        buyer, seller, bid = self._seller_and_shipped_bid(
            winning_price=Decimal('1000'),
            buyer_fee_rate=Decimal('2.50'),
        )

        bid.trigger_release_to_seller()

        bid.refresh_from_db()
        seller.refresh_from_db()
        buyer.refresh_from_db()

        # Status + fee snapshot on the bid row.
        self.assertEqual(bid.status, 'completed')
        self.assertEqual(bid.platform_fee_collected, Decimal('25.00'))

        # Buyer escrow drained, seller paid net.
        self.assertEqual(buyer.escrow_held, Decimal('0.00'))
        self.assertEqual(seller.wallet_balance, Decimal('975.00'))

        # Both ledger rows exist (release + fee_deduction).
        self.assertEqual(
            EscrowLedger.objects.filter(
                bidding_request=bid, transaction_type='release',
            ).count(),
            1,
        )
        self.assertEqual(
            EscrowLedger.objects.filter(
                bidding_request=bid, transaction_type='fee_deduction',
            ).count(),
            1,
        )

    def test_release_with_zero_fee_rate_skips_fee_ledger_entry(self):
        """If the buyer was onboarded at 0% (a promo or partner deal),
        there's no commission to deduct — the fee_deduction ledger
        row must NOT be created. Otherwise the platform shows a
        spurious 0-EGP fee row, and any aggregation that counts rows
        instead of summing amounts will misreport."""
        buyer, seller, bid = self._seller_and_shipped_bid(
            winning_price=Decimal('500'),
            buyer_fee_rate=Decimal('0.00'),
        )

        bid.trigger_release_to_seller()
        bid.refresh_from_db()
        seller.refresh_from_db()

        self.assertEqual(bid.platform_fee_collected, Decimal('0.00'))
        self.assertEqual(seller.wallet_balance, Decimal('500.00'))
        self.assertEqual(
            EscrowLedger.objects.filter(
                bidding_request=bid, transaction_type='fee_deduction',
            ).count(),
            0,
            'fee_deduction ledger row should only exist when fee > 0',
        )

    def test_release_rejected_when_not_shipped(self):
        """Status guard. Releasing money on an auction that hasn't
        been confirmed shipped is the highest-risk failure mode —
        it would pay the seller before the buyer has even received
        the part."""
        buyer = _make_client(wallet=Decimal('0'), held=Decimal('1000'), suffix='rel_early')
        seller = _make_client(wallet=Decimal('0'), suffix='rel_early_sell')
        bid = BlindBiddingRequest.objects.create(
            buyer=buyer, part_number='P-E',
            required_qty=1, winning_price=Decimal('1000'),
            winner=seller, status='escrow_held',  # not yet shipped
            expires_at=timezone.now() + timedelta(days=1),
        )

        with self.assertRaises(ValidationError):
            bid.trigger_release_to_seller()

        # Nothing moved.
        buyer.refresh_from_db()
        seller.refresh_from_db()
        self.assertEqual(buyer.escrow_held, Decimal('1000.00'))
        self.assertEqual(seller.wallet_balance, Decimal('0.00'))

    def test_release_rejected_when_no_winner(self):
        """An auction that reached 'shipped' status without a winner
        shouldn't exist in real flows, but if data ever drifts there,
        we don't want a NoneType crash to leave money in escrow with
        no seller to pay."""
        buyer = _make_client(wallet=Decimal('0'), held=Decimal('500'), suffix='rel_nowin')
        bid = BlindBiddingRequest.objects.create(
            buyer=buyer, part_number='P-N',
            required_qty=1, winning_price=Decimal('500'),
            winner=None, status='shipped',
            expires_at=timezone.now() + timedelta(days=1),
        )

        with self.assertRaises(ValidationError):
            bid.trigger_release_to_seller()


class TriggerRefundToBuyerTests(TransactionTestCase):
    """``trigger_refund_to_buyer`` reverses an escrow hold. Allowed
    only from ``escrow_held`` or ``disputed`` statuses — refunding
    after delivery would double-pay (seller already received, buyer
    gets their money back too)."""

    def test_refund_returns_money_to_buyer_wallet(self):
        buyer = _make_client(wallet=Decimal('0'), held=Decimal('800'), suffix='refnd_ok')
        bid = BlindBiddingRequest.objects.create(
            buyer=buyer, part_number='P-RF',
            required_qty=1, winning_price=Decimal('800'),
            status='escrow_held',
            expires_at=timezone.now() + timedelta(days=1),
        )

        bid.trigger_refund_to_buyer()

        buyer.refresh_from_db()
        bid.refresh_from_db()
        self.assertEqual(buyer.wallet_balance, Decimal('800.00'))
        self.assertEqual(buyer.escrow_held, Decimal('0.00'))
        self.assertEqual(bid.status, 'cancelled')

    def test_refund_rejected_when_completed(self):
        """The single most dangerous failure mode: refunding after
        the seller has been paid. Must be blocked at the status guard."""
        buyer = _make_client(wallet=Decimal('0'), held=Decimal('0'), suffix='refnd_dn')
        bid = BlindBiddingRequest.objects.create(
            buyer=buyer, part_number='P-RFB',
            required_qty=1, winning_price=Decimal('500'),
            status='completed',
            expires_at=timezone.now() + timedelta(days=1),
        )

        with self.assertRaises(ValidationError):
            bid.trigger_refund_to_buyer()

    def test_refund_allowed_from_disputed_status(self):
        """A buyer-won dispute lands in 'disputed' status, then the
        admin/system triggers refund."""
        buyer = _make_client(wallet=Decimal('0'), held=Decimal('400'), suffix='refnd_dis')
        bid = BlindBiddingRequest.objects.create(
            buyer=buyer, part_number='P-RFD',
            required_qty=1, winning_price=Decimal('400'),
            status='disputed',
            expires_at=timezone.now() + timedelta(days=1),
        )

        bid.trigger_refund_to_buyer()

        buyer.refresh_from_db()
        bid.refresh_from_db()
        self.assertEqual(buyer.wallet_balance, Decimal('400.00'))
        self.assertEqual(bid.status, 'cancelled')
