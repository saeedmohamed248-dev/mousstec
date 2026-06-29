"""UniversalSmartOrchestrator — state machine + auto-backup/rollback guard.

Pure async unit tests (no DB, no hardware) driving the orchestrator through
both branches and every failure/rollback path. The MockUniversalEcuIo records
its call order and the bytes it was asked to restore, so we can assert the
invariant: nothing is coded/flashed before a backup exists, and any abort or
error after the backup can be rolled back to the exact saved snapshot.
"""
from __future__ import annotations

import asyncio
import unittest

from bmw_ecu.universal import (
    MockUniversalEcuIo,
    UniversalSmartOrchestrator,
    UState,
    infer_topology,
)


def _run(coro):
    return asyncio.run(coro)


class TopologyTests(unittest.TestCase):
    def test_enet_maps_to_fseries_fem(self) -> None:
        self.assertEqual(infer_topology("doip"), ("F/G-Series", "FEM"))

    def test_kdcan_maps_to_rseries_cas(self) -> None:
        self.assertEqual(infer_topology("kdcan"), ("R/E-Series", "CAS"))

    def test_unknown_is_safe_default(self) -> None:
        self.assertEqual(infer_topology("weird")[1], "FEM")


class UnlockedFlowTests(unittest.TestCase):
    def _orch(self, **kw):
        self.sink_calls = []

        async def sink(b):
            self.sink_calls.append(b)

        io = MockUniversalEcuIo(transport_kind="doip", dme_locked=False, **kw)
        return UniversalSmartOrchestrator(io=io, backup_sink=sink), io

    def test_happy_path_detect_backup_code_sync(self) -> None:
        orch, io = self._orch()
        p = _run(orch.handle("start"))
        self.assertEqual(orch.state, UState.DETECTED)
        self.assertEqual(p.payload["body_module"], "FEM")
        self.assertEqual(p.expects, "BACKUP")

        p = _run(orch.handle("backup"))
        self.assertEqual(orch.state, UState.BACKED_UP)
        self.assertEqual(p.expects, "CODE")
        self.assertTrue(p.payload["backup_sha256"])
        self.assertEqual(len(self.sink_calls), 1)         # persisted once

        p = _run(orch.handle("code", {"options": {"a": 1, "b": 2}}))
        self.assertEqual(orch.state, UState.CODED)
        self.assertEqual(p.expects, "SYNC")

        p = _run(orch.handle("sync"))
        self.assertEqual(orch.state, UState.DONE)
        self.assertTrue(p.is_terminal)
        # Backup was taken BEFORE coding.
        self.assertLess(io.calls.index("read_coding_snapshot"),
                        io.calls.index("code_dme"))

    def test_clear_dtcs_runs_after_backup_before_coding(self) -> None:
        orch, io = self._orch()
        _run(orch.handle("start"))
        _run(orch.handle("backup"))
        p = _run(orch.handle("code", {"options": {"a": 1}}))
        self.assertTrue(p.payload["dtcs_cleared"])
        # CLEAR_DTCS_PRE_CODING ordering: backup → clear DTCs → code.
        self.assertLess(io.calls.index("read_coding_snapshot"),
                        io.calls.index("clear_dtcs"))
        self.assertLess(io.calls.index("clear_dtcs"),
                        io.calls.index("code_dme"))

    def test_cannot_code_before_backup(self) -> None:
        orch, _ = self._orch()
        _run(orch.handle("start"))
        p = _run(orch.handle("code", {"options": {"x": 1}}))
        self.assertTrue(p.is_error)
        self.assertEqual(orch.state, UState.FAILED)

    def test_rollback_button_present_after_backup(self) -> None:
        orch, _ = self._orch()
        _run(orch.handle("start"))
        p = _run(orch.handle("backup"))
        events = [a.event for a in p.actions]
        self.assertIn("rollback", events)


