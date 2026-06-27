"""Premium trio — EGS ISN reset, ACSM crash reset, CBS battery manager.

Pure-Python, zero DB, zero hardware. Each module is driven through
its mock provider + MockSafetyGate so every state transition + every
failure mode is asserted deterministically.

Critical area: ACSM. Beyond the happy path, the tests verify the HARD
SAFETY BLOCK paths (deployed bag, blocking DTCs, voltage out of range)
land in BLOCKED_FOR_SAFETY — NOT in FAILED. That distinction is the
contract the chatbot UI uses to render "physical inspection required"
vs "transient error, retry".
"""
from __future__ import annotations

import asyncio
import unittest

from bmw_ecu.premium import (
    AbstractSafetyGate,
    AcsmCrashEvent,
    AcsmCrashOrchestrator,
    AcsmCrashState,
    AirbagModuleState,
    BatterySpec,
    BatteryType,
    CbsBatteryEvent,
    CbsBatteryOrchestrator,
    CbsBatteryState,
    EgsIsnEvent,
    EgsIsnOrchestrator,
    EgsIsnState,
    GearPosition,
    IgnitionState,
    MockSafetyGate,
)
from bmw_ecu.premium.acsm_crash_reset import MockAcsmServiceProvider
from bmw_ecu.premium.cbs_battery_manager import MockCbsServiceProvider
from bmw_ecu.premium.egs_isn_reset import MockEgsServiceProvider


def _run(coro):
    return asyncio.run(coro)


_VIN = "WBA12345678901234"


# ─────────────────────────────────────────────────────────────────────
# SafetyGate
# ─────────────────────────────────────────────────────────────────────
class SafetyGateTests(unittest.TestCase):
    def test_default_mock_is_safe(self) -> None:
        gate = MockSafetyGate()
        report = _run(gate.probe(require={
            "voltage_min_v": 11.5, "voltage_max_v": 14.8,
            "gear_in": [GearPosition.P],
            "ignition_in": [IgnitionState.KOEO],
        }))
        self.assertTrue(report.ok)
        self.assertEqual(report.refusal_reasons, ())

    def test_voltage_too_low_refused(self) -> None:
        gate = MockSafetyGate(voltage_v=10.5)
        report = _run(gate.probe(require={"voltage_min_v": 11.5}))
        self.assertFalse(report.ok)
        self.assertTrue(any("voltage too low" in r
                            for r in report.refusal_reasons))

    def test_wrong_gear_refused(self) -> None:
        gate = MockSafetyGate(gear=GearPosition.D)
        report = _run(gate.probe(require={"gear_in": [GearPosition.P]}))
        self.assertFalse(report.ok)

    def test_forbidden_dtc_refused(self) -> None:
        gate = MockSafetyGate(recent_dtcs=("P0700",))
        report = _run(gate.probe(require={"forbidden_dtcs": ("P0700",)}))
        self.assertFalse(report.ok)

    def test_deployed_bag_refused_when_required(self) -> None:
        gate = MockSafetyGate(airbag_modules=(
            ("driver", AirbagModuleState.DEPLOYED),
            ("passenger", AirbagModuleState.OK),
        ))
        report = _run(gate.probe(require={"forbid_deployed_bag": True}))
        self.assertFalse(report.ok)
        self.assertTrue(any("driver" in r for r in report.refusal_reasons))


# ─────────────────────────────────────────────────────────────────────
# EGS ISN Reset
# ─────────────────────────────────────────────────────────────────────
class EgsIsnHappyPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.safety = MockSafetyGate()
        self.provider = MockEgsServiceProvider(
            initial_isn=bytes(range(0x10, 0x30)),
        )
        self.orch = EgsIsnOrchestrator(safety=self.safety,
                                       provider=self.provider)

    def test_full_happy_path(self) -> None:
        p1 = _run(self.orch.handle(EgsIsnEvent.CHECK_PREREQS,
                                   {"vin": _VIN}))
        self.assertEqual(self.orch.state, EgsIsnState.PREREQ_OK)
        self.assertEqual(p1.expects, "READ_BOUND_ISN")

        p2 = _run(self.orch.handle(EgsIsnEvent.READ_BOUND_ISN))
        self.assertEqual(self.orch.state, EgsIsnState.CURRENT_ISN_READ)
        self.assertEqual(self.provider.security_calls, [_VIN])
        self.assertFalse(p2.payload["already_clear"])

        p3 = _run(self.orch.handle(EgsIsnEvent.REQUEST_CLEAR))
        self.assertEqual(self.orch.state, EgsIsnState.RESET_REQUESTED)
        self.assertEqual(self.provider.clear_calls, 1)

        p4 = _run(self.orch.handle(EgsIsnEvent.VERIFY))
        self.assertEqual(self.orch.state, EgsIsnState.VERIFIED)
        self.assertEqual(self.provider.restart_calls, 1)
        self.assertEqual(self.orch.data.bound_isn_hex_after,
                         "FF" * 32)

        p5 = _run(self.orch.handle(EgsIsnEvent.FINISH))
        self.assertEqual(self.orch.state, EgsIsnState.DONE)
        self.assertTrue(p5.is_terminal)
        self.assertEqual(p5.progress_pct, 100)


class EgsIsnFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = MockEgsServiceProvider(
            initial_isn=bytes(range(0x10, 0x30)),
        )

    def test_voltage_too_low_blocks_prereqs(self) -> None:
        safety = MockSafetyGate(voltage_v=10.0)
        orch = EgsIsnOrchestrator(safety=safety, provider=self.provider)
        prompt = _run(orch.handle(EgsIsnEvent.CHECK_PREREQS, {"vin": _VIN}))
        self.assertEqual(orch.state, EgsIsnState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "prereq_failed")

    def test_gear_not_in_park_blocks_prereqs(self) -> None:
        safety = MockSafetyGate(gear=GearPosition.D)
        orch = EgsIsnOrchestrator(safety=safety, provider=self.provider)
        prompt = _run(orch.handle(EgsIsnEvent.CHECK_PREREQS, {"vin": _VIN}))
        self.assertEqual(orch.state, EgsIsnState.FAILED)

    def test_active_gearbox_dtc_blocks_prereqs(self) -> None:
        safety = MockSafetyGate(recent_dtcs=("P0700",))
        orch = EgsIsnOrchestrator(safety=safety, provider=self.provider)
        prompt = _run(orch.handle(EgsIsnEvent.CHECK_PREREQS, {"vin": _VIN}))
        self.assertEqual(orch.state, EgsIsnState.FAILED)

    def test_provider_refuses_clear(self) -> None:
        safety = MockSafetyGate()
        provider = MockEgsServiceProvider(
            initial_isn=bytes(range(0x10, 0x30)),
            refuse_clear=True,
        )
        orch = EgsIsnOrchestrator(safety=safety, provider=provider)
        _run(orch.handle(EgsIsnEvent.CHECK_PREREQS, {"vin": _VIN}))
        _run(orch.handle(EgsIsnEvent.READ_BOUND_ISN))
        prompt = _run(orch.handle(EgsIsnEvent.REQUEST_CLEAR))
        self.assertEqual(orch.state, EgsIsnState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "reset_refused")

    def test_illegal_transition(self) -> None:
        orch = EgsIsnOrchestrator(safety=MockSafetyGate(),
                                  provider=self.provider)
        # READ_BOUND_ISN straight from IDLE — illegal.
        prompt = _run(orch.handle(EgsIsnEvent.READ_BOUND_ISN))
        self.assertEqual(orch.state, EgsIsnState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "illegal_transition")

    def test_abort_marks_failed(self) -> None:
        orch = EgsIsnOrchestrator(safety=MockSafetyGate(),
                                  provider=self.provider)
        prompt = _run(orch.handle(EgsIsnEvent.ABORT))
        self.assertEqual(orch.state, EgsIsnState.FAILED)


# ─────────────────────────────────────────────────────────────────────
# ACSM Crash Reset — the safety-critical one
# ─────────────────────────────────────────────────────────────────────
def _crash_record():
    return {
        "timestamp": 1_700_000_000, "severity": 3,
        "deployed_slots": (), "raw_hex": "AA" * 64,
    }


class AcsmHappyPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.safety = MockSafetyGate(
            ignition=IgnitionState.KOEO,
            airbag_modules=(
                ("driver", AirbagModuleState.OK),
                ("passenger", AirbagModuleState.OK),
                ("side_l", AirbagModuleState.OK),
                ("side_r", AirbagModuleState.OK),
            ),
        )
        self.provider = MockAcsmServiceProvider(initial_record=_crash_record())
        self.orch = AcsmCrashOrchestrator(safety=self.safety,
                                          provider=self.provider)

    def test_full_happy_path(self) -> None:
        p1 = _run(self.orch.handle(AcsmCrashEvent.ASSESS_DAMAGE,
                                   {"vin": _VIN, "technician_id": "tech_42"}))
        self.assertEqual(self.orch.state, AcsmCrashState.DAMAGE_ASSESSED)
        self.assertEqual(p1.payload["ok_module_count"], 4)

        p2 = _run(self.orch.handle(AcsmCrashEvent.READ_RECORD))
        self.assertEqual(self.orch.state, AcsmCrashState.CRASH_RECORD_READ)
        self.assertTrue(p2.payload["has_record"])

        p3 = _run(self.orch.handle(AcsmCrashEvent.BACKUP))
        self.assertEqual(self.orch.state, AcsmCrashState.BACKUP_SAVED)
        self.assertTrue(p3.payload["backup_ref"].startswith("BAK-"))
        self.assertEqual(len(self.provider.backup_calls), 1)

        p4 = _run(self.orch.handle(AcsmCrashEvent.REQUEST_CLEAR))
        self.assertEqual(self.orch.state, AcsmCrashState.CLEAR_REQUESTED)
        self.assertEqual(self.provider.clear_calls, 1)
        # Safety gate ran TWICE: once at ASSESS_DAMAGE, once again
        # immediately before the irreversible clear.
        self.assertEqual(len(self.safety.probe_calls), 2)

        p5 = _run(self.orch.handle(AcsmCrashEvent.VERIFY))
        self.assertEqual(self.orch.state, AcsmCrashState.VERIFIED)

        p6 = _run(self.orch.handle(AcsmCrashEvent.FINISH))
        self.assertEqual(self.orch.state, AcsmCrashState.DONE)
        self.assertTrue(p6.is_terminal)
        self.assertFalse(p6.is_error)


class AcsmSafetyBlockTests(unittest.TestCase):
    """CRITICAL — verifies the BLOCKED_FOR_SAFETY terminal state is
    used (NOT FAILED) when a hard safety pre-condition trips."""

    def _build(self, *, safety: MockSafetyGate) -> AcsmCrashOrchestrator:
        return AcsmCrashOrchestrator(
            safety=safety,
            provider=MockAcsmServiceProvider(initial_record=_crash_record()),
        )

    def test_deployed_bag_blocks_for_safety_not_failed(self) -> None:
        safety = MockSafetyGate(airbag_modules=(
            ("driver", AirbagModuleState.DEPLOYED),
            ("passenger", AirbagModuleState.OK),
        ))
        orch = self._build(safety=safety)
        prompt = _run(orch.handle(AcsmCrashEvent.ASSESS_DAMAGE,
                                  {"vin": _VIN}))
        # Distinct terminal state — NOT FAILED.
        self.assertEqual(orch.state, AcsmCrashState.BLOCKED_FOR_SAFETY)
        self.assertTrue(prompt.is_safety_block)
        self.assertFalse(prompt.is_error)
        self.assertTrue(prompt.is_terminal)
        reasons_combined = " ".join(prompt.payload["blocked_reasons"])
        self.assertIn("driver", reasons_combined)

    def test_disconnected_squib_blocks_for_safety(self) -> None:
        safety = MockSafetyGate(airbag_modules=(
            ("passenger_buckle", AirbagModuleState.DISCONNECTED),
        ))
        orch = self._build(safety=safety)
        _run(orch.handle(AcsmCrashEvent.ASSESS_DAMAGE, {"vin": _VIN}))
        self.assertEqual(orch.state, AcsmCrashState.BLOCKED_FOR_SAFETY)

    def test_shorted_squib_blocks_for_safety(self) -> None:
        safety = MockSafetyGate(airbag_modules=(
            ("curtain_l", AirbagModuleState.SHORTED),
        ))
        orch = self._build(safety=safety)
        _run(orch.handle(AcsmCrashEvent.ASSESS_DAMAGE, {"vin": _VIN}))
        self.assertEqual(orch.state, AcsmCrashState.BLOCKED_FOR_SAFETY)

    def test_blocking_dtc_blocks_for_safety(self) -> None:
        safety = MockSafetyGate(recent_dtcs=("B0001",))
        orch = self._build(safety=safety)
        _run(orch.handle(AcsmCrashEvent.ASSESS_DAMAGE, {"vin": _VIN}))
        self.assertEqual(orch.state, AcsmCrashState.BLOCKED_FOR_SAFETY)

    def test_engine_running_blocks_for_safety(self) -> None:
        safety = MockSafetyGate(ignition=IgnitionState.KOER)
        orch = self._build(safety=safety)
        _run(orch.handle(AcsmCrashEvent.ASSESS_DAMAGE, {"vin": _VIN}))
        self.assertEqual(orch.state, AcsmCrashState.BLOCKED_FOR_SAFETY)

    def test_voltage_too_low_blocks_for_safety(self) -> None:
        safety = MockSafetyGate(voltage_v=11.2)
        orch = self._build(safety=safety)
        _run(orch.handle(AcsmCrashEvent.ASSESS_DAMAGE, {"vin": _VIN}))
        self.assertEqual(orch.state, AcsmCrashState.BLOCKED_FOR_SAFETY)

    def test_safety_block_after_backup_still_blocks(self) -> None:
        """A wire that comes loose between ASSESS_DAMAGE and the second
        gate (right before REQUEST_CLEAR) must trip the second probe and
        BLOCK — never silently allow the clear."""
        safety = MockSafetyGate(
            airbag_modules=(("driver", AirbagModuleState.OK),),
        )
        orch = self._build(safety=safety)
        _run(orch.handle(AcsmCrashEvent.ASSESS_DAMAGE, {"vin": _VIN}))
        _run(orch.handle(AcsmCrashEvent.READ_RECORD))
        _run(orch.handle(AcsmCrashEvent.BACKUP))
        # Simulate wire coming loose AFTER backup but BEFORE clear.
        safety.airbag_modules = (
            ("driver", AirbagModuleState.DISCONNECTED),
        )
        prompt = _run(orch.handle(AcsmCrashEvent.REQUEST_CLEAR))
        self.assertEqual(orch.state, AcsmCrashState.BLOCKED_FOR_SAFETY)
        self.assertTrue(prompt.is_safety_block)
        # The clear routine must NOT have run.
        self.assertEqual(orch.provider.clear_calls, 0)


