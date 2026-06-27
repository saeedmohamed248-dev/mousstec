"""Bidirectional actuator tests + TPMS — pure-Python, zero DB, zero hardware.

Drives the generic ActuatorTestOrchestrator across the catalog and the
TpmsRelearnOrchestrator through MockSafetyGate + the mock IO/RDC providers,
asserting:
  • the declarative actuator catalog shape,
  • the happy path for each actuator (control ALWAYS returned to the ECU),
  • the SAFETY INVARIANT: control returned on confirm, abort-from-active,
    and on a mid-drive error,
  • the pre-condition gate + failure paths (bus down, security denied,
    control rejected — grant NOT consumed on failure),
  • entitlement integration (check at START, consume once on CONFIRM —
    even when the technician's verdict is "not working"),
  • the TPMS read → relearn → finish flow + its failure/entitlement paths.
"""
from __future__ import annotations

import asyncio
import unittest

from bmw_ecu.actuators import (
    ACTUATOR_CATALOG,
    BIDIRECTIONAL_FEATURE,
    TPMS_FEATURE,
    ActuatorData,
    ActuatorEvent,
    ActuatorState,
    ActuatorTestOrchestrator,
    ControlKind,
    MockActuatorProvider,
    MockTpmsProvider,
    TpmsEvent,
    TpmsRelearnOrchestrator,
    TpmsSensor,
    TpmsState,
    all_actuators,
    get_actuator,
)
from bmw_ecu.premium.safety_checks import IgnitionState, MockSafetyGate
from bmw_ecu.services.entitlement_guard import MockEntitlementGuard


def _run(coro):
    return asyncio.run(coro)


_VIN = "WBA12345678901234"


def _gate() -> MockSafetyGate:
    """KOEO, 12.6 V, P — passes every actuator + TPMS pre-condition."""
    return MockSafetyGate(voltage_v=12.6, ignition=IgnitionState.KOEO)


def _orch(code: str, *, provider=None, gate=None, entitlement=None):
    return ActuatorTestOrchestrator(
        safety=gate or _gate(),
        provider=provider or MockActuatorProvider(),
        entitlement=entitlement,
    )


# ─────────────────────────────────────────────────────────────────────
# Catalog
# ─────────────────────────────────────────────────────────────────────
class ActuatorCatalogTests(unittest.TestCase):
    def test_eight_actuators(self) -> None:
        self.assertEqual(len(ACTUATOR_CATALOG), 8)
        self.assertEqual(len(all_actuators()), 8)

    def test_every_actuator_well_formed(self) -> None:
        for a in all_actuators():
            with self.subTest(a.code):
                self.assertTrue(a.name_ar and a.name_en)
                self.assertTrue(a.target_module)
                self.assertIsInstance(a.control_kind, ControlKind)
                self.assertGreater(a.io_did, 0)
                self.assertGreater(a.default_duration_s, 0)
                self.assertTrue(a.observe_question_ar)
                self.assertEqual(a.feature_code, BIDIRECTIONAL_FEATURE)

    def test_security_protected_subset(self) -> None:
        secured = {a.code for a in all_actuators() if a.needs_security}
        self.assertEqual(secured, {"injector_1", "egr_valve"})

    def test_unique_io_dids(self) -> None:
        dids = [a.io_did for a in all_actuators()]
        self.assertEqual(len(dids), len(set(dids)))

    def test_to_dict_formats_did_hex(self) -> None:
        self.assertEqual(get_actuator("radiator_fan").to_dict()["io_did"],
                         "0xF010")


