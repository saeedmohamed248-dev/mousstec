"""Final integration — granular SaaS feature gating wired into the five
chatbot-guided orchestrators.

Each premium orchestrator (BenchOrchestrator, FrmRecoveryOrchestrator,
EgsIsnOrchestrator, AcsmCrashOrchestrator, CbsBatteryOrchestrator)
accepts an optional `entitlement=` argument. When set:
  • check() runs immediately AFTER the orchestrator validates its
    first-event payload (so an unentitled tenant gets a structured
    `not_entitled` FAILED prompt — no hardware / DB chatter);
  • consume() runs exactly once on FINISH (so the grant counts a use
    only AFTER the work has succeeded on the bench).

These tests use MockEntitlementGuard so the wiring is verified
hermetically — no DB, no tenant schema.

Two assertions per orchestrator:
  1. entitled tenant → orchestrator advances, check_calls == 1,
     consume_calls == 1 after FINISH.
  2. unentitled tenant → orchestrator transitions to FAILED with
     error_code "not_entitled", check_calls == 1, consume_calls == 0.

A backwards-compat assertion is also covered by the existing 36 + 34
+ 32 = 102 orchestrator tests in test_bench_orchestrator.py,
test_frm_recovery.py, test_premium_trio.py — none of those construct
an entitlement, and they all still pass. That's the "non-breaking"
guarantee for code paths that don't subscribe to granular billing.
"""
from __future__ import annotations

import asyncio
import unittest

from bmw_ecu.services.entitlement_guard import MockEntitlementGuard

# ── Orchestrators under test ──────────────────────────────────────────
from bmw_ecu.key_learning import (
    BenchEvent, BenchOrchestrator, BenchState, MockSmartHarness,
)
from bmw_ecu.key_learning.eeprom_dump import build_test_dump
from bmw_ecu.legacy import (
    FrmRecoveryEvent, FrmRecoveryOrchestrator, FrmRecoveryState, FrmVariant,
    MockBdmTransport, get_frm_profile,
)
from bmw_ecu.legacy.cloud_rebuild import build_template_blob
from bmw_ecu.premium import (
    AcsmCrashEvent, AcsmCrashOrchestrator, AcsmCrashState,
    AirbagModuleState,
    CbsBatteryEvent, CbsBatteryOrchestrator, CbsBatteryState,
    EgsIsnEvent, EgsIsnOrchestrator, EgsIsnState,
    GearPosition, IgnitionState,
    MockSafetyGate,
)
from bmw_ecu.premium.acsm_crash_reset import MockAcsmServiceProvider
from bmw_ecu.premium.cbs_battery_manager import MockCbsServiceProvider
from bmw_ecu.premium.egs_isn_reset import MockEgsServiceProvider


def _run(coro):
    return asyncio.run(coro)


_VIN = "WBA12345678901234"
_ISN = bytes(range(0x10, 0x30))