class AcsmFailureTests(unittest.TestCase):
    """FAILED path (transient error, retry possible) — not safety blocks."""

    def test_illegal_transition_marks_failed_not_blocked(self) -> None:
        orch = AcsmCrashOrchestrator(
            safety=MockSafetyGate(),
            provider=MockAcsmServiceProvider(initial_record=_crash_record()),
        )
        # READ_RECORD straight from IDLE — illegal.
        prompt = _run(orch.handle(AcsmCrashEvent.READ_RECORD))
        self.assertEqual(orch.state, AcsmCrashState.FAILED)
        self.assertFalse(prompt.is_safety_block)
        self.assertTrue(prompt.is_error)

    def test_provider_refuses_clear_blocks_for_safety(self) -> None:
        # ACSM refusing the clear is treated as a safety block, not
        # a transient failure (could indicate the squib check failed).
        orch = AcsmCrashOrchestrator(
            safety=MockSafetyGate(airbag_modules=(("d", AirbagModuleState.OK),)),
            provider=MockAcsmServiceProvider(
                initial_record=_crash_record(), refuse_clear=True,
            ),
        )
        _run(orch.handle(AcsmCrashEvent.ASSESS_DAMAGE, {"vin": _VIN}))
        _run(orch.handle(AcsmCrashEvent.READ_RECORD))
        _run(orch.handle(AcsmCrashEvent.BACKUP))
        prompt = _run(orch.handle(AcsmCrashEvent.REQUEST_CLEAR))
        self.assertEqual(orch.state, AcsmCrashState.BLOCKED_FOR_SAFETY)
        self.assertTrue(prompt.is_safety_block)


