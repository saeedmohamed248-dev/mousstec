"""Used-DME swap (CAS↔DME ISN alignment) orchestrator tests.

Pure-unit (no Django/tenant DB): drives the state machine with the Mock
provider and asserts the safety-critical ordering + honest failure branches:
  • backup ALWAYS happens before the ISN write;
  • a virgin CAS read is refused (never written as a real ISN);
  • a verify mismatch fails and points at the backup;
  • any provider error degrades to an honest FAILED prompt, not a fake success.
"""
from __future__ import annotations

import asyncio
import unittest

from bmw_ecu.isn.dme_swap_orchestrator import (
    DME_SWAP_PROFILES,
    DmeSwapOrchestrator,
    MockDmeSwapProvider,
    SwapEvent,
    SwapState,
)

PKEY = "R56_N18_MEVD17"
VIN = "WMWXX_TEST_000001"


def _run(coro):
    return asyncio.run(coro)


def _drive_to_aligned(orch: DmeSwapOrchestrator) -> None:
    _run(orch.handle(SwapEvent.SELECT_PROFILE, {"profile_key": PKEY, "vin": VIN}))
    _run(orch.handle(SwapEvent.READ_CAS_ISN))
    _run(orch.handle(SwapEvent.BACKUP_DME))
    _run(orch.handle(SwapEvent.WRITE_DME_ISN))
    _run(orch.handle(SwapEvent.VERIFY))
    _run(orch.handle(SwapEvent.ALIGN))


class HappyPathTests(unittest.TestCase):
    def test_full_flow_reaches_done(self) -> None:
        prov = MockDmeSwapProvider()
        orch = DmeSwapOrchestrator(prov)
        _drive_to_aligned(orch)
        final = _run(orch.handle(SwapEvent.FINISH))
        self.assertEqual(orch.state, SwapState.DONE)
        self.assertTrue(final.is_terminal)
        self.assertFalse(final.is_error)
        self.assertEqual(final.progress_pct, 100)

    def test_backup_strictly_precedes_write(self) -> None:
        prov = MockDmeSwapProvider()
        orch = DmeSwapOrchestrator(prov)
        _drive_to_aligned(orch)
        self.assertLess(prov.calls.index("backup_dme"),
                        prov.calls.index("write_dme_isn"))
        # And the exact real-world order is preserved end to end.
        self.assertEqual(
            prov.calls,
            ["read_cas_isn", "backup_dme", "write_dme_isn",
             "verify_dme_isn", "align_ews"],
        )

    def test_profile_marks_bench_required_for_mevd17(self) -> None:
        # N18 DME ISN write is bench-only — the flow must say so, not pretend OBD.
        self.assertTrue(DME_SWAP_PROFILES[PKEY].requires_bench)
        prov = MockDmeSwapProvider()
        orch = DmeSwapOrchestrator(prov)
        _run(orch.handle(SwapEvent.SELECT_PROFILE, {"profile_key": PKEY, "vin": VIN}))
        p = _run(orch.handle(SwapEvent.READ_CAS_ISN))
        self.assertTrue(p.payload["requires_bench"])