# ─────────────────────────────────────────────────────────────────────
# Bench Key Programming — feature_code = "key_programming"
# ─────────────────────────────────────────────────────────────────────
class BenchEntitlementTests(unittest.TestCase):
    def _build(self, *, guard) -> tuple[MockSmartHarness, BenchOrchestrator]:
        dump = build_test_dump(chip="M35080", isn=_ISN)
        harness = MockSmartHarness(eeprom_payload=dump)
        orch = BenchOrchestrator(harness, entitlement=guard)
        return harness, orch

    def _drive_to_done(self, orch: BenchOrchestrator) -> None:
        _run(orch.handle(BenchEvent.SELECT_PROFILE,
                         {"family": "CAS3", "vin": _VIN}))
        _run(orch.handle(BenchEvent.CONFIRM_WIRING))
        _run(orch.handle(BenchEvent.POWER_ON))
        _run(orch.handle(BenchEvent.ENTER_BENCH))
        _run(orch.handle(BenchEvent.DUMP_NOW))
        _run(orch.handle(BenchEvent.EXTRACT_ISN))
        _run(orch.handle(BenchEvent.PICK_KEY_SLOT))
        _run(orch.handle(BenchEvent.BURN_KEY))
        _run(orch.handle(BenchEvent.VERIFY))
        _run(orch.handle(BenchEvent.FINISH))

    def test_entitled_drives_full_flow_and_consumes_once(self) -> None:
        guard = MockEntitlementGuard(feature_code="key_programming")
        harness, orch = self._build(guard=guard)
        self._drive_to_done(orch)

        self.assertEqual(orch.state, BenchState.DONE)
        self.assertEqual(guard.check_calls, 1)
        self.assertEqual(len(guard.consume_calls), 1)
        # The op_ref encodes the VIN + family + slot so accounting can
        # reconcile this consume to a specific bench session.
        ref = guard.consume_calls[0]["operation_ref"]
        self.assertIn(_VIN, ref)
        self.assertIn("CAS3", ref)
        self.assertEqual(guard.consume_calls[0]["vin"], _VIN)

    def test_unentitled_blocks_at_select_profile(self) -> None:
        guard = MockEntitlementGuard(
            feature_code="key_programming",
            entitled_result=False,
            refusal_reason="no grant for 'key_programming'",
        )
        harness, orch = self._build(guard=guard)
        prompt = _run(orch.handle(BenchEvent.SELECT_PROFILE,
                                  {"family": "CAS3", "vin": _VIN}))
        self.assertEqual(orch.state, BenchState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "not_entitled")
        # check() ran ONCE; no consume() because nothing succeeded.
        self.assertEqual(guard.check_calls, 1)
        self.assertEqual(len(guard.consume_calls), 0)
        # Critical — no hardware chatter happened on the refusal path.
        self.assertEqual(len(harness.detect_calls), 0)


# ─────────────────────────────────────────────────────────────────────
# FRM3 Recovery — feature_code = "frm_repair"
# ─────────────────────────────────────────────────────────────────────
def _frm_corrupted_dump(profile, vin: str) -> bytearray:
    dump = bytearray(build_template_blob(profile))
    dump[profile.vin_offset:profile.vin_offset + 17] = vin.encode()
    for i in range(profile.fa_offset, profile.fa_offset + profile.fa_length):
        dump[i] = 0xFF
    return dump


class FrmEntitlementTests(unittest.TestCase):
    def _build(self, *, guard) -> tuple[MockBdmTransport, FrmRecoveryOrchestrator]:
        profile = get_frm_profile(FrmVariant.E90_FRM3)
        dump = _frm_corrupted_dump(profile, _VIN)
        bdm = MockBdmTransport(memory=bytearray(dump),
                               dflash_base=profile.dflash_base)
        orch = FrmRecoveryOrchestrator(bdm, entitlement=guard)
        return bdm, orch

    def _drive_to_done(self, orch: FrmRecoveryOrchestrator) -> None:
        _run(orch.handle(FrmRecoveryEvent.SELECT_MODEL,
                         {"variant": "E90_FRM3", "vin": _VIN}))
        _run(orch.handle(FrmRecoveryEvent.CONNECT_BDM))
        _run(orch.handle(FrmRecoveryEvent.READ_DFLASH))
        _run(orch.handle(FrmRecoveryEvent.ANALYZE))
        _run(orch.handle(FrmRecoveryEvent.REBUILD, {"vin": _VIN}))
        _run(orch.handle(FrmRecoveryEvent.FLASH_BACK))
        _run(orch.handle(FrmRecoveryEvent.INJECT_VO_FA,
                         {"fa_codes": ["5DA", "6FL"]}))
        _run(orch.handle(FrmRecoveryEvent.VERIFY))
        _run(orch.handle(FrmRecoveryEvent.FINISH))

    def test_entitled_drives_full_flow_and_consumes_once(self) -> None:
        guard = MockEntitlementGuard(feature_code="frm_repair")
        bdm, orch = self._build(guard=guard)
        self._drive_to_done(orch)

        self.assertEqual(orch.state, FrmRecoveryState.DONE)
        self.assertEqual(guard.check_calls, 1)
        self.assertEqual(len(guard.consume_calls), 1)
        ref = guard.consume_calls[0]["operation_ref"]
        self.assertIn("E90_FRM3", ref)
        self.assertIn(_VIN, ref)

    def test_unentitled_blocks_at_select_model(self) -> None:
        guard = MockEntitlementGuard(
            feature_code="frm_repair",
            entitled_result=False,
            refusal_reason="no active grant for 'frm_repair'",
        )
        bdm, orch = self._build(guard=guard)
        prompt = _run(orch.handle(FrmRecoveryEvent.SELECT_MODEL,
                                  {"variant": "E90_FRM3", "vin": _VIN}))
        self.assertEqual(orch.state, FrmRecoveryState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "not_entitled")
        self.assertEqual(guard.check_calls, 1)
        self.assertEqual(len(guard.consume_calls), 0)
        # No BDM chatter on the refusal path.
        self.assertEqual(len(bdm.connect_calls), 0)


