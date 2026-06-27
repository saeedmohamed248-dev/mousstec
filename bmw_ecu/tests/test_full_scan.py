"""Full-System Auto-Scan — pure-Python, zero DB, zero hardware.

Drives the FullScanOrchestrator through its mock provider so every state
transition, the traffic-light roll-up (GREEN / YELLOW / RED), the
missing/unreachable-module handling, the DTC severity classification,
and the entitlement gate are all asserted deterministically.
"""
from __future__ import annotations

import asyncio
import unittest

from bmw_ecu.scan import (
    DtcSeverity,
    FullScanOrchestrator,
    MockScanProvider,
    OverallStatus,
    ScanEvent,
    ScanState,
    decode_dtc,
    describe_module,
    expected_module_codes,
)
from bmw_ecu.scan.dtc_decoder import (
    STATUS_CONFIRMED,
    STATUS_PENDING,
    STATUS_WARNING_INDICATOR,
)
from bmw_ecu.services.entitlement_guard import MockEntitlementGuard


def _run(coro):
    return asyncio.run(coro)


_VIN = "WBA12345678901234"


# ─────────────────────────────────────────────────────────────────────
# DTC decoder
# ─────────────────────────────────────────────────────────────────────
class DtcDecoderTests(unittest.TestCase):
    def test_known_code_bilingual(self) -> None:
        d = decode_dtc("P0301", STATUS_CONFIRMED)
        self.assertEqual(d.code, "P0301")
        self.assertIn("السلندر", d.description_ar)
        self.assertIn("isfire", d.description_en)

    def test_unknown_code_category_fallback(self) -> None:
        d = decode_dtc("P1ABC", STATUS_CONFIRMED)
        self.assertTrue(d.description_ar)   # never blank
        self.assertTrue(d.description_en)
        self.assertEqual(d.severity, DtcSeverity.HARD)  # confirmed powertrain

    def test_confirmed_powertrain_is_hard(self) -> None:
        d = decode_dtc("P0171", STATUS_CONFIRMED)
        self.assertEqual(d.severity, DtcSeverity.HARD)
        self.assertTrue(d.is_confirmed)
        self.assertFalse(d.is_pending)

    def test_pending_only_is_soft(self) -> None:
        d = decode_dtc("P0171", STATUS_PENDING)
        self.assertEqual(d.severity, DtcSeverity.SOFT)
        self.assertTrue(d.is_pending)
        self.assertFalse(d.is_confirmed)

    def test_history_only_is_info(self) -> None:
        # No confirmed, no pending bits → informational history.
        d = decode_dtc("P0420", 0x00)
        self.assertEqual(d.severity, DtcSeverity.INFO)

    def test_safety_code_always_safety(self) -> None:
        d = decode_dtc("B1018", 0x00)   # even with no active bits
        self.assertEqual(d.severity, DtcSeverity.SAFETY)

    def test_confirmed_fault_in_safety_module_escalates(self) -> None:
        # A generic U-code becomes SAFETY when the module is the airbag.
        d = decode_dtc("U0100", STATUS_CONFIRMED,
                       module_is_safety_critical=True)
        self.assertEqual(d.severity, DtcSeverity.SAFETY)

    def test_warning_lamp_bit_decoded(self) -> None:
        d = decode_dtc("P0700", STATUS_CONFIRMED | STATUS_WARNING_INDICATOR)
        self.assertTrue(d.is_warning_lamp)


# ─────────────────────────────────────────────────────────────────────
# Module map
# ─────────────────────────────────────────────────────────────────────
class ModuleMapTests(unittest.TestCase):
    def test_expected_codes_per_chassis(self) -> None:
        g = expected_module_codes("g_series")
        self.assertIn("bdc", g)
        self.assertIn("acsm", g)
        e = expected_module_codes("e_series")
        self.assertIn("frm", e)
        self.assertIn("cas", e)

    def test_describe_unknown_module_fallback(self) -> None:
        m = describe_module("xyz999")
        self.assertEqual(m.code, "xyz999")
        self.assertIn("XYZ999", m.name_en)
        self.assertFalse(m.is_safety_critical)

    def test_known_module_safety_flag(self) -> None:
        self.assertTrue(describe_module("acsm").is_safety_critical)
        self.assertTrue(describe_module("dsc").is_safety_critical)
        self.assertFalse(describe_module("ihka").is_safety_critical)


