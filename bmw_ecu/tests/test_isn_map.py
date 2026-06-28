"""Per-family ISN access mapping + guarded extraction (#2)."""
from __future__ import annotations

import asyncio
import unittest

from bmw_ecu.execution.ecu_profiles import KNOWN_PROFILES
from bmw_ecu.isn import (
    IsnExtractor,
    IsnNotOverUds,
    IsnSpecUnverified,
    get_isn_spec,
    isn_spec_for_profile,
)
from bmw_ecu.mocks import MockEcu, MockTransport
from bmw_ecu.uds import MockSeedKeyProvider, SecurityAccess, UdsClient


class IsnMapTests(unittest.TestCase):
    def test_fem_spec_is_level_5_uds(self) -> None:
        spec = get_isn_spec("FEM")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.security_level, 0x05)
        self.assertTrue(spec.over_uds)
        self.assertFalse(spec.verified)  # placeholder until confirmed

    def test_mevd17_isn_is_bench_only(self) -> None:
        spec = get_isn_spec("MEVD17")
        self.assertFalse(spec.over_uds)  # N20 DME → BDM/bench, not RDBI

    def test_spec_for_profile_falls_back_to_profile_fields(self) -> None:
        prof = KNOWN_PROFILES["FEM_F30"]
        spec = isn_spec_for_profile(prof)
        self.assertEqual(spec.family, "FEM")
        self.assertEqual(spec.security_level, 0x05)


class GuardedExtractionTests(unittest.TestCase):
    def _extractor(self):
        ecu = MockEcu(vin="WBA_ISN_TEST_0001")
        transport = MockTransport(ecu)
        client = UdsClient(transport, ecu_addr=0x40, session_name="t")
        security = SecurityAccess(client, MockSeedKeyProvider())
        return transport, IsnExtractor(client, security)

    def test_unverified_spec_is_refused_by_default(self) -> None:
        async def go():
            transport, ex = self._extractor()
            await transport.open()
            # FEM spec is unverified → must refuse without the opt-in.
            await ex.extract_for_profile(
                vin="WBA_ISN_TEST_0001", profile=KNOWN_PROFILES["FEM_F30"])

        with self.assertRaises(IsnSpecUnverified):
            asyncio.run(go())

    def test_bench_only_family_refused_over_uds(self) -> None:
        async def go():
            transport, ex = self._extractor()
            await transport.open()
            await ex.extract_for_profile(
                vin="x", profile=KNOWN_PROFILES["MEVD17_2_9"],
                allow_unverified=True)

        with self.assertRaises(IsnNotOverUds):
            asyncio.run(go())

    def test_opt_in_unverified_reads_isn_from_mock(self) -> None:
        async def go():
            transport, ex = self._extractor()
            await transport.open()
            return await ex.extract_for_profile(
                vin="WBA_ISN_TEST_0001", profile=KNOWN_PROFILES["FEM_F30"],
                allow_unverified=True)

        isn = asyncio.run(go())
        self.assertEqual(len(isn), 32)


if __name__ == "__main__":
    unittest.main()