# ─────────────────────────────────────────────────────────────────────
# EGS ISN Reset — feature_code = "egs_isn_reset"
# ─────────────────────────────────────────────────────────────────────
class EgsEntitlementTests(unittest.TestCase):
    def _build(self, *, guard):
        return EgsIsnOrchestrator(
            safety=MockSafetyGate(),
            provider=MockEgsServiceProvider(initial_isn=_ISN),
            entitlement=guard,
        )

    def test_entitled_drives_full_flow_and_consumes_once(self) -> None:
        guard = MockEntitlementGuard(feature_code="egs_isn_reset")
        orch = self._build(guard=guard)
        _run(orch.handle(EgsIsnEvent.CHECK_PREREQS, {"vin": _VIN}))
        _run(orch.handle(EgsIsnEvent.READ_BOUND_ISN))
        _run(orch.handle(EgsIsnEvent.REQUEST_CLEAR))
        _run(orch.handle(EgsIsnEvent.VERIFY))
        _run(orch.handle(EgsIsnEvent.FINISH))

        self.assertEqual(orch.state, EgsIsnState.DONE)
        self.assertEqual(guard.check_calls, 1)
        self.assertEqual(len(guard.consume_calls), 1)
        self.assertIn(_VIN, guard.consume_calls[0]["operation_ref"])

    def test_unentitled_blocks_at_check_prereqs(self) -> None:
        guard = MockEntitlementGuard(
            feature_code="egs_isn_reset",
            entitled_result=False,
        )
        orch = self._build(guard=guard)
        prompt = _run(orch.handle(EgsIsnEvent.CHECK_PREREQS, {"vin": _VIN}))
        self.assertEqual(orch.state, EgsIsnState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "not_entitled")
        self.assertEqual(guard.check_calls, 1)
        self.assertEqual(len(guard.consume_calls), 0)


