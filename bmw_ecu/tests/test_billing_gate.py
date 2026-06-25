"""Billing gate semantics — idempotency, capture, release, declines."""
from __future__ import annotations

import asyncio
import unittest
from decimal import Decimal

from bmw_ecu.exceptions import FeeAuthorizationDeclined
from bmw_ecu.services.billing_gate import (
    DEFAULT_FEE_EGP, MockBillingGate,
)


class MockBillingGateTests(unittest.TestCase):
    def test_default_fee_is_450_egp(self) -> None:
        self.assertEqual(DEFAULT_FEE_EGP, Decimal("450.00"))

    def test_authorize_then_capture(self) -> None:
        gate = MockBillingGate()
        async def run() -> None:
            a = await gate.authorize_diagnostic_fee(vin="V1")
            self.assertEqual(a.amount, DEFAULT_FEE_EGP)
            self.assertTrue(a.is_new)
            c = await gate.capture_fee(vin="V1")
            self.assertEqual(c.status, "captured")
        asyncio.run(run())

    def test_authorize_is_idempotent(self) -> None:
        gate = MockBillingGate()
        async def run() -> None:
            a1 = await gate.authorize_diagnostic_fee(vin="V1")
            a2 = await gate.authorize_diagnostic_fee(vin="V1")
            self.assertEqual(a1.authorization_ref, a2.authorization_ref)
            self.assertFalse(a2.is_new)
        asyncio.run(run())

    def test_release_marks_released(self) -> None:
        gate = MockBillingGate()
        async def run() -> None:
            await gate.authorize_diagnostic_fee(vin="V1")
            r = await gate.release_fee(vin="V1", reason="rolled back")
            self.assertEqual(r.status, "released")
        asyncio.run(run())

    def test_decline_list(self) -> None:
        gate = MockBillingGate(decline_vins={"BADVIN"})
        async def run() -> None:
            with self.assertRaises(FeeAuthorizationDeclined):
                await gate.authorize_diagnostic_fee(vin="BADVIN")
        asyncio.run(run())
