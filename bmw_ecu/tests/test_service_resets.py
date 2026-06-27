"""Service Resets — pure-Python, zero DB, zero hardware.

Drives the generic ServiceResetOrchestrator across every procedure in the
catalog through MockSafetyGate + MockResetProvider, asserting:
  • the declarative catalog shape (every procedure has steps, the right
    feature_code, sane safety requirements),
  • the happy path for each of the 5 procedures,
  • the pre-condition gate (DPF needs engine running, EPB needs higher
    voltage, SAS blocks on its forbidden DTCs),
  • the failure paths (bus down, security denied, routine rejected, a
    failed step — grant NOT consumed on any of them),
  • the entitlement integration (check at START, consume once on FINISH).
"""
from __future__ import annotations

import asyncio
import unittest

from bmw_ecu.premium.safety_checks import (
    GearPosition,
    IgnitionState,
    MockSafetyGate,
)
from bmw_ecu.resets import (
    PROCEDURE_CATALOG,
    SERVICE_RESETS_FEATURE,
    MockResetProvider,
    ResetEvent,
    ResetState,
    ServiceResetOrchestrator,
    get_procedure,
)
from bmw_ecu.services.entitlement_guard import MockEntitlementGuard


def _run(coro):
    return asyncio.run(coro)


_VIN = "WBA12345678901234"


def _gate_for(code: str) -> MockSafetyGate:
    """A safety gate pre-configured to PASS the given procedure."""
    proc = get_procedure(code)
    req = proc.safety
    ign = req.ignition_in[0]
    gear = req.gear_in[0]
    # Pick a voltage comfortably inside the band.
    volt = (req.voltage_min_v + req.voltage_max_v) / 2
    return MockSafetyGate(voltage_v=volt, gear=gear, ignition=ign)


# ─────────────────────────────────────────────────────────────────────
# Catalog
# ─────────────────────────────────────────────────────────────────────
class CatalogTests(unittest.TestCase):
    def test_five_procedures(self) -> None:
        self.assertEqual(
            set(PROCEDURE_CATALOG),
            {"oil_reset", "epb_service", "sas_calibration",
             "dpf_regen", "throttle_adaptation"},
        )

    def test_every_procedure_well_formed(self) -> None:
        for code, proc in PROCEDURE_CATALOG.items():
            self.assertEqual(proc.feature_code, SERVICE_RESETS_FEATURE)
            self.assertTrue(proc.steps, f"{code} has no steps")
            self.assertTrue(proc.name_ar and proc.name_en)
            self.assertTrue(proc.success_message_ar)
            # to_dict round-trips cleanly
            d = proc.to_dict()
            self.assertEqual(d["code"], code)
            self.assertEqual(len(d["steps"]), len(proc.steps))

    def test_dpf_requires_engine_running(self) -> None:
        self.assertIn(IgnitionState.KOER,
                      get_procedure("dpf_regen").safety.ignition_in)

    def test_oil_reset_no_security(self) -> None:
        self.assertFalse(get_procedure("oil_reset").needs_security_access)

    def test_epb_needs_security(self) -> None:
        self.assertTrue(get_procedure("epb_service").needs_security_access)


# ─────────────────────────────────────────────────────────────────────
# Happy paths — every procedure
# ─────────────────────────────────────────────────────────────────────
class HappyPathTests(unittest.TestCase):
    def _drive(self, code, provider=None):
        orch = ServiceResetOrchestrator(
            safety=_gate_for(code),
            provider=provider or MockResetProvider(),
        )
        p = _run(orch.handle(ResetEvent.START,
                             {"procedure_code": code, "vin": _VIN}))
        self.assertFalse(p.is_error, f"{code} START failed: {p.body}")
        self.assertEqual(orch.state, ResetState.PREREQ_OK)
        p = _run(orch.handle(ResetEvent.RUN))
        self.assertEqual(orch.state, ResetState.COMPLETED, f"{code} RUN: {p.body}")
        p = _run(orch.handle(ResetEvent.FINISH))
        self.assertTrue(p.is_terminal)
        self.assertEqual(orch.state, ResetState.DONE)
        self.assertEqual(p.progress_pct, 100)
        return orch, p

    def test_all_procedures_happy(self) -> None:
        for code in PROCEDURE_CATALOG:
            with self.subTest(procedure=code):
                self._drive(code)

    def test_security_called_only_when_required(self) -> None:
        prov = MockResetProvider()
        self._drive("oil_reset", provider=prov)
        self.assertEqual(prov.security_calls, [])   # oil reset: no security

        prov2 = MockResetProvider()
        self._drive("epb_service", provider=prov2)
        self.assertEqual(prov2.security_calls, [_VIN])   # epb: yes

    def test_steps_executed_in_order(self) -> None:
        prov = MockResetProvider()
        self._drive("throttle_adaptation", provider=prov)
        # throttle has two ROUTINE_START then a ROUTINE_RESULT
        self.assertEqual(prov.routine_starts, [0xEF40, 0xEF41])
        self.assertEqual(prov.routine_results, [0xEF41])

    def test_finish_emits_success_message(self) -> None:
        _, p = self._drive("oil_reset")
        self.assertIn("الزيت", p.body)


