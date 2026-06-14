"""Regression coverage for the EscrowLedger signals.

These two signal handlers live in ``clients/models.py`` and form the
bookkeeping core of the platform wallet:

* ``validate_escrow_balance_before_save`` — pre_save guard that
  refuses any ``hold`` or ``withdrawal`` ledger row whose
  ``client.wallet_balance`` is below the amount.
* ``update_client_balances_on_ledger_entry`` — post_save handler
  that moves money between ``wallet_balance`` and ``escrow_held``
  atomically via ``F()`` expressions.

The risk profile is "bank-grade": a silent regression (e.g. a future
refactor that turns these into local closures the way signals_quota
once did) would let users create wallet entries without the
matching balance check, or skip the F()-based balance update,
producing real money discrepancies before anyone noticed.

Coverage strategy:

* Build a Client in the public schema with ``auto_create_schema =
  False`` so we don't spin up a tenant DB per test. EscrowLedger
  rows live in the public schema regardless.
* Use ``refresh_from_db()`` after every save so we observe what the
  signals committed, not the stale Python instance.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TransactionTestCase
from django.utils import timezone

from clients.models import BlindBiddingRequest, Client, EscrowLedger


class _EscrowTestBase(TransactionTestCase):
    """Skip the tenant schema-create dance — EscrowLedger only needs
    a Client row in public to operate on."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    def _make_client(self, *, wallet=Decimal('0'), held=Decimal('0'), suffix='a'):
        """Create a Client in public without provisioning a tenant
        schema. ``auto_create_schema = False`` keeps this fast — the
        Escrow signals only read/write the Client row itself.
        """
        c = Client(
            schema_name=f'escrow_test_{suffix}',
            name=f'Escrow Test {suffix}',
            owner_name='Test Owner',
            phone='01000000000',
            wallet_balance=wallet,
            escrow_held=held,
        )
        c.auto_create_schema = False
        c.save()
        return c


class EscrowPreSaveGuardTests(_EscrowTestBase):
    """``validate_escrow_balance_before_save`` must veto under-funded
    hold/withdrawal rows before they touch the database."""

    def test_hold_blocked_when_balance_below_amount(self):
        client = self._make_client(wallet=Decimal('50'), suffix='hold_blk')
        with self.assertRaises(ValidationError):
            EscrowLedger.objects.create(
                client=client,
                transaction_type='hold',
                amount=Decimal('100'),
                description='Should fail',
            )
        # Nothing must have shifted on the wallet.
        client.refresh_from_db()
        self.assertEqual(client.wallet_balance, Decimal('50.00'))
        self.assertEqual(client.escrow_held, Decimal('0.00'))

    def test_hold_passes_at_exact_balance(self):
        client = self._make_client(wallet=Decimal('100'), suffix='hold_exact')
        EscrowLedger.objects.create(
            client=client,
            transaction_type='hold',
            amount=Decimal('100'),
            description='exact match',
        )
        client.refresh_from_db()
        self.assertEqual(client.wallet_balance, Decimal('0.00'))
        self.assertEqual(client.escrow_held, Decimal('100.00'))

    def test_withdrawal_blocked_when_balance_below_amount(self):
        client = self._make_client(wallet=Decimal('20'), suffix='wd_blk')
        with self.assertRaises(ValidationError):
            EscrowLedger.objects.create(
                client=client,
                transaction_type='withdrawal',
                amount=Decimal('100'),
                description='Should fail',
            )
        client.refresh_from_db()
        self.assertEqual(client.wallet_balance, Decimal('20.00'))

    def test_deposit_is_never_blocked(self):
        """Deposits add money; there's no 'have enough' check to fail."""
        client = self._make_client(wallet=Decimal('0'), suffix='dep_zero')
        EscrowLedger.objects.create(
            client=client,
            transaction_type='deposit',
            amount=Decimal('500'),
            description='top-up',
        )
        client.refresh_from_db()
        self.assertEqual(client.wallet_balance, Decimal('500.00'))

    def test_update_path_is_not_revalidated(self):
        """Updating an existing ledger row must skip the balance
        check — only NEW entries are gated. (Anti-regression: if
        somebody removes the ``if instance.pk: return`` guard, every
        admin edit would start failing for over-spent rows.)"""
        client = self._make_client(wallet=Decimal('100'), suffix='upd')
        entry = EscrowLedger.objects.create(
            client=client,
            transaction_type='hold',
            amount=Decimal('100'),
            description='initial',
        )
        # Now the wallet is 0; editing the description must not
        # re-check the balance.
        entry.description = 'edited'
        entry.save()  # would raise if pre_save re-ran the guard