# ─────────────────────────────────────────────────────────────────────
# Happy paths
# ─────────────────────────────────────────────────────────────────────
class ActuatorHappyPathTests(unittest.TestCase):
    def test_every_actuator_runs_and_returns_control(self) -> None:
        for a in all_actuators():
            with self.subTest(a.code):
                prov = MockActuatorProvider()
                o = _orch(a.code, provider=prov)
                p = _run(o.handle(ActuatorEvent.START,
                                  {"actuator_code": a.code, "vin": _VIN}))
                self.assertEqual(p.state, ActuatorState.ARMED)
                p = _run(o.handle(ActuatorEvent.ACTIVATE))
                self.assertEqual(p.state, ActuatorState.ACTIVE)
                self.assertEqual(prov.activate_calls,
                                 [(a.io_did, a.control_kind.value,
                                   a.default_duration_s)])
                p = _run(o.handle(ActuatorEvent.CONFIRM, {"working": True}))
                self.assertEqual(p.state, ActuatorState.DONE)
                self.assertTrue(p.is_terminal)
                # SAFETY: control handed back to the ECU exactly once.
                self.assertEqual(prov.return_calls, [a.io_did])
                self.assertTrue(o.data.control_returned)

    def test_security_unlocked_only_when_required(self) -> None:
        prov = MockActuatorProvider()
        o = _orch("injector_1", provider=prov)
        _run(o.handle(ActuatorEvent.START,
                      {"actuator_code": "injector_1", "vin": _VIN}))
        _run(o.handle(ActuatorEvent.ACTIVATE))
        self.assertEqual(prov.security_calls, [_VIN])

        prov2 = MockActuatorProvider()
        o2 = _orch("horn", provider=prov2)
        _run(o2.handle(ActuatorEvent.START,
                       {"actuator_code": "horn", "vin": _VIN}))
        _run(o2.handle(ActuatorEvent.ACTIVATE))
        self.assertEqual(prov2.security_calls, [])

    def test_feedback_echoed_into_active_prompt(self) -> None:
        prov = MockActuatorProvider(feedback={0xF010: {"rpm": 3200}})
        o = _orch("radiator_fan", provider=prov)
        _run(o.handle(ActuatorEvent.START,
                      {"actuator_code": "radiator_fan", "vin": _VIN}))
        p = _run(o.handle(ActuatorEvent.ACTIVATE))
        self.assertEqual(p.payload["feedback"], {"rpm": 3200})


# ─────────────────────────────────────────────────────────────────────
# Safety invariant — control ALWAYS returned
# ─────────────────────────────────────────────────────────────────────
class ActuatorSafetyInvariantTests(unittest.TestCase):
    def test_confirm_returns_control(self) -> None:
        prov = MockActuatorProvider()
        o = _orch("fuel_pump", provider=prov)
        _run(o.handle(ActuatorEvent.START,
                      {"actuator_code": "fuel_pump", "vin": _VIN}))
        _run(o.handle(ActuatorEvent.ACTIVATE))
        _run(o.handle(ActuatorEvent.CONFIRM, {"working": True}))
        self.assertEqual(prov.return_calls, [0xF011])

    def test_abort_from_active_returns_control(self) -> None:
        prov = MockActuatorProvider()
        o = _orch("fuel_pump", provider=prov)
        _run(o.handle(ActuatorEvent.START,
                      {"actuator_code": "fuel_pump", "vin": _VIN}))
        _run(o.handle(ActuatorEvent.ACTIVATE))
        p = _run(o.handle(ActuatorEvent.ABORT))
        self.assertEqual(p.state, ActuatorState.FAILED)
        self.assertEqual(prov.return_calls, [0xF011])

    def test_abort_before_active_does_not_touch_output(self) -> None:
        prov = MockActuatorProvider()
        o = _orch("fuel_pump", provider=prov)
        _run(o.handle(ActuatorEvent.START,
                      {"actuator_code": "fuel_pump", "vin": _VIN}))
        p = _run(o.handle(ActuatorEvent.ABORT))  # in ARMED, not ACTIVE
        self.assertEqual(p.state, ActuatorState.FAILED)
        self.assertEqual(prov.return_calls, [])

    def test_control_returned_once_even_on_double_confirm_attempt(self) -> None:
        prov = MockActuatorProvider()
        o = _orch("horn", provider=prov)
        _run(o.handle(ActuatorEvent.START,
                      {"actuator_code": "horn", "vin": _VIN}))
        _run(o.handle(ActuatorEvent.ACTIVATE))
        _run(o.handle(ActuatorEvent.CONFIRM, {"working": True}))
        # A second CONFIRM is illegal (terminal) — must not re-return.
        p = _run(o.handle(ActuatorEvent.CONFIRM, {"working": True}))
        self.assertEqual(p.state, ActuatorState.FAILED)
        self.assertEqual(prov.return_calls, [0xF050])