# ─────────────────────────────────────────────────────────────────────
# Pre-condition gate
# ─────────────────────────────────────────────────────────────────────
class PrereqTests(unittest.TestCase):
    def test_dpf_blocked_engine_off(self) -> None:
        # Default mock is KOEO → DPF needs KOER → blocked.
        orch = ServiceResetOrchestrator(
            safety=MockSafetyGate(voltage_v=13.4),
            provider=MockResetProvider())
        p = _run(orch.handle(ResetEvent.START,
                             {"procedure_code": "dpf_regen", "vin": _VIN}))
        self.assertTrue(p.is_error)
        self.assertEqual(p.payload["error_code"], "prereq_failed")

    def test_epb_blocked_low_voltage(self) -> None:
        # EPB needs >= 12.5 V; give it 12.1.
        orch = ServiceResetOrchestrator(
            safety=MockSafetyGate(voltage_v=12.1),
            provider=MockResetProvider())
        p = _run(orch.handle(ResetEvent.START,
                             {"procedure_code": "epb_service", "vin": _VIN}))
        self.assertTrue(p.is_error)
        self.assertEqual(p.payload["error_code"], "prereq_failed")

    def test_sas_blocked_on_forbidden_dtc(self) -> None:
        gate = MockSafetyGate(voltage_v=12.6, recent_dtcs=("C1500",))
        orch = ServiceResetOrchestrator(safety=gate,
                                        provider=MockResetProvider())
        p = _run(orch.handle(ResetEvent.START,
                             {"procedure_code": "sas_calibration", "vin": _VIN}))
        self.assertTrue(p.is_error)
        self.assertEqual(p.payload["error_code"], "prereq_failed")

    def test_unknown_procedure(self) -> None:
        orch = ServiceResetOrchestrator(safety=MockSafetyGate(),
                                        provider=MockResetProvider())
        p = _run(orch.handle(ResetEvent.START,
                             {"procedure_code": "made_up", "vin": _VIN}))
        self.assertTrue(p.is_error)
        self.assertEqual(p.payload["error_code"], "unknown_procedure")


# ─────────────────────────────────────────────────────────────────────
# Failure paths
# ─────────────────────────────────────────────────────────────────────
class FailurePathTests(unittest.TestCase):
    def test_bus_down_fails_at_run(self) -> None:
        orch = ServiceResetOrchestrator(
            safety=_gate_for("oil_reset"),
            provider=MockResetProvider(bus_down=True))
        _run(orch.handle(ResetEvent.START,
                        {"procedure_code": "oil_reset", "vin": _VIN}))
        p = _run(orch.handle(ResetEvent.RUN))
        self.assertTrue(p.is_error)
        self.assertEqual(p.payload["error_code"], "transport_error")

    def test_security_denied(self) -> None:
        orch = ServiceResetOrchestrator(
            safety=_gate_for("epb_service"),
            provider=MockResetProvider(deny_security=True))
        _run(orch.handle(ResetEvent.START,
                        {"procedure_code": "epb_service", "vin": _VIN}))
        p = _run(orch.handle(ResetEvent.RUN))
        self.assertTrue(p.is_error)
        self.assertEqual(p.payload["error_code"], "security_denied")

    def test_routine_rejected(self) -> None:
        orch = ServiceResetOrchestrator(
            safety=_gate_for("oil_reset"),
            provider=MockResetProvider(reject_rids=(0xAB01,)))
        _run(orch.handle(ResetEvent.START,
                        {"procedure_code": "oil_reset", "vin": _VIN}))
        p = _run(orch.handle(ResetEvent.RUN))
        self.assertTrue(p.is_error)
        self.assertEqual(p.payload["error_code"], "routine_rejected")

    def test_failed_step_stops_sequence(self) -> None:
        # First throttle routine "fails" (ok=False) → second never runs.
        prov = MockResetProvider(fail_rids=(0xEF40,))
        orch = ServiceResetOrchestrator(
            safety=_gate_for("throttle_adaptation"), provider=prov)
        _run(orch.handle(ResetEvent.START,
                        {"procedure_code": "throttle_adaptation", "vin": _VIN}))
        p = _run(orch.handle(ResetEvent.RUN))
        self.assertTrue(p.is_error)
        self.assertEqual(p.payload["error_code"], "step_failed")
        self.assertEqual(prov.routine_starts, [0xEF40])   # stopped after fail

    def test_run_before_start_illegal(self) -> None:
        orch = ServiceResetOrchestrator(safety=MockSafetyGate(),
                                        provider=MockResetProvider())
        p = _run(orch.handle(ResetEvent.RUN))
        self.assertTrue(p.is_error)
        self.assertEqual(p.payload["error_code"], "illegal_transition")

    def test_abort(self) -> None:
        orch = ServiceResetOrchestrator(safety=_gate_for("oil_reset"),
                                        provider=MockResetProvider())
        _run(orch.handle(ResetEvent.START,
                        {"procedure_code": "oil_reset", "vin": _VIN}))
        p = _run(orch.handle(ResetEvent.ABORT))
        self.assertTrue(p.is_error)
        self.assertEqual(orch.state, ResetState.FAILED)