class EscrowPostSaveLedgerTests(_EscrowTestBase):
    """``update_client_balances_on_ledger_entry`` is the actual ledger.
    Every transaction_type must move money the way the docstring on
    each branch says it should."""

    def test_deposit_increases_wallet(self):
        client = self._make_client(wallet=Decimal('100'), suffix='dep')
        EscrowLedger.objects.create(
            client=client,
            transaction_type='deposit',
            amount=Decimal('250.50'),
            description='top-up',
        )
        client.refresh_from_db()
        self.assertEqual(client.wallet_balance, Decimal('350.50'))
        self.assertEqual(client.escrow_held, Decimal('0.00'))

    def test_hold_moves_wallet_to_escrow(self):
        client = self._make_client(wallet=Decimal('500'), suffix='hold')
        EscrowLedger.objects.create(
            client=client,
            transaction_type='hold',
            amount=Decimal('200'),
            description='auction lock',
        )
        client.refresh_from_db()
        self.assertEqual(client.wallet_balance, Decimal('300.00'))
        self.assertEqual(client.escrow_held, Decimal('200.00'))

    def test_refund_reverses_hold(self):
        """A refund must put escrow money back into the wallet —
        the inverse of a hold. End-to-end, hold-then-refund leaves
        the client exactly where they started."""
        client = self._make_client(wallet=Decimal('500'), suffix='refund')
        EscrowLedger.objects.create(
            client=client, transaction_type='hold',
            amount=Decimal('200'), description='hold first',
        )
        EscrowLedger.objects.create(
            client=client, transaction_type='refund',
            amount=Decimal('200'), description='auction cancelled',
        )
        client.refresh_from_db()
        self.assertEqual(client.wallet_balance, Decimal('500.00'))
        self.assertEqual(client.escrow_held, Decimal('0.00'))

    def test_withdrawal_decreases_wallet_only(self):
        """Withdrawals leave escrow alone; only the wallet shrinks."""
        client = self._make_client(
            wallet=Decimal('1000'), held=Decimal('300'), suffix='wd',
        )
        EscrowLedger.objects.create(
            client=client, transaction_type='withdrawal',
            amount=Decimal('400'), description='cash-out',
        )
        client.refresh_from_db()
        self.assertEqual(client.wallet_balance, Decimal('600.00'))
        self.assertEqual(client.escrow_held, Decimal('300.00'))

    def test_release_decreases_escrow_with_no_seller(self):
        """``release`` without a linked bidding_request must still
        reduce ``escrow_held``. The seller-payout branch is only for
        rows tied to an awarded auction."""
        client = self._make_client(
            wallet=Decimal('100'), held=Decimal('200'), suffix='rel_nosell',
        )
        EscrowLedger.objects.create(
            client=client, transaction_type='release',
            amount=Decimal('150'), description='manual release',
        )
        client.refresh_from_db()
        self.assertEqual(client.escrow_held, Decimal('50.00'))
        self.assertEqual(client.wallet_balance, Decimal('100.00'))

    def test_updates_do_not_double_post(self):
        """Re-saving an existing ledger row must NOT re-run the
        balance update — only the initial create should move money.
        Without this guard, every admin edit would double-credit."""
        client = self._make_client(wallet=Decimal('500'), suffix='nodbl')
        entry = EscrowLedger.objects.create(
            client=client, transaction_type='deposit',
            amount=Decimal('100'), description='one-time',
        )
        client.refresh_from_db()
        self.assertEqual(client.wallet_balance, Decimal('600.00'))

        entry.description = 'edited'
        entry.save()
        client.refresh_from_db()
        # Still 600 — not 700.
        self.assertEqual(client.wallet_balance, Decimal('600.00'))