# ─────────────────────────────────────────────────────────────────────
# CBS Battery Manager
# ─────────────────────────────────────────────────────────────────────
class CbsBatteryHappyPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.safety = MockSafetyGate(voltage_v=12.7)
        self.provider = MockCbsServiceProvider()
        self.orch = CbsBatteryOrchestrator(safety=self.safety,
                                           provider=self.provider)

    def test_full_happy_path_agm_on_f30(self) -> None:
        p1 = _run(self.orch.handle(CbsBatteryEvent.ENTER_BATTERY_INFO, {
            "vin": _VIN, "chassis": "F30",
            "type": "agm", "capacity_ah": 90, "serial": "BMW-AGM-001",
        }))
        self.assertEqual(self.orch.state, CbsBatteryState.BATTERY_INFO_OK)
        self.assertEqual(p1.payload["type"], "agm")

        _run(self.orch.handle(CbsBatteryEvent.CHECK_VEHICLE))
        self.assertEqual(self.orch.state, CbsBatteryState.VEHICLE_STATE_OK)

        _run(self.orch.handle(CbsBatteryEvent.READ_OLD))
        self.assertEqual(self.orch.state, CbsBatteryState.OLD_REG_READ)
        self.assertEqual(self.orch.data.old_registration["serial"],
                         "OLDBAT-001")

        _run(self.orch.handle(CbsBatteryEvent.WRITE_NEW))
        self.assertEqual(self.orch.state, CbsBatteryState.NEW_REG_WRITTEN)
        self.assertEqual(len(self.provider.writes), 1)
        self.assertEqual(self.provider.writes[0].capacity_ah, 90)

        _run(self.orch.handle(CbsBatteryEvent.RESET_COUNTERS))
        self.assertEqual(self.orch.state, CbsBatteryState.CBS_RESET)
        self.assertEqual(self.provider.reset_calls, 1)

        p5 = _run(self.orch.handle(CbsBatteryEvent.VERIFY))
        self.assertEqual(self.orch.state, CbsBatteryState.VERIFIED)
        self.assertEqual(p5.payload["cbs_index"], 100)

        p6 = _run(self.orch.handle(CbsBatteryEvent.FINISH))
        self.assertEqual(self.orch.state, CbsBatteryState.DONE)
        self.assertTrue(p6.is_terminal)


class CbsBatteryFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = MockCbsServiceProvider()

    def test_lead_acid_on_f30_refused(self) -> None:
        """F30 with start/stop requires AGM/EFB — lead-acid would
        over-fry under cyclic regen charging."""
        orch = CbsBatteryOrchestrator(
            safety=MockSafetyGate(), provider=self.provider,
        )
        prompt = _run(orch.handle(CbsBatteryEvent.ENTER_BATTERY_INFO, {
            "vin": _VIN, "chassis": "F30",
            "type": "lead_acid", "capacity_ah": 80,
        }))
        self.assertEqual(orch.state, CbsBatteryState.FAILED)
        self.assertEqual(prompt.payload["error_code"],
                         "incompatible_battery_type")

    def test_invalid_capacity_refused(self) -> None:
        orch = CbsBatteryOrchestrator(
            safety=MockSafetyGate(), provider=self.provider,
        )
        prompt = _run(orch.handle(CbsBatteryEvent.ENTER_BATTERY_INFO, {
            "vin": _VIN, "chassis": "F30",
            "type": "agm", "capacity_ah": 999,   # over plausible range
        }))
        self.assertEqual(orch.state, CbsBatteryState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "invalid_battery_spec")

    def test_voltage_too_high_blocks_check_vehicle(self) -> None:
        safety = MockSafetyGate(voltage_v=14.0)   # alternator overlay
        orch = CbsBatteryOrchestrator(safety=safety, provider=self.provider)
        _run(orch.handle(CbsBatteryEvent.ENTER_BATTERY_INFO, {
            "vin": _VIN, "chassis": "F30",
            "type": "agm", "capacity_ah": 90,
        }))
        prompt = _run(orch.handle(CbsBatteryEvent.CHECK_VEHICLE))
        self.assertEqual(orch.state, CbsBatteryState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "vehicle_state_failed")

    def test_charging_system_dtc_blocks(self) -> None:
        safety = MockSafetyGate(recent_dtcs=("P0562",))
        orch = CbsBatteryOrchestrator(safety=safety, provider=self.provider)
        _run(orch.handle(CbsBatteryEvent.ENTER_BATTERY_INFO, {
            "vin": _VIN, "chassis": "F30",
            "type": "agm", "capacity_ah": 90,
        }))
        prompt = _run(orch.handle(CbsBatteryEvent.CHECK_VEHICLE))
        self.assertEqual(orch.state, CbsBatteryState.FAILED)

    def test_unknown_chassis_skips_compat_check_but_still_works(self) -> None:
        """Unknown chassis (e.g. a new G-series variant we haven't
        catalogued) should not BLOCK — the compat list is opt-in."""
        orch = CbsBatteryOrchestrator(
            safety=MockSafetyGate(voltage_v=12.7),
            provider=self.provider,
        )
        prompt = _run(orch.handle(CbsBatteryEvent.ENTER_BATTERY_INFO, {
            "vin": _VIN, "chassis": "G99",     # not in _CHASSIS_TYPES
            "type": "lead_acid", "capacity_ah": 80,
        }))
        self.assertEqual(orch.state, CbsBatteryState.BATTERY_INFO_OK)

    def test_provider_write_failure_marks_failed(self) -> None:
        provider = MockCbsServiceProvider(refuse_write=True)
        orch = CbsBatteryOrchestrator(
            safety=MockSafetyGate(voltage_v=12.7),
            provider=provider,
        )
        _run(orch.handle(CbsBatteryEvent.ENTER_BATTERY_INFO, {
            "vin": _VIN, "chassis": "F30",
            "type": "agm", "capacity_ah": 90,
        }))
        _run(orch.handle(CbsBatteryEvent.CHECK_VEHICLE))
        _run(orch.handle(CbsBatteryEvent.READ_OLD))
        prompt = _run(orch.handle(CbsBatteryEvent.WRITE_NEW))
        self.assertEqual(orch.state, CbsBatteryState.FAILED)