# ─────────────────────────────────────────────────────────────────────
# Entitlement integration
# ─────────────────────────────────────────────────────────────────────
class EntitlementTests(unittest.TestCase):
    def test_unentitled_blocked_at_start(self) -> None:
        guard = MockEntitlementGuard(
            feature_code=SERVICE_RESETS_FEATURE, entitled_result=False,
            refusal_reason="no service grant")
        prov = MockResetProvider()
        orch = ServiceResetOrchestrator(
            safety=_gate_for("oil_reset"), provider=prov, entitlement=guard)
        p = _run(orch.handle(ResetEvent.START,
                             {"procedure_code": "oil_reset", "vin": _VIN}))
        self.assertTrue(p.is_error)
        self.assertEqual(p.payload["error_code"], "not_entitled")
        self.assertEqual(guard.check_calls, 1)
        self.assertEqual(prov.session_calls, 0)      # never touched the bus
        self.assertEqual(guard.consume_calls, [])    # never charged

    def test_entitled_consumes_once_on_finish(self) -> None:
        guard = MockEntitlementGuard(feature_code=SERVICE_RESETS_FEATURE)
        orch = ServiceResetOrchestrator(
            safety=_gate_for("sas_calibration"),
            provider=MockResetProvider(), entitlement=guard)
        _run(orch.handle(ResetEvent.START,
                        {"procedure_code": "sas_calibration", "vin": _VIN}))
        _run(orch.handle(ResetEvent.RUN))
        self.assertEqual(guard.consume_calls, [])   # not yet
        _run(orch.handle(ResetEvent.FINISH))
        self.assertEqual(len(guard.consume_calls), 1)
        self.assertTrue(
            guard.consume_calls[0]["operation_ref"].startswith("sas_calibration-"))

    def test_failed_step_does_not_consume(self) -> None:
        guard = MockEntitlementGuard(feature_code=SERVICE_RESETS_FEATURE)
        prov = MockResetProvider(fail_rids=(0xAB01,))
        orch = ServiceResetOrchestrator(
            safety=_gate_for("oil_reset"), provider=prov, entitlement=guard)
        _run(orch.handle(ResetEvent.START,
                        {"procedure_code": "oil_reset", "vin": _VIN}))
        _run(orch.handle(ResetEvent.RUN))
        self.assertEqual(guard.consume_calls, [])   # work failed → no charge

    def test_snapshot_restore(self) -> None:
        orch = ServiceResetOrchestrator(
            safety=_gate_for("oil_reset"), provider=MockResetProvider())
        _run(orch.handle(ResetEvent.START,
                        {"procedure_code": "oil_reset", "vin": _VIN}))
        snap = orch.snapshot()
        restored = ServiceResetOrchestrator.restore(
            safety=_gate_for("oil_reset"),
            provider=MockResetProvider(), snapshot=snap)
        self.assertEqual(restored.state, ResetState.PREREQ_OK)
        self.assertEqual(restored.data.procedure_code, "oil_reset")
        self.assertIsNotNone(restored.procedure)
        p = _run(restored.handle(ResetEvent.RUN))
        self.assertEqual(restored.state, ResetState.COMPLETED)


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
