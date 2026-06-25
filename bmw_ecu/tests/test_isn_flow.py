"""Full ISN extract → inject → verify against the mock ECU."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from bmw_ecu.isn import IsnExtractor, IsnInjector
from bmw_ecu.mocks import MockEcu, MockTransport
from bmw_ecu.safety import BackupStore, BatteryMonitor, PreflightGate
from bmw_ecu.uds import MockSeedKeyProvider, SecurityAccess, UdsClient


class IsnFlowTests(unittest.TestCase):
    def test_extract_then_inject_into_fresh_ecu(self) -> None:
        async def run() -> None:
            # Source FEM
            fem = MockEcu(vin="WBA1234FEM000000")
            src_t = MockTransport(fem)
            await src_t.open()
            src_client = UdsClient(src_t, ecu_addr=0x40, session_name="src")
            sa_src = SecurityAccess(src_client, MockSeedKeyProvider())
            extractor = IsnExtractor(src_client, sa_src)
            isn = await extractor.extract(vin=fem.vin)
            self.assertEqual(len(isn), 32)

            # Target DME (fresh — different ISN)
            dme = MockEcu(vin="WBA1234FEM000000", isn=bytes(32))
            dme_t = MockTransport(dme)
            await dme_t.open()
            dme_client = UdsClient(dme_t, ecu_addr=0x12, session_name="dme")
            sa_dme = SecurityAccess(dme_client, MockSeedKeyProvider())

            with tempfile.TemporaryDirectory() as d:
                store = BackupStore(Path(d))
                async def v() -> float: return 13.6
                gate = PreflightGate(BatteryMonitor(reader=v), store)
                injector = IsnInjector(dme_client, sa_dme, gate)
                await injector.inject(vin=fem.vin, ecu_name="DME_N20", isn=isn)
            # Verify
            self.assertEqual(dme.isn, isn)

        asyncio.run(run())