class RollbackTests(unittest.TestCase):
    def _orch(self, **kw):
        io = MockUniversalEcuIo(transport_kind="doip", dme_locked=False,
                                snapshot=b"ORIGINAL_STATE", **kw)
        return UniversalSmartOrchestrator(io=io), io

    def test_error_during_coding_offers_rollback_then_restores(self) -> None:
        orch, io = self._orch(fail_on="code_dme")
        _run(orch.handle("start"))
        _run(orch.handle("backup"))
        # Mutate live state then fail the coding.
        p = _run(orch.handle("code", {"options": {"x": 1}}))
        self.assertTrue(p.is_error)
        self.assertEqual(p.expects, "ROLLBACK")
        self.assertIn("rollback", [a.event for a in p.actions])

        p = _run(orch.handle("rollback"))
        self.assertEqual(orch.state, UState.ROLLED_BACK)
        self.assertTrue(p.is_terminal)
        # The exact saved bytes were written back to the ECU.
        self.assertEqual(io.restored_with, b"ORIGINAL_STATE")

    def test_abort_after_backup_offers_rollback(self) -> None:
        orch, io = self._orch()
        _run(orch.handle("start"))
        _run(orch.handle("backup"))
        p = _run(orch.handle("abort"))
        self.assertTrue(p.is_error)
        self.assertIn("rollback", [a.event for a in p.actions])
        # Not restored until the tech actually clicks rollback.
        self.assertIsNone(io.restored_with)

        _run(orch.handle("rollback"))
        self.assertEqual(io.restored_with, b"ORIGINAL_STATE")

    def test_abort_before_backup_is_clean_close_no_restore(self) -> None:
        orch, io = self._orch()
        _run(orch.handle("start"))
        p = _run(orch.handle("abort"))
        self.assertTrue(p.is_terminal)
        self.assertEqual(orch.state, UState.FAILED)
        self.assertIsNone(io.restored_with)

    def test_rollback_is_idempotent(self) -> None:
        orch, io = self._orch()
        _run(orch.handle("start"))
        _run(orch.handle("backup"))
        _run(orch.handle("rollback"))
        first = io.restored_with
        p = _run(orch.handle("rollback"))
        self.assertEqual(orch.state, UState.ROLLED_BACK)
        self.assertTrue(p.is_terminal)
        # write_coding_snapshot not called a second time.
        self.assertEqual(io.calls.count("write_coding_snapshot"), 1)
        self.assertEqual(io.restored_with, first)


class LockedBenchFlowTests(unittest.TestCase):
    _PINOUT = {"power_pin": 87, "ground_pin": 88, "boot_pin": 24,
               "callouts": [{"pin": 24, "label": "BOOT"}]}

    def test_locked_halts_for_bench_with_pinout_then_extracts(self) -> None:
        io = MockUniversalEcuIo(transport_kind="kdcan", dme_locked=True,
                                vin="WMWWORK00R56MINI1", pinout=self._PINOUT)
        orch = UniversalSmartOrchestrator(io=io)

        p = _run(orch.handle("start"))
        self.assertEqual(p.payload["body_module"], "CAS")   # R-series → CAS

        p = _run(orch.handle("backup"))
        # Locked → auto-halt for bench, pinout surfaced.
        self.assertEqual(orch.state, UState.BENCH_HALTED)
        self.assertTrue(p.payload["has_pinout"])
        self.assertEqual(p.payload["pinout"]["boot_pin"], 24)
        self.assertEqual(p.expects, "READY")

        p = _run(orch.handle("ready"))
        self.assertEqual(orch.state, UState.BENCH_READY)

        p = _run(orch.handle("extract"))
        self.assertEqual(orch.state, UState.EXTRACTED)
        self.assertEqual(p.expects, "SYNC")

        p = _run(orch.handle("sync"))
        self.assertEqual(orch.state, UState.DONE)
        self.assertTrue(p.is_terminal)

    def test_locked_without_pinout_refuses_to_guess(self) -> None:
        io = MockUniversalEcuIo(transport_kind="kdcan", dme_locked=True,
                                pinout=None)
        orch = UniversalSmartOrchestrator(io=io)
        _run(orch.handle("start"))
        p = _run(orch.handle("backup"))
        self.assertEqual(orch.state, UState.BENCH_HALTED)
        self.assertFalse(p.payload["has_pinout"])
        self.assertEqual(p.expects, "register_board")
        # No READY action offered — you must register a confirmed board first.
        self.assertNotIn("ready", [a.event for a in p.actions])

    def test_rollback_available_mid_bench(self) -> None:
        io = MockUniversalEcuIo(transport_kind="kdcan", dme_locked=True,
                                pinout=self._PINOUT, snapshot=b"LOCKED_ORIG")
        orch = UniversalSmartOrchestrator(io=io)
        _run(orch.handle("start"))
        _run(orch.handle("backup"))
        _run(orch.handle("ready"))
        p = _run(orch.handle("rollback"))
        self.assertEqual(orch.state, UState.ROLLED_BACK)
        self.assertEqual(io.restored_with, b"LOCKED_ORIG")


class SnapshotResumeTests(unittest.TestCase):
    def test_snapshot_roundtrips_state_and_data(self) -> None:
        io = MockUniversalEcuIo(transport_kind="doip", dme_locked=False)
        orch = UniversalSmartOrchestrator(io=io)
        _run(orch.handle("start"))
        _run(orch.handle("backup"))
        snap = orch.snapshot()

        resumed = UniversalSmartOrchestrator.restore(io=io, snapshot=snap,
                                                     backup=orch._backup)
        self.assertEqual(resumed.state, UState.BACKED_UP)
        self.assertEqual(resumed.data.vin, orch.data.vin)
        self.assertEqual(resumed.data.backup_sha256, orch.data.backup_sha256)
        # Resumed instance can still code through to DONE.
        _run(resumed.handle("code", {"options": {"z": 9}}))
        p = _run(resumed.handle("sync"))
        self.assertEqual(resumed.state, UState.DONE)
        self.assertTrue(p.is_terminal)


if __name__ == "__main__":
    unittest.main()