# ─────────────────────────────────────────────────────────────────────
# Orchestrator — happy path + roll-up
# ─────────────────────────────────────────────────────────────────────
class FullScanFlowTests(unittest.TestCase):
    def _orch(self, provider, entitlement=None):
        return FullScanOrchestrator(provider=provider, entitlement=entitlement)

    def test_clean_car_is_green(self) -> None:
        prov = MockScanProvider(
            vin=_VIN,
            reachable=list(expected_module_codes("f_series")),
            faults={c: [] for c in expected_module_codes("f_series")},
        )
        orch = self._orch(prov)
        _run(orch.handle(ScanEvent.CONNECT, {"chassis_family": "f_series"}))
        p = _run(orch.handle(ScanEvent.SCAN_ALL))
        self.assertEqual(p.payload["overall"], OverallStatus.GREEN.value)
        self.assertEqual(p.payload["total_faults"], 0)
        self.assertEqual(p.state, ScanState.REPORT_READY)

    def test_pending_body_fault_is_yellow(self) -> None:
        codes = list(expected_module_codes("f_series"))
        faults = {c: [] for c in codes}
        faults["ihka"] = [("B3000", STATUS_PENDING)]   # pending, body → soft
        prov = MockScanProvider(vin=_VIN, reachable=codes, faults=faults)
        orch = self._orch(prov)
        _run(orch.handle(ScanEvent.CONNECT, {"chassis_family": "f_series"}))
        p = _run(orch.handle(ScanEvent.SCAN_ALL))
        self.assertEqual(p.payload["overall"], OverallStatus.YELLOW.value)
        self.assertEqual(p.payload["counts"]["soft"], 1)

    def test_confirmed_engine_fault_is_red(self) -> None:
        codes = list(expected_module_codes("f_series"))
        faults = {c: [] for c in codes}
        faults["dme"] = [("P0301", STATUS_CONFIRMED)]
        prov = MockScanProvider(vin=_VIN, reachable=codes, faults=faults)
        orch = self._orch(prov)
        _run(orch.handle(ScanEvent.CONNECT, {"chassis_family": "f_series"}))
        p = _run(orch.handle(ScanEvent.SCAN_ALL))
        self.assertEqual(p.payload["overall"], OverallStatus.RED.value)
        self.assertEqual(p.payload["counts"]["hard"], 1)

    def test_airbag_crash_data_is_red_safety(self) -> None:
        codes = list(expected_module_codes("f_series"))
        faults = {c: [] for c in codes}
        faults["acsm"] = [("B1018", STATUS_CONFIRMED)]
        prov = MockScanProvider(vin=_VIN, reachable=codes, faults=faults)
        orch = self._orch(prov)
        _run(orch.handle(ScanEvent.CONNECT, {"chassis_family": "f_series"}))
        p = _run(orch.handle(ScanEvent.SCAN_ALL))
        self.assertEqual(p.payload["overall"], OverallStatus.RED.value)
        self.assertEqual(p.payload["counts"]["safety"], 1)

    def test_unreachable_safety_module_forces_red(self) -> None:
        # ACSM expected for f_series but it didn't answer → RED even with
        # zero faults elsewhere.
        codes = [c for c in expected_module_codes("f_series") if c != "acsm"]
        prov = MockScanProvider(vin=_VIN, reachable=codes,
                                faults={c: [] for c in codes})
        orch = self._orch(prov)
        _run(orch.handle(ScanEvent.CONNECT, {"chassis_family": "f_series"}))
        p = _run(orch.handle(ScanEvent.SCAN_ALL))
        self.assertEqual(p.payload["overall"], OverallStatus.RED.value)
        # ACSM appears as an unreachable result row, not silently dropped.
        acsm_rows = [r for r in p.payload["results"]
                     if r["module"]["code"] == "acsm"]
        self.assertEqual(len(acsm_rows), 1)
        self.assertFalse(acsm_rows[0]["reachable"])

    def test_extra_uncatalogued_module_not_dropped(self) -> None:
        codes = list(expected_module_codes("f_series")) + ["wxyz"]
        prov = MockScanProvider(
            vin=_VIN, reachable=codes,
            faults={**{c: [] for c in codes}, "wxyz": [("U0200", STATUS_PENDING)]},
        )
        orch = self._orch(prov)
        _run(orch.handle(ScanEvent.CONNECT, {"chassis_family": "f_series"}))
        p = _run(orch.handle(ScanEvent.SCAN_ALL))
        rows = [r for r in p.payload["results"] if r["module"]["code"] == "wxyz"]
        self.assertEqual(len(rows), 1)

    def test_finish_emits_terminal(self) -> None:
        prov = MockScanProvider(vin=_VIN, reachable=["dme"], faults={"dme": []})
        orch = self._orch(prov)
        _run(orch.handle(ScanEvent.CONNECT, {"chassis_family": "f_series"}))
        _run(orch.handle(ScanEvent.SCAN_ALL))
        fin = _run(orch.handle(ScanEvent.FINISH))
        self.assertTrue(fin.is_terminal)
        self.assertEqual(fin.state, ScanState.DONE)
        self.assertEqual(fin.progress_pct, 100)