class FailureBranchTests(unittest.TestCase):
    def test_virgin_cas_read_is_refused(self) -> None:
        prov = MockDmeSwapProvider(fail_read=True)  # returns all-zero ISN
        orch = DmeSwapOrchestrator(prov)
        _run(orch.handle(SwapEvent.SELECT_PROFILE, {"profile_key": PKEY, "vin": VIN}))
        p = _run(orch.handle(SwapEvent.READ_CAS_ISN))
        self.assertEqual(orch.state, SwapState.FAILED)
        self.assertEqual(p.payload["error_code"], "virgin_isn")
        # Never proceeded to a write.
        self.assertNotIn("write_dme_isn", prov.calls)

    def test_write_without_backup_is_impossible(self) -> None:
        # Even if a caller skips ahead, WRITE before BACKUP is an illegal
        # transition → FAILED, never a silent write.
        prov = MockDmeSwapProvider()
        orch = DmeSwapOrchestrator(prov)
        _run(orch.handle(SwapEvent.SELECT_PROFILE, {"profile_key": PKEY, "vin": VIN}))
        _run(orch.handle(SwapEvent.READ_CAS_ISN))
        p = _run(orch.handle(SwapEvent.WRITE_DME_ISN))  # skipped BACKUP_DME
        self.assertEqual(orch.state, SwapState.FAILED)
        self.assertEqual(p.payload["error_code"], "illegal_transition")
        self.assertNotIn("write_dme_isn", prov.calls)

    def test_verify_mismatch_points_at_backup(self) -> None:
        prov = MockDmeSwapProvider(corrupt_verify=True)
        orch = DmeSwapOrchestrator(prov)
        _run(orch.handle(SwapEvent.SELECT_PROFILE, {"profile_key": PKEY, "vin": VIN}))
        _run(orch.handle(SwapEvent.READ_CAS_ISN))
        _run(orch.handle(SwapEvent.BACKUP_DME))
        _run(orch.handle(SwapEvent.WRITE_DME_ISN))
        p = _run(orch.handle(SwapEvent.VERIFY))
        self.assertEqual(orch.state, SwapState.FAILED)
        self.assertEqual(p.payload["error_code"], "verify_mismatch")
        self.assertTrue(p.payload["backup_ref"])  # backup is still available

    def test_provider_error_degrades_to_failed(self) -> None:
        prov = MockDmeSwapProvider(fail_write=True)
        orch = DmeSwapOrchestrator(prov)
        _run(orch.handle(SwapEvent.SELECT_PROFILE, {"profile_key": PKEY, "vin": VIN}))
        _run(orch.handle(SwapEvent.READ_CAS_ISN))
        _run(orch.handle(SwapEvent.BACKUP_DME))
        p = _run(orch.handle(SwapEvent.WRITE_DME_ISN))
        self.assertEqual(orch.state, SwapState.FAILED)
        self.assertEqual(p.payload["error_code"], "provider_error")

    def test_align_failure_after_verify(self) -> None:
        prov = MockDmeSwapProvider(fail_align=True)
        orch = DmeSwapOrchestrator(prov)
        _run(orch.handle(SwapEvent.SELECT_PROFILE, {"profile_key": PKEY, "vin": VIN}))
        _run(orch.handle(SwapEvent.READ_CAS_ISN))
        _run(orch.handle(SwapEvent.BACKUP_DME))
        _run(orch.handle(SwapEvent.WRITE_DME_ISN))
        _run(orch.handle(SwapEvent.VERIFY))
        p = _run(orch.handle(SwapEvent.ALIGN))
        self.assertEqual(orch.state, SwapState.FAILED)
        self.assertEqual(p.payload["error_code"], "provider_error")


class ZgwGatewayTests(unittest.TestCase):
    def test_zgw_gateway_is_recorded_and_surfaced(self) -> None:
        # A ZGW bench rig bridges OBD → PT-CAN; the flow must record it and
        # tell the technician the read is gateway-routed, not direct OBD.
        prov = MockDmeSwapProvider()
        orch = DmeSwapOrchestrator(prov)
        p = _run(orch.handle(SwapEvent.SELECT_PROFILE,
                             {"profile_key": PKEY, "vin": VIN, "gateway": "zgw"}))
        self.assertEqual(orch.data.gateway, "ZGW")
        self.assertEqual(p.payload["gateway"], "ZGW")
        self.assertIn("ZGW", p.body)

    def test_no_gateway_defaults_to_direct_obd(self) -> None:
        prov = MockDmeSwapProvider()
        orch = DmeSwapOrchestrator(prov)
        p = _run(orch.handle(SwapEvent.SELECT_PROFILE,
                             {"profile_key": PKEY, "vin": VIN}))
        self.assertEqual(orch.data.gateway, "")
        self.assertIn("OBD", p.body)

    def test_gateway_survives_snapshot_restore(self) -> None:
        prov = MockDmeSwapProvider()
        orch = DmeSwapOrchestrator(prov)
        _run(orch.handle(SwapEvent.SELECT_PROFILE,
                        {"profile_key": PKEY, "vin": VIN, "gateway": "ZGW"}))
        resumed = DmeSwapOrchestrator.restore(MockDmeSwapProvider(), orch.snapshot())
        self.assertEqual(resumed.data.gateway, "ZGW")


class SerialisationTests(unittest.TestCase):
    def test_snapshot_restore_resumes_mid_flow(self) -> None:
        prov = MockDmeSwapProvider()
        orch = DmeSwapOrchestrator(prov)
        _run(orch.handle(SwapEvent.SELECT_PROFILE, {"profile_key": PKEY, "vin": VIN}))
        _run(orch.handle(SwapEvent.READ_CAS_ISN))
        snap = orch.snapshot()

        resumed = DmeSwapOrchestrator.restore(MockDmeSwapProvider(), snap)
        self.assertEqual(resumed.state, SwapState.CAS_ISN_READ)
        self.assertEqual(resumed.data.cas_isn_hex, orch.data.cas_isn_hex)
        # Resume continues cleanly to DONE.
        _run(resumed.handle(SwapEvent.BACKUP_DME))
        _run(resumed.handle(SwapEvent.WRITE_DME_ISN))
        _run(resumed.handle(SwapEvent.VERIFY))
        _run(resumed.handle(SwapEvent.ALIGN))
        final = _run(resumed.handle(SwapEvent.FINISH))
        self.assertEqual(resumed.state, SwapState.DONE)
        self.assertTrue(final.is_terminal)


if __name__ == "__main__":
    unittest.main()
