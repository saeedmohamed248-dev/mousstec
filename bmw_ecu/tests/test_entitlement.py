"""Entitlement verdict semantics + MockEntitlementProvider behaviour."""
from __future__ import annotations

import asyncio
import unittest

from bmw_ecu.services.entitlement import (
    EntitlementVerdict, MockEntitlementProvider, OperationType,
)


class MockEntitlementTests(unittest.TestCase):
    def test_entitled_path(self) -> None:
        prov = MockEntitlementProvider(
            entitled={("V1", OperationType.CODING)},
        )
        async def run() -> EntitlementVerdict:
            return await prov.verify(vin="V1", operation_type=OperationType.CODING)
        v = asyncio.run(run())
        self.assertTrue(v.entitled)
        self.assertEqual(v.mode, "subscription")

    def test_denied_path(self) -> None:
        prov = MockEntitlementProvider(
            denied={("V2", OperationType.CODING)},
        )
        async def run() -> EntitlementVerdict:
            return await prov.verify(vin="V2", operation_type=OperationType.CODING)
        v = asyncio.run(run())
        self.assertFalse(v.entitled)
        self.assertEqual(v.mode, "denied")

    def test_default_holds_idempotently(self) -> None:
        prov = MockEntitlementProvider()
        async def run() -> tuple[EntitlementVerdict, EntitlementVerdict]:
            a = await prov.verify(vin="V3", operation_type=OperationType.CODING)
            b = await prov.verify(vin="V3", operation_type=OperationType.CODING)
            return a, b
        a, b = asyncio.run(run())
        self.assertEqual(a.mode, "hold")
        self.assertEqual(a.hold_ref, b.hold_ref)  # idempotent

    def test_isn_and_coding_are_separate(self) -> None:
        prov = MockEntitlementProvider(
            entitled={("V4", OperationType.CODING)},
        )
        async def run():
            coding = await prov.verify(vin="V4", operation_type=OperationType.CODING)
            isn = await prov.verify(vin="V4", operation_type=OperationType.ISN)
            return coding, isn
        coding, isn = asyncio.run(run())
        self.assertTrue(coding.entitled)
        # ISN entry not whitelisted in mock → defaults to hold.
        self.assertFalse(isn.entitled)
        self.assertEqual(isn.mode, "hold")