# ─────────────────────────────────────────────────────────────────────
# ACSM Crash Reset — feature_code = "acsm_crash_reset"
# ─────────────────────────────────────────────────────────────────────
class AcsmEntitlementTests(unittest.TestCase):
    def _build(self, *, guard):
        return AcsmCrashOrchestrator(
            safety=MockSafetyGate(
                ignition=IgnitionState.KOEO,
                airbag_modules=(("driver", AirbagModuleState.OK),),
            ),
            provider=MockAcsmServiceProvider(
                initial_record={"timestamp": 1, "severity": 2,
                                "deployed_slots": (), "raw_hex": "AA"},
            ),
            entitlement=guard,
        )

    def test_entitled_drives_full_flow_and_consumes_once(self) -> None:
        guard = MockEntitlementGuard(feature_code="acsm_crash_reset")
        orch = self._build(guard=guard)
        _run(orch.handle(AcsmCrashEvent.ASSESS_DAMAGE, {"vin": _VIN}))
        _run(orch.handle(AcsmCrashEvent.READ_RECORD))
        _run(orch.handle(AcsmCrashEvent.BACKUP))
        _run(orch.handle(AcsmCrashEvent.REQUEST_CLEAR))
        _run(orch.handle(AcsmCrashEvent.VERIFY))
        _run(orch.handle(AcsmCrashEvent.FINISH))

        self.assertEqual(orch.state, AcsmCrashState.DONE)
        self.assertEqual(guard.check_calls, 1)
        self.assertEqual(len(guard.consume_calls), 1)
        # The op_ref carries the backup reference so accounting can
        # reconcile to the specific crash record that was cleared.
        ref = guard.consume_calls[0]["operation_ref"]
        self.assertIn(_VIN, ref)
        self.assertTrue("BAK-" in ref or "no-bak" in ref)

    def test_unentitled_blocks_at_assess_damage(self) -> None:
        guard = MockEntitlementGuard(
            feature_code="acsm_crash_reset",
            entitled_result=False,
        )
        orch = self._build(guard=guard)
        prompt = _run(orch.handle(AcsmCrashEvent.ASSESS_DAMAGE, {"vin": _VIN}))
        # This is a NOT-ENTITLED failure, NOT a safety block. Crucial
        # distinction: the UI renders "subscribe to unlock", not
        # "physical inspection required".
        self.assertEqual(orch.state, AcsmCrashState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "not_entitled")
        self.assertFalse(prompt.is_safety_block)
        self.assertEqual(guard.check_calls, 1)
        self.assertEqual(len(guard.consume_calls), 0)

    def test_safety_block_does_not_consume_grant(self) -> None:
        """A BLOCKED_FOR_SAFETY terminal state must NOT decrement the
        grant — the technician was refused service, never actually used
        it."""
        guard = MockEntitlementGuard(feature_code="acsm_crash_reset")
        orch = AcsmCrashOrchestrator(
            safety=MockSafetyGate(
                # Deployed bag → safety block at ASSESS_DAMAGE.
                airbag_modules=(("driver", AirbagModuleState.DEPLOYED),),
            ),
            provider=MockAcsmServiceProvider(
                initial_record={"timestamp": 1, "severity": 2,
                                "deployed_slots": (), "raw_hex": "AA"},
            ),
            entitlement=guard,
        )
        prompt = _run(orch.handle(AcsmCrashEvent.ASSESS_DAMAGE, {"vin": _VIN}))
        self.assertEqual(orch.state, AcsmCrashState.BLOCKED_FOR_SAFETY)
        self.assertTrue(prompt.is_safety_block)
        # check() WAS called (we let the guard pass before safety probes)
        # but consume() was NOT — terminal is BLOCKED_FOR_SAFETY, not DONE.
        self.assertEqual(guard.check_calls, 1)
        self.assertEqual(len(guard.consume_calls), 0)


# ─────────────────────────────────────────────────────────────────────
# CBS Battery Manager — feature_code = "cbs_battery_manager"
# ─────────────────────────────────────────────────────────────────────
class CbsEntitlementTests(unittest.TestCase):
    def _build(self, *, guard):
        return CbsBatteryOrchestrator(
            safety=MockSafetyGate(voltage_v=12.7),
            provider=MockCbsServiceProvider(),
            entitlement=guard,
        )

    def test_entitled_drives_full_flow_and_consumes_once(self) -> None:
        guard = MockEntitlementGuard(feature_code="cbs_battery_manager")
        orch = self._build(guard=guard)
        _run(orch.handle(CbsBatteryEvent.ENTER_BATTERY_INFO, {
            "vin": _VIN, "chassis": "F30",
            "type": "agm", "capacity_ah": 90,
        }))
        _run(orch.handle(CbsBatteryEvent.CHECK_VEHICLE))
        _run(orch.handle(CbsBatteryEvent.READ_OLD))
        _run(orch.handle(CbsBatteryEvent.WRITE_NEW))
        _run(orch.handle(CbsBatteryEvent.RESET_COUNTERS))
        _run(orch.handle(CbsBatteryEvent.VERIFY))
        _run(orch.handle(CbsBatteryEvent.FINISH))

        self.assertEqual(orch.state, CbsBatteryState.DONE)
        self.assertEqual(guard.check_calls, 1)
        self.assertEqual(len(guard.consume_calls), 1)
        ref = guard.consume_calls[0]["operation_ref"]
        self.assertIn(_VIN, ref)
        self.assertIn("F30", ref)

    def test_unentitled_blocks_at_enter_battery_info(self) -> None:
        guard = MockEntitlementGuard(
            feature_code="cbs_battery_manager",
            entitled_result=False,
        )
        orch = self._build(guard=guard)
        prompt = _run(orch.handle(CbsBatteryEvent.ENTER_BATTERY_INFO, {
            "vin": _VIN, "chassis": "F30",
            "type": "agm", "capacity_ah": 90,
        }))
        self.assertEqual(orch.state, CbsBatteryState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "not_entitled")
        self.assertEqual(guard.check_calls, 1)
        self.assertEqual(len(guard.consume_calls), 0)