# ─────────────────────────────────────────────────────────────────────
# Snapshot / restore — one round-trip per orchestrator
# ─────────────────────────────────────────────────────────────────────
class SnapshotRestoreTests(unittest.TestCase):
    def test_egs_snapshot_round_trip(self) -> None:
        safety = MockSafetyGate()
        provider = MockEgsServiceProvider(initial_isn=bytes(range(0x10, 0x30)))
        a = EgsIsnOrchestrator(safety=safety, provider=provider)
        _run(a.handle(EgsIsnEvent.CHECK_PREREQS, {"vin": _VIN}))
        _run(a.handle(EgsIsnEvent.READ_BOUND_ISN))

        snap = a.snapshot()
        provider2 = MockEgsServiceProvider(
            initial_isn=bytes(range(0x10, 0x30)),
        )
        b = EgsIsnOrchestrator.restore(safety=safety,
                                       provider=provider2, snapshot=snap)
        self.assertEqual(b.state, EgsIsnState.CURRENT_ISN_READ)
        self.assertEqual(b.data.vin, _VIN)
        # Continue on the restored orchestrator.
        _run(b.handle(EgsIsnEvent.REQUEST_CLEAR))
        self.assertEqual(b.state, EgsIsnState.RESET_REQUESTED)

    def test_acsm_snapshot_round_trip(self) -> None:
        safety = MockSafetyGate(airbag_modules=(("d", AirbagModuleState.OK),))
        provider = MockAcsmServiceProvider(initial_record=_crash_record())
        a = AcsmCrashOrchestrator(safety=safety, provider=provider)
        _run(a.handle(AcsmCrashEvent.ASSESS_DAMAGE, {"vin": _VIN}))
        _run(a.handle(AcsmCrashEvent.READ_RECORD))
        _run(a.handle(AcsmCrashEvent.BACKUP))

        snap = a.snapshot()
        provider2 = MockAcsmServiceProvider(initial_record=_crash_record())
        b = AcsmCrashOrchestrator.restore(safety=safety,
                                          provider=provider2, snapshot=snap)
        self.assertEqual(b.state, AcsmCrashState.BACKUP_SAVED)
        self.assertEqual(b.data.vin, _VIN)
        # Continue: REQUEST_CLEAR on the restored orchestrator.
        _run(b.handle(AcsmCrashEvent.REQUEST_CLEAR))
        self.assertEqual(b.state, AcsmCrashState.CLEAR_REQUESTED)

    def test_cbs_snapshot_round_trip(self) -> None:
        safety = MockSafetyGate(voltage_v=12.7)
        provider = MockCbsServiceProvider()
        a = CbsBatteryOrchestrator(safety=safety, provider=provider)
        _run(a.handle(CbsBatteryEvent.ENTER_BATTERY_INFO, {
            "vin": _VIN, "chassis": "F30",
            "type": "agm", "capacity_ah": 90, "serial": "X1",
        }))
        _run(a.handle(CbsBatteryEvent.CHECK_VEHICLE))
        _run(a.handle(CbsBatteryEvent.READ_OLD))

        snap = a.snapshot()
        provider2 = MockCbsServiceProvider()
        b = CbsBatteryOrchestrator.restore(safety=safety,
                                           provider=provider2, snapshot=snap)
        self.assertEqual(b.state, CbsBatteryState.OLD_REG_READ)
        self.assertEqual(b.data.chassis, "F30")
        _run(b.handle(CbsBatteryEvent.WRITE_NEW))
        self.assertEqual(b.state, CbsBatteryState.NEW_REG_WRITTEN)