class EscrowReleaseAuctionPayoutTests(_EscrowTestBase):
    """The ``release`` branch has two halves:

      1. The buyer's escrow_held shrinks by the released amount.
      2. If the EscrowLedger row is linked to an awarded
         BlindBiddingRequest (``bidding_request.winner_id`` set),
         the seller's wallet_balance grows by
         ``amount - platform_fee_collected``.

    The second half is the actual seller payout — the *single most
    money-moving* line in the whole platform. There is no test
    elsewhere that covers the cross-client transfer (buyer → seller)
    or the platform fee deduction. A silent bug here would either
    underpay sellers or skip platform commission silently.
    """

    def _make_bidding_request(
        self, *, buyer, winner, winning_price, fee=Decimal('0'),
    ):
        return BlindBiddingRequest.objects.create(
            buyer=buyer,
            part_number='X-001',
            required_qty=1,
            winning_price=winning_price,
            winner=winner,
            platform_fee_collected=fee,
            status='escrow_held',
            expires_at=timezone.now() + timedelta(days=1),
        )

    def test_release_pays_seller_net_of_platform_fee(self):
        buyer = self._make_client(
            wallet=Decimal('0'), held=Decimal('1000'), suffix='auc_buy',
        )
        seller = self._make_client(
            wallet=Decimal('200'), suffix='auc_sell',
        )
        bid = self._make_bidding_request(
            buyer=buyer, winner=seller,
            winning_price=Decimal('1000'), fee=Decimal('25'),
        )

        EscrowLedger.objects.create(
            client=buyer,
            bidding_request=bid,
            transaction_type='release',
            amount=Decimal('1000'),
            description='auction completed',
        )

        buyer.refresh_from_db()
        seller.refresh_from_db()
        # Buyer's hold is released.
        self.assertEqual(buyer.escrow_held, Decimal('0.00'))
        # Buyer's spendable wallet stays at 0 — release is NOT a
        # refund. Money goes to the seller, not back to the buyer.
        self.assertEqual(buyer.wallet_balance, Decimal('0.00'))
        # Seller gets winning_price - fee.
        self.assertEqual(seller.wallet_balance, Decimal('1175.00'))  # 200 + 975

    def test_release_with_zero_fee_pays_full_amount(self):
        """The default platform_fee_collected is 0 (column default).
        Make sure that case sends the full winning_price to the seller
        instead of accidentally subtracting None or '' from amount."""
        buyer = self._make_client(held=Decimal('500'), suffix='zfee_buy')
        seller = self._make_client(wallet=Decimal('0'), suffix='zfee_sell')
        bid = self._make_bidding_request(
            buyer=buyer, winner=seller,
            winning_price=Decimal('500'), fee=Decimal('0'),
        )

        EscrowLedger.objects.create(
            client=buyer, bidding_request=bid,
            transaction_type='release',
            amount=Decimal('500'),
            description='no-fee deal',
        )

        seller.refresh_from_db()
        self.assertEqual(seller.wallet_balance, Decimal('500.00'))

    def test_release_does_not_credit_when_no_winner(self):
        """If the auction was cancelled and a release happens for
        administrative reasons (refund-shaped release), there's no
        seller to pay. The signal must still decrement escrow_held
        without crashing on a NoneType winner_id."""
        buyer = self._make_client(held=Decimal('200'), suffix='nowin')
        bid = BlindBiddingRequest.objects.create(
            buyer=buyer,
            part_number='Y-9',
            required_qty=1,
            winning_price=Decimal('200'),
            winner=None,  # cancelled mid-flight
            status='cancelled',
            expires_at=timezone.now() + timedelta(days=1),
        )

        EscrowLedger.objects.create(
            client=buyer, bidding_request=bid,
            transaction_type='release',
            amount=Decimal('200'),
            description='manual release without award',
        )
        buyer.refresh_from_db()
        self.assertEqual(buyer.escrow_held, Decimal('0.00'))