# ─────────────────────────────────────────────────────────────────────
# Orchestrator — failure + guard paths
# ─────────────────────────────────────────────────────────────────────
class FullScanFailureTests(unittest.TestCase):
    def test_gateway_down_fails_at_connect(self) -> None:
        prov = MockScanProvider(gateway_down=True)
        orch = FullScanOrchestrator(provider=prov)
        p = _run(orch.handle(ScanEvent.CONNECT, {"chassis_family": "f_series"}))
        self.assertTrue(p.is_error)
        self.assertEqual(orch.state, ScanState.FAILED)
        self.assertEqual(p.payload["error_code"], "transport_error")

    def test_scan_before_connect_is_illegal(self) -> None:
        prov = MockScanProvider()
        orch = FullScanOrchestrator(provider=prov)
        p = _run(orch.handle(ScanEvent.SCAN_ALL))
        self.assertTrue(p.is_error)
        self.assertEqual(p.payload["error_code"], "illegal_transition")

    def test_finish_before_report_is_illegal(self) -> None:
        prov = MockScanProvider(vin=_VIN, reachable=["dme"], faults={"dme": []})
        orch = FullScanOrchestrator(provider=prov)
        _run(orch.handle(ScanEvent.CONNECT, {"chassis_family": "f_series"}))
        p = _run(orch.handle(ScanEvent.FINISH))
        self.assertTrue(p.is_error)

    def test_abort_fails_session(self) -> None:
        prov = MockScanProvider(vin=_VIN, reachable=["dme"], faults={"dme": []})
        orch = FullScanOrchestrator(provider=prov)
        _run(orch.handle(ScanEvent.CONNECT, {"chassis_family": "f_series"}))
        p = _run(orch.handle(ScanEvent.ABORT))
        self.assertTrue(p.is_error)
        self.assertEqual(orch.state, ScanState.FAILED)

    def test_unknown_event(self) -> None:
        orch = FullScanOrchestrator(provider=MockScanProvider())
        p = _run(orch.handle("nope"))
        self.assertEqual(p.payload["error_code"], "unknown_event")


# ─────────────────────────────────────────────────────────────────────
# Entitlement integration
# ─────────────────────────────────────────────────────────────────────
class FullScanEntitlementTests(unittest.TestCase):
    def test_unentitled_blocked_at_connect(self) -> None:
        guard = MockEntitlementGuard(
            feature_code="full_system_scan", entitled_result=False,
            refusal_reason="no scan grant")
        prov = MockScanProvider(vin=_VIN, reachable=["dme"], faults={"dme": []})
        orch = FullScanOrchestrator(provider=prov, entitlement=guard)
        p = _run(orch.handle(ScanEvent.CONNECT, {"chassis_family": "f_series"}))
        self.assertTrue(p.is_error)
        self.assertEqual(p.payload["error_code"], "not_entitled")
        self.assertEqual(guard.check_calls, 1)
        self.assertEqual(guard.consume_calls, [])   # never charged

    def test_entitled_consumes_once_on_finish(self) -> None:
        guard = MockEntitlementGuard(feature_code="full_system_scan")
        prov = MockScanProvider(vin=_VIN, reachable=["dme"], faults={"dme": []})
        orch = FullScanOrchestrator(provider=prov, entitlement=guard)
        _run(orch.handle(ScanEvent.CONNECT, {"chassis_family": "f_series"}))
        _run(orch.handle(ScanEvent.SCAN_ALL))
        self.assertEqual(guard.consume_calls, [])   # not yet
        _run(orch.handle(ScanEvent.FINISH))
        self.assertEqual(len(guard.consume_calls), 1)
        self.assertEqual(guard.consume_calls[0]["vin"], _VIN)
        self.assertTrue(guard.consume_calls[0]["operation_ref"].startswith("scan-"))

    def test_snapshot_restore_preserves_state(self) -> None:
        prov = MockScanProvider(vin=_VIN, reachable=["dme"], faults={"dme": []})
        orch = FullScanOrchestrator(provider=prov)
        _run(orch.handle(ScanEvent.CONNECT, {"chassis_family": "f_series"}))
        snap = orch.snapshot()
        restored = FullScanOrchestrator.restore(provider=prov, snapshot=snap)
        self.assertEqual(restored.state, ScanState.CONNECTED)
        self.assertEqual(restored.data.vin, _VIN)
        # Can keep going from the restored point.
        p = _run(restored.handle(ScanEvent.SCAN_ALL))
        self.assertEqual(p.state, ScanState.REPORT_READY)


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
