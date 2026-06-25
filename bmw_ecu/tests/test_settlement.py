"""Settlement-provider behaviour (no Django ORM needed for these paths).

We test the COMPOSITE provider's fall-through logic + outcome shape by
substituting fake wallet/paymob providers. The real WalletDeductProvider
+ PaymobInvoiceProvider have integration touchpoints (Client model,
billing.services.paymob) covered in Django-test land — see the
ApiOrchestrator capture flow tests for live coverage.
"""
from __future__ import annotations

import asyncio
import unittest
from decimal import Decimal

from bmw_ecu.services.settlement import (
    AbstractSettlementProvider,
    SettlementOutcome,
    WalletThenPaymobProvider,
)


class _FakeWallet(AbstractSettlementProvider):
    name = "wallet"

    def __init__(self, *, succeed: bool, have: Decimal = Decimal("0")) -> None:
        self.succeed = succeed
        self.have = have
        self.calls = 0

    async def settle(self, *, authorization_ref, vin, amount, currency,
                     tenant_schema=None):
        self.calls += 1
        if self.succeed:
            return SettlementOutcome(
                mode="wallet", succeeded=True, amount=amount,
                wallet_before=self.have, wallet_after=self.have - amount,
            )
        return SettlementOutcome(
            mode="wallet", succeeded=False, amount=amount,
            wallet_before=self.have, wallet_after=self.have,
            error_message=f"insufficient ({self.have} EGP)",
        )


class _FakePaymob(AbstractSettlementProvider):
    name = "paymob"

    def __init__(self) -> None:
        self.calls = 0

    async def settle(self, *, authorization_ref, vin, amount, currency,
                     tenant_schema=None):
        self.calls += 1
        return SettlementOutcome(
            mode="paymob", succeeded=True, amount=amount,
            paymob_iframe_url=f"https://paymob.test/iframe/{authorization_ref}",
        )


class CompositeProviderTests(unittest.TestCase):
    def _composite(self, wallet, paymob):
        c = WalletThenPaymobProvider()
        c.wallet = wallet
        c.paymob = paymob
        return c

    def test_wallet_succeeds_no_paymob_call(self) -> None:
        wallet = _FakeWallet(succeed=True, have=Decimal("1000"))
        paymob = _FakePaymob()
        comp = self._composite(wallet, paymob)

        async def run():
            return await comp.settle(
                authorization_ref="REF1", vin="V", amount=Decimal("450.00"),
                currency="EGP",
            )

        out = asyncio.run(run())
        self.assertTrue(out.succeeded)
        self.assertEqual(out.mode, "wallet")
        self.assertEqual(out.wallet_after, Decimal("550.00"))
        self.assertEqual(paymob.calls, 0)

    def test_wallet_fails_falls_back_to_paymob(self) -> None:
        wallet = _FakeWallet(succeed=False, have=Decimal("100"))
        paymob = _FakePaymob()
        comp = self._composite(wallet, paymob)

        async def run():
            return await comp.settle(
                authorization_ref="REF2", vin="V", amount=Decimal("450.00"),
                currency="EGP",
            )

        out = asyncio.run(run())
        self.assertTrue(out.succeeded)
        self.assertEqual(out.mode, "paymob")
        self.assertIn("paymob.test", out.paymob_iframe_url)
        # Paymob outcome inherits the wallet snapshot for accounting trail.
        self.assertEqual(out.wallet_before, Decimal("100"))
        self.assertEqual(out.wallet_after, Decimal("100"))
        self.assertEqual(wallet.calls, 1)
        self.assertEqual(paymob.calls, 1)

    def test_settlement_outcome_shape(self) -> None:
        """Quick contract test — every field the audit row will read exists."""
        out = SettlementOutcome(
            mode="paymob", succeeded=True, amount=Decimal("450"),
            paymob_iframe_url="https://x", error_message="",
        )
        self.assertEqual(out.amount, Decimal("450"))
        self.assertIsNone(out.wallet_before)


class ProviderResolverTests(unittest.TestCase):
    def test_default_is_composite(self) -> None:
        # Don't import Django settings — just verify the resolver string map.
        from bmw_ecu.services.settlement import (
            PaymobInvoiceProvider, WalletDeductProvider, WalletThenPaymobProvider,
        )
        self.assertEqual(WalletDeductProvider().name, "wallet")
        self.assertEqual(PaymobInvoiceProvider().name, "paymob")
        self.assertEqual(WalletThenPaymobProvider().name, "wallet_then_paymob")