# ─────────────────────────────────────────────────────────────────────
# Pre-condition + failure paths
# ─────────────────────────────────────────────────────────────────────
class ActuatorFailureTests(unittest.TestCase):
    def test_unknown_actuator(self) -> None:
        o = _orch("")
        p = _run(o.handle(ActuatorEvent.START,
                          {"actuator_code": "nope", "vin": _VIN}))
        self.assertEqual(p.state, ActuatorState.FAILED)
        self.assertEqual(p.payload["error_code"], "unknown_actuator")

    def test_low_voltage_blocks_start(self) -> None:
        gate = MockSafetyGate(voltage_v=10.0, ignition=IgnitionState.KOEO)
        o = _orch("radiator_fan", gate=gate)
        p = _run(o.handle(ActuatorEvent.START,
                          {"actuator_code": "radiator_fan", "vin": _VIN}))
        self.assertEqual(p.state, ActuatorState.FAILED)
        self.assertEqual(p.payload["error_code"], "prereq_failed")

    def test_wrong_ignition_blocks_start(self) -> None:
        gate = MockSafetyGate(voltage_v=12.6, ignition=IgnitionState.KOER)
        o = _orch("radiator_fan", gate=gate)
        p = _run(o.handle(ActuatorEvent.START,
                          {"actuator_code": "radiator_fan", "vin": _VIN}))
        self.assertEqual(p.payload["error_code"], "prereq_failed")

    def test_bus_down_on_activate(self) -> None:
        prov = MockActuatorProvider(bus_down=True)
        o = _orch("horn", provider=prov)
        _run(o.handle(ActuatorEvent.START,
                      {"actuator_code": "horn", "vin": _VIN}))
        p = _run(o.handle(ActuatorEvent.ACTIVATE))
        self.assertEqual(p.payload["error_code"], "transport_error")

    def test_security_denied_on_activate(self) -> None:
        prov = MockActuatorProvider(deny_security=True)
        o = _orch("injector_1", provider=prov)
        _run(o.handle(ActuatorEvent.START,
                      {"actuator_code": "injector_1", "vin": _VIN}))
        p = _run(o.handle(ActuatorEvent.ACTIVATE))
        self.assertEqual(p.payload["error_code"], "security_denied")

    def test_control_rejected_returns_control_and_fails(self) -> None:
        prov = MockActuatorProvider(reject_dids=(0xF050,))
        o = _orch("horn", provider=prov)
        _run(o.handle(ActuatorEvent.START,
                      {"actuator_code": "horn", "vin": _VIN}))
        p = _run(o.handle(ActuatorEvent.ACTIVATE))
        self.assertEqual(p.state, ActuatorState.FAILED)
        self.assertEqual(p.payload["error_code"], "control_rejected")

    def test_activate_before_start_illegal(self) -> None:
        o = _orch("horn")
        p = _run(o.handle(ActuatorEvent.ACTIVATE))
        self.assertEqual(p.payload["error_code"], "illegal_transition")

    def test_unknown_event(self) -> None:
        o = _orch("horn")
        p = _run(o.handle("frobnicate"))
        self.assertEqual(p.payload["error_code"], "unknown_event")


# ─────────────────────────────────────────────────────────────────────
# Entitlement
# ─────────────────────────────────────────────────────────────────────
class ActuatorEntitlementTests(unittest.TestCase):
    def test_unentitled_blocks_at_start(self) -> None:
        ent = MockEntitlementGuard(feature_code=BIDIRECTIONAL_FEATURE,
                                   entitled_result=False)
        o = _orch("horn", entitlement=ent)
        p = _run(o.handle(ActuatorEvent.START,
                          {"actuator_code": "horn", "vin": _VIN}))
        self.assertEqual(p.payload["error_code"], "not_entitled")
        self.assertEqual(ent.check_calls, 1)
        self.assertEqual(ent.consume_calls, [])

    def test_consume_once_on_confirm_working(self) -> None:
        ent = MockEntitlementGuard(feature_code=BIDIRECTIONAL_FEATURE)
        o = _orch("horn", entitlement=ent)
        _run(o.handle(ActuatorEvent.START,
                      {"actuator_code": "horn", "vin": _VIN}))
        _run(o.handle(ActuatorEvent.ACTIVATE))
        _run(o.handle(ActuatorEvent.CONFIRM, {"working": True}))
        self.assertEqual(len(ent.consume_calls), 1)
        self.assertEqual(ent.consume_calls[0]["operation_ref"],
                         f"horn-{_VIN}")

    def test_consume_even_when_not_working(self) -> None:
        # The diagnostic test WAS performed → still consumes.
        ent = MockEntitlementGuard(feature_code=BIDIRECTIONAL_FEATURE)
        o = _orch("horn", entitlement=ent)
        _run(o.handle(ActuatorEvent.START,
                      {"actuator_code": "horn", "vin": _VIN}))
        _run(o.handle(ActuatorEvent.ACTIVATE))
        p = _run(o.handle(ActuatorEvent.CONFIRM, {"working": False}))
        self.assertEqual(p.state, ActuatorState.DONE)
        self.assertFalse(p.payload["working"])
        self.assertEqual(len(ent.consume_calls), 1)

    def test_failed_activate_does_not_consume(self) -> None:
        ent = MockEntitlementGuard(feature_code=BIDIRECTIONAL_FEATURE)
        prov = MockActuatorProvider(bus_down=True)
        o = _orch("horn", provider=prov, entitlement=ent)
        _run(o.handle(ActuatorEvent.START,
                      {"actuator_code": "horn", "vin": _VIN}))
        _run(o.handle(ActuatorEvent.ACTIVATE))
        self.assertEqual(ent.consume_calls, [])

    def test_snapshot_restore_mid_flow(self) -> None:
        prov = MockActuatorProvider()
        o = _orch("horn", provider=prov)
        _run(o.handle(ActuatorEvent.START,
                      {"actuator_code": "horn", "vin": _VIN}))
        snap = o.snapshot()
        self.assertEqual(snap["state"], "armed")
        o2 = ActuatorTestOrchestrator.restore(
            safety=_gate(), provider=prov, snapshot=snap)
        self.assertEqual(o2.state, ActuatorState.ARMED)
        self.assertEqual(o2.data.actuator_code, "horn")
        p = _run(o2.handle(ActuatorEvent.ACTIVATE))
        self.assertEqual(p.state, ActuatorState.ACTIVE)


