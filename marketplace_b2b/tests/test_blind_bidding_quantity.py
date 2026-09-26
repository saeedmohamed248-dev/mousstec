"""
B2B blind bidding — quantity & auto-award money regressions (review 2026-09).

Before the fix:
* ``trigger_escrow_hold`` froze the price of ONE unit even when the buyer
  asked for many.
* The auto-award path froze ``price × qty × (1 + fee)`` from the buyer while
  the release only paid out ``price`` of one unit — the rest of the buyer's
  money stayed in ``escrow_held`` forever, and the fee was charged twice
  (once to the buyer on hold, once to the seller on release).
* The AI award task set ``completed`` then called a release that requires
  ``shipped`` — it failed on every run.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.test import TransactionTestCase
from django.utils import timezone

from clients.models import BidOffer, BlindBiddingRequest, Client, EscrowLedger


def _client(suffix, *, wallet=Decimal('0'), fee_rate=Decimal('2.50')):
    c = Client(
        schema_name=f'qty_{suffix}', name=f'Qty {suffix}', owner_name='O', phone='01000000000',
        wallet_balance=wallet, escrow_held=Decimal('0'), platform_fee_rate=fee_rate,
    )
    c.auto_create_schema = False
    c.save()
    return c


class QuantityEscrowTests(TransactionTestCase):
    def _bid(self, buyer, *, qty=3, price=Decimal('100'), status='open', winner=None):
        return BlindBiddingRequest.objects.create(
            buyer=buyer, part_number='P-QTY', required_qty=qty, winning_price=price,
            winner=winner, status=status, expires_at=timezone.now() + timedelta(hours=2),
        )

    def test_hold_freezes_price_times_quantity(self):
        buyer = _client('h_b', wallet=Decimal('1000'))
        bid = self._bid(buyer, qty=3)
        bid.trigger_escrow_hold()
        buyer.refresh_from_db()
        self.assertEqual(buyer.escrow_held, Decimal('300.00'))
        self.assertEqual(buyer.wallet_balance, Decimal('700.00'))

    def test_release_pays_seller_full_quantity_and_clears_escrow(self):
        buyer = _client('r_b', wallet=Decimal('1000'))
        seller = _client('r_s')
        bid = self._bid(buyer, qty=3, winner=seller)
        bid.trigger_escrow_hold()
        bid.status = 'shipped'
        bid.save(update_fields=['status'])
        bid.trigger_release_to_seller()

        buyer.refresh_from_db(); seller.refresh_from_db(); bid.refresh_from_db()
        self.assertEqual(buyer.escrow_held, Decimal('0.00'))
        self.assertEqual(bid.platform_fee_collected, Decimal('7.50'))    # 2.5% of 300
        self.assertEqual(seller.wallet_balance, Decimal('292.50'))       # 300 - fee

    def test_legacy_overheld_escrow_is_refunded_on_release(self):
        # Old auto-award rows froze price × qty × (1 + fee).
        buyer = _client('l_b', wallet=Decimal('1000'))
        seller = _client('l_s')
        bid = self._bid(buyer, qty=2, winner=seller, status='escrow_held')
        EscrowLedger.objects.create(
            client=buyer, bidding_request=bid, transaction_type='hold',
            amount=Decimal('205.00'), description='legacy hold',
        )
        bid.status = 'shipped'
        bid.save(update_fields=['status'])
        bid.trigger_release_to_seller()
        buyer.refresh_from_db(); seller.refresh_from_db()
        self.assertEqual(buyer.escrow_held, Decimal('0.00'))
        self.assertEqual(buyer.wallet_balance, Decimal('1000') - Decimal('205') + Decimal('5'))
        self.assertEqual(seller.wallet_balance, Decimal('195.00'))  # 200 - 2.5%

    def test_refund_returns_everything_that_was_held(self):
        buyer = _client('f_b', wallet=Decimal('1000'))
        bid = self._bid(buyer, qty=4)
        bid.trigger_escrow_hold()
        bid.trigger_refund_to_buyer()
        buyer.refresh_from_db()
        self.assertEqual(buyer.escrow_held, Decimal('0.00'))
        self.assertEqual(buyer.wallet_balance, Decimal('1000.00'))


class AwardTaskTests(TransactionTestCase):
    def test_award_task_holds_escrow_instead_of_crashing(self):
        from clients.tasks import process_ai_bidding_award
        buyer = _client('a_b', wallet=Decimal('1000'))
        s1, s2 = _client('a_s1'), _client('a_s2')
        bid = BlindBiddingRequest.objects.create(
            buyer=buyer, part_number='P-AW', required_qty=2,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        BidOffer.objects.create(bidding_request=bid, seller=s1, offer_price=Decimal('150'))
        BidOffer.objects.create(bidding_request=bid, seller=s2, offer_price=Decimal('120'))

        process_ai_bidding_award.apply(args=[bid.pk])

        bid.refresh_from_db(); buyer.refresh_from_db()
        self.assertEqual(bid.status, 'escrow_held')
        self.assertIsNotNone(bid.winner_id)
        self.assertEqual(buyer.escrow_held, bid.winning_price * 2)
        self.assertEqual(BidOffer.objects.filter(bidding_request=bid, is_winner=True).count(), 1)

    def test_award_waits_when_buyer_cannot_cover(self):
        from clients.tasks import process_ai_bidding_award
        buyer = _client('w_b', wallet=Decimal('10'))
        seller = _client('w_s')
        bid = BlindBiddingRequest.objects.create(
            buyer=buyer, part_number='P-W', required_qty=1,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        BidOffer.objects.create(bidding_request=bid, seller=seller, offer_price=Decimal('500'))
        process_ai_bidding_award.apply(args=[bid.pk])
        bid.refresh_from_db()
        self.assertEqual(bid.status, 'awarding')
        self.assertEqual(bid.winner_id, seller.pk)
