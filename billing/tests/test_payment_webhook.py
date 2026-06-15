"""Regression coverage for ``universal_webhook_multiplexer``.

This is the endpoint a payment provider calls after a customer's
top-up clears. Every successful call moves real money into a
tenant's wallet — a regression here is straight-up cash leaving
the platform's balance sheet, or a customer's deposit landing on
the wrong side of a fraud hold.

Two production bugs are pinned by tests in this file:

1. Double-credit: the deposit branch manually does
   ``tenant.wallet_balance = F('wallet_balance') + amount`` *and*
   then creates an EscrowLedger row whose post_save signal does the
   same F-increment a second time. Net effect: every webhook
   credited the tenant **twice**. A 1,000 EGP top-up deposited
   2,000 EGP. Demonstrated by
   ``test_normal_deposit_credits_wallet_exactly_once``.

2. AML hold storm: the >100k branch creates an EscrowLedger row of
   type 'hold' on a wallet that has not yet been credited. The
   pre_save guard refuses the hold ("الرصيد المتاح لا يكفي") so the
   atomic block aborts and the 500 response loses the deposit
   entirely. Demonstrated by
   ``test_aml_large_deposit_is_held_not_lost``.

The tests stub ``WEBHOOK_HMAC_SECRET`` and forge a valid signature so
they don't have to assert HMAC behavior — HMAC is correctness-tested
implicitly because the wrong secret would 403 before the bug paths
fire.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal

from django.test import RequestFactory, TransactionTestCase, override_settings

from clients.models import Client, EscrowLedger
from clients.views.webhook_views import universal_webhook_multiplexer

WEBHOOK_SECRET = 'test-secret-32chars-or-whatever-X'


def _make_tenant(*, wallet=Decimal('0'), suffix='wh'):
    c = Client(
        schema_name=f'wh_{suffix}',
        name=f'Webhook {suffix}',
        owner_name='Owner',
        phone='01000000000',
        wallet_balance=wallet,
    )
    c.auto_create_schema = False
    c.save()
    return c


def _signed_payload(*, body: dict, secret: str = WEBHOOK_SECRET) -> tuple[bytes, str]:
    raw = json.dumps(body).encode('utf-8')
    sig = hmac.new(secret.encode('utf-8'), raw, hashlib.sha256).hexdigest()
    return raw, sig


@override_settings(WEBHOOK_HMAC_SECRET=WEBHOOK_SECRET)
class PaymentWebhookTests(TransactionTestCase):
    """Each test posts a fresh signed payload through the real view
    function so HMAC + idempotency + the balance write all happen."""

    def _post(self, body: dict):
        raw, sig = _signed_payload(body=body)
        req = RequestFactory().post(
            '/webhook/',
            data=raw,
            content_type='application/json',
        )
        req.META['HTTP_X_WEBHOOK_SIGNATURE'] = sig
        return universal_webhook_multiplexer(req)

    def test_normal_deposit_credits_wallet_exactly_once(self):
        """A 1,000 EGP top-up must add 1,000 to wallet_balance —
        not 2,000.

        Pre-fix: the view did BOTH ``F('wallet_balance') + amount``
        on the tenant AND created a 'deposit' EscrowLedger row
        whose post_save signal repeated the same +amount. Pinning
        the value at exactly the deposit amount prevents the
        regression from coming back."""
        tenant = _make_tenant(wallet=Decimal('0'), suffix='dep1')

        resp = self._post({
            'id': 'evt-1',
            'type': 'payment_intent.succeeded',
            'data': {
                'amount_received': 100000,  # 1,000 EGP in piasters
                'metadata': {'client_id': tenant.pk},
            },
        })
        self.assertEqual(resp.status_code, 200)

        tenant.refresh_from_db()
        self.assertEqual(
            tenant.wallet_balance, Decimal('1000.00'),
            'Wallet must be credited exactly once. Two += amount '
            'paths (manual update + signal) means double-credit.',
        )
        # Exactly one ledger row, of type 'deposit'.
        rows = EscrowLedger.objects.filter(client=tenant)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().transaction_type, 'deposit')

    def test_aml_large_deposit_is_held_not_lost(self):
        """A deposit over the AML threshold (100,000 EGP) must be
        recorded AND held — the tenant's spendable wallet stays at
        zero, the escrow_held column carries the locked amount, and
        the row count is observable for the fraud team.

        Pre-fix: the view created a 'hold' EscrowLedger row directly
        on an empty wallet. The pre_save guard refused the row
        ("الرصيد المتاح لا يكفي") so the atomic block aborted and
        the deposit silently disappeared — webhook returned 500,
        provider retried, idempotency cache had nothing because
        the cache.set never ran. Money lost.
        """
        tenant = _make_tenant(wallet=Decimal('0'), suffix='aml')

        resp = self._post({
            'id': 'evt-aml-1',
            'type': 'payment_intent.succeeded',
            'data': {
                'amount_received': 15_000_000,  # 150,000 EGP
                'metadata': {'client_id': tenant.pk},
            },
        })
        self.assertEqual(resp.status_code, 200)

        tenant.refresh_from_db()
        self.assertEqual(
            tenant.escrow_held, Decimal('150000.00'),
            'Large deposit must be parked in escrow_held for review.',
        )
        self.assertEqual(
            tenant.wallet_balance, Decimal('0.00'),
            'Spendable wallet must NOT reflect the funds yet.',
        )
        self.assertTrue(
            tenant.is_fraud_flagged,
            'Tenant must be flagged for the fraud-shield middleware.',
        )

    def test_duplicate_event_is_idempotent(self):
        """If the provider retries the same event_id, the wallet must
        not be credited a second time. The view caches the event_id
        in cache.set for 24h after a successful post; second call
        returns {'status': 'duplicate'} without touching the DB."""
        tenant = _make_tenant(wallet=Decimal('0'), suffix='dup')

        body = {
            'id': 'evt-dup-1',
            'type': 'payment_intent.succeeded',
            'data': {
                'amount_received': 50000,  # 500 EGP
                'metadata': {'client_id': tenant.pk},
            },
        }
        self._post(body)
        self._post(body)  # replay

        tenant.refresh_from_db()
        self.assertEqual(tenant.wallet_balance, Decimal('500.00'))
        self.assertEqual(EscrowLedger.objects.filter(client=tenant).count(), 1)

    def test_bad_signature_is_rejected(self):
        """Wrong HMAC must hit 403 before any balance write happens."""
        tenant = _make_tenant(wallet=Decimal('0'), suffix='badhmac')
        raw, _real_sig = _signed_payload(body={
            'id': 'evt-bad-1',
            'type': 'payment_intent.succeeded',
            'data': {
                'amount_received': 10000,
                'metadata': {'client_id': tenant.pk},
            },
        })
        req = RequestFactory().post(
            '/webhook/', data=raw, content_type='application/json',
        )
        req.META['HTTP_X_WEBHOOK_SIGNATURE'] = 'a' * 64  # bogus
        resp = universal_webhook_multiplexer(req)
        self.assertEqual(resp.status_code, 403)

        tenant.refresh_from_db()
        self.assertEqual(tenant.wallet_balance, Decimal('0.00'))
        self.assertEqual(EscrowLedger.objects.filter(client=tenant).count(), 0)