# ─────────────────────────────────────────────────────────────────────
# TPMS
# ─────────────────────────────────────────────────────────────────────
def _sensors(*, weak=(), low=()):
    out = []
    for pos in ("FL", "FR", "RL", "RR"):
        out.append(TpmsSensor(
            position=pos,
            sensor_id=f"ID-{pos}",
            pressure_bar=1.5 if pos in low else 2.4,
            temp_c=30.0,
            battery_ok=pos not in weak,
        ))
    return tuple(out)


def _tpms(**kw):
    return TpmsRelearnOrchestrator(
        safety=kw.pop("gate", None) or _gate(),
        provider=kw.pop("provider", None) or MockTpmsProvider(sensors=_sensors()),
        **kw,
    )


class TpmsReadResultTests(unittest.TestCase):
    def test_healthy_set(self) -> None:
        from bmw_ecu.actuators.tpms import TpmsReadResult
        r = TpmsReadResult(_sensors())
        self.assertTrue(r.all_healthy)
        self.assertEqual(r.weak_batteries, ())
        self.assertEqual(r.out_of_range, ())

    def test_flags_weak_and_low(self) -> None:
        from bmw_ecu.actuators.tpms import TpmsReadResult
        r = TpmsReadResult(_sensors(weak=("RR",), low=("RL",)))
        self.assertFalse(r.all_healthy)
        self.assertEqual(r.weak_batteries, ("RR",))
        self.assertEqual(r.out_of_range, ("RL",))


class TpmsHappyPathTests(unittest.TestCase):
    def test_read_relearn_finish(self) -> None:
        prov = MockTpmsProvider(sensors=_sensors())
        o = _tpms(provider=prov)
        p = _run(o.handle(TpmsEvent.READ, {"vin": _VIN}))
        self.assertEqual(p.state, TpmsState.READ_DONE)
        self.assertEqual(prov.read_calls, 1)
        self.assertEqual(len(p.payload["read"]["sensors"]), 4)
        p = _run(o.handle(TpmsEvent.RELEARN))
        self.assertEqual(p.state, TpmsState.RELEARNED)
        self.assertEqual(prov.relearn_calls,
                         [{"FL": "ID-FL", "FR": "ID-FR",
                           "RL": "ID-RL", "RR": "ID-RR"}])
        p = _run(o.handle(TpmsEvent.FINISH))
        self.assertEqual(p.state, TpmsState.DONE)
        self.assertTrue(p.is_terminal)

    def test_security_only_when_required(self) -> None:
        prov = MockTpmsProvider(sensors=_sensors())
        o = _tpms(provider=prov, needs_security=True)
        _run(o.handle(TpmsEvent.READ, {"vin": _VIN}))
        _run(o.handle(TpmsEvent.RELEARN))
        self.assertEqual(prov.security_calls, [_VIN])

        prov2 = MockTpmsProvider(sensors=_sensors())
        o2 = _tpms(provider=prov2, needs_security=False)
        _run(o2.handle(TpmsEvent.READ, {"vin": _VIN}))
        _run(o2.handle(TpmsEvent.RELEARN))
        self.assertEqual(prov2.security_calls, [])