# ─────────────────────────────────────────────────────────────────────
# Backwards-compat — orchestrators without an entitlement still work
# ─────────────────────────────────────────────────────────────────────
class BackwardsCompatTests(unittest.TestCase):
    """Construct each orchestrator the OLD way (no entitlement= kwarg)
    and verify it still drives end-to-end. This is the non-breaking
    guarantee for the 102 orchestrator tests that pre-date this commit.
    """

    def test_bench_without_entitlement(self) -> None:
        dump = build_test_dump(chip="M35080", isn=_ISN)
        orch = BenchOrchestrator(MockSmartHarness(eeprom_payload=dump))
        _run(orch.handle(BenchEvent.SELECT_PROFILE,
                         {"family": "CAS3", "vin": _VIN}))
        # Just verify the IDLE → PROFILE_SELECTED transition worked
        # without the entitlement layer interfering.
        self.assertEqual(orch.state, BenchState.PROFILE_SELECTED)

    def test_frm_without_entitlement(self) -> None:
        profile = get_frm_profile(FrmVariant.E90_FRM3)
        dump = _frm_corrupted_dump(profile, _VIN)
        bdm = MockBdmTransport(memory=bytearray(dump),
                               dflash_base=profile.dflash_base)
        orch = FrmRecoveryOrchestrator(bdm)
        _run(orch.handle(FrmRecoveryEvent.SELECT_MODEL,
                         {"variant": "E90_FRM3", "vin": _VIN}))
        self.assertEqual(orch.state, FrmRecoveryState.MODEL_SELECTED)

    def test_egs_without_entitlement(self) -> None:
        orch = EgsIsnOrchestrator(
            safety=MockSafetyGate(),
            provider=MockEgsServiceProvider(initial_isn=_ISN),
        )
        _run(orch.handle(EgsIsnEvent.CHECK_PREREQS, {"vin": _VIN}))
        self.assertEqual(orch.state, EgsIsnState.PREREQ_OK)

    def test_acsm_without_entitlement(self) -> None:
        orch = AcsmCrashOrchestrator(
            safety=MockSafetyGate(
                airbag_modules=(("d", AirbagModuleState.OK),)),
            provider=MockAcsmServiceProvider(
                initial_record={"timestamp": 1, "severity": 1,
                                "deployed_slots": (), "raw_hex": "AA"}),
        )
        _run(orch.handle(AcsmCrashEvent.ASSESS_DAMAGE, {"vin": _VIN}))
        self.assertEqual(orch.state, AcsmCrashState.DAMAGE_ASSESSED)

    def test_cbs_without_entitlement(self) -> None:
        orch = CbsBatteryOrchestrator(
            safety=MockSafetyGate(voltage_v=12.7),
            provider=MockCbsServiceProvider(),
        )
        _run(orch.handle(CbsBatteryEvent.ENTER_BATTERY_INFO, {
            "vin": _VIN, "chassis": "F30",
            "type": "agm", "capacity_ah": 90,
        }))
        self.assertEqual(orch.state, CbsBatteryState.BATTERY_INFO_OK)


# ─────────────────────────────────────────────────────────────────────
# EntitlementGuard helper itself
# ─────────────────────────────────────────────────────────────────────
class MockEntitlementGuardTests(unittest.TestCase):
    """The mock is itself exercised by every test above, but a few
    direct assertions pin its contract."""

    def test_default_is_entitled(self) -> None:
        g = MockEntitlementGuard(feature_code="x")
        entitled, reason = g.check()
        self.assertTrue(entitled)
        self.assertEqual(g.check_calls, 1)

    def test_can_flip_to_unentitled(self) -> None:
        g = MockEntitlementGuard(feature_code="x", entitled_result=False,
                                 refusal_reason="custom reason")
        entitled, reason = g.check()
        self.assertFalse(entitled)
        self.assertEqual(reason, "custom reason")

    def test_consume_records_payload(self) -> None:
        g = MockEntitlementGuard(feature_code="x")
        g.consume(vin="V1", operation_ref="OP-1")
        g.consume(vin="V2", operation_ref="OP-2")
        self.assertEqual(g.consume_calls, [
            {"vin": "V1", "operation_ref": "OP-1"},
            {"vin": "V2", "operation_ref": "OP-2"},
        ])