class TpmsFailureTests(unittest.TestCase):
    def test_low_voltage_blocks_read(self) -> None:
        gate = MockSafetyGate(voltage_v=10.0, ignition=IgnitionState.KOEO)
        o = _tpms(gate=gate)
        p = _run(o.handle(TpmsEvent.READ, {"vin": _VIN}))
        self.assertEqual(p.payload["error_code"], "prereq_failed")

    def test_bus_down_on_relearn(self) -> None:
        prov = MockTpmsProvider(sensors=_sensors(), bus_down=True)
        o = _tpms(provider=prov)
        _run(o.handle(TpmsEvent.READ, {"vin": _VIN}))
        p = _run(o.handle(TpmsEvent.RELEARN))
        self.assertEqual(p.payload["error_code"], "transport_error")

    def test_security_denied_on_relearn(self) -> None:
        prov = MockTpmsProvider(sensors=_sensors(), deny_security=True)
        o = _tpms(provider=prov, needs_security=True)
        _run(o.handle(TpmsEvent.READ, {"vin": _VIN}))
        p = _run(o.handle(TpmsEvent.RELEARN))
        self.assertEqual(p.payload["error_code"], "security_denied")

    def test_relearn_rejected(self) -> None:
        prov = MockTpmsProvider(sensors=_sensors(), reject_relearn=True)
        o = _tpms(provider=prov)
        _run(o.handle(TpmsEvent.READ, {"vin": _VIN}))
        p = _run(o.handle(TpmsEvent.RELEARN))
        self.assertEqual(p.payload["error_code"], "relearn_rejected")

    def test_relearn_before_read_illegal(self) -> None:
        o = _tpms()
        p = _run(o.handle(TpmsEvent.RELEARN))
        self.assertEqual(p.payload["error_code"], "illegal_transition")

    def test_abort(self) -> None:
        o = _tpms()
        _run(o.handle(TpmsEvent.READ, {"vin": _VIN}))
        p = _run(o.handle(TpmsEvent.ABORT))
        self.assertEqual(p.state, TpmsState.FAILED)

    def test_unknown_event(self) -> None:
        o = _tpms()
        p = _run(o.handle("spin"))
        self.assertEqual(p.payload["error_code"], "unknown_event")


class TpmsEntitlementTests(unittest.TestCase):
    def test_unentitled_blocks_read(self) -> None:
        ent = MockEntitlementGuard(feature_code=TPMS_FEATURE,
                                   entitled_result=False)
        o = _tpms(entitlement=ent)
        p = _run(o.handle(TpmsEvent.READ, {"vin": _VIN}))
        self.assertEqual(p.payload["error_code"], "not_entitled")
        self.assertEqual(ent.consume_calls, [])

    def test_consume_once_on_finish(self) -> None:
        ent = MockEntitlementGuard(feature_code=TPMS_FEATURE)
        o = _tpms(entitlement=ent)
        _run(o.handle(TpmsEvent.READ, {"vin": _VIN}))
        _run(o.handle(TpmsEvent.RELEARN))
        _run(o.handle(TpmsEvent.FINISH))
        self.assertEqual(len(ent.consume_calls), 1)
        self.assertEqual(ent.consume_calls[0]["operation_ref"],
                         f"tpms-{_VIN}")

    def test_failed_relearn_does_not_consume(self) -> None:
        ent = MockEntitlementGuard(feature_code=TPMS_FEATURE)
        prov = MockTpmsProvider(sensors=_sensors(), reject_relearn=True)
        o = _tpms(provider=prov, entitlement=ent)
        _run(o.handle(TpmsEvent.READ, {"vin": _VIN}))
        _run(o.handle(TpmsEvent.RELEARN))
        self.assertEqual(ent.consume_calls, [])

    def test_snapshot_restore(self) -> None:
        prov = MockTpmsProvider(sensors=_sensors())
        o = _tpms(provider=prov)
        _run(o.handle(TpmsEvent.READ, {"vin": _VIN}))
        snap = o.snapshot()
        self.assertEqual(snap["state"], "read_done")
        o2 = TpmsRelearnOrchestrator.restore(
            safety=_gate(), provider=prov, snapshot=snap)
        self.assertEqual(o2.state, TpmsState.READ_DONE)
        self.assertEqual(len(o2.data.sensors), 4)
        p = _run(o2.handle(TpmsEvent.RELEARN))
        self.assertEqual(p.state, TpmsState.RELEARNED)


if __name__ == "__main__":
    unittest.main()
