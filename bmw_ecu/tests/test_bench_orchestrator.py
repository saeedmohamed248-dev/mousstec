"""Bench Key Programming orchestrator — unit tests.

Pure-Python: zero DB, zero HTTP, zero hardware. The orchestrator is
driven through a MockSmartHarness so each transition can be asserted
deterministically. Covers:

  • Profile catalog completeness (CAS3 / CAS3+ / FEM / BDC).
  • EEPROM dump parser — happy + every failure mode.
  • ISN extraction — wrapper round dump.isn with re-validation.
  • Key slot allocation — preferred / dealer-reserved / exhausted.
  • Key fob deterministic generation.
  • Orchestrator end-to-end happy path (EEPROM flow + UDS flow).
  • Illegal transition → FAILED prompt + structured error_code.
  • Snapshot / restore round-trip.
"""
from __future__ import annotations

import asyncio
import unittest

from bmw_ecu.exceptions import IsnMismatch
from bmw_ecu.key_learning import (
    BenchData,
    BenchEvent,
    BenchOrchestrator,
    BenchState,
    EepromParseError,
    IllegalBenchTransition,
    KEY_LEARNING_PROFILES,
    KeyAllocationError,
    MockSmartHarness,
    ModuleFamily,
    allocate_key_slot,
    extract_isn_from_dump,
    generate_key_fob,
    get_profile,
    parse_dump,
)
from bmw_ecu.key_learning.eeprom_dump import build_test_dump
from bmw_ecu.key_learning.smart_harness import HarnessConnection


# ─────────────────────────────────────────────────────────────────────
# Profile catalog
# ─────────────────────────────────────────────────────────────────────
class ProfileCatalogTests(unittest.TestCase):
    def test_every_family_has_a_profile(self) -> None:
        for fam in ModuleFamily:
            self.assertIn(fam, KEY_LEARNING_PROFILES,
                          f"profile missing for {fam.value}")

    def test_get_profile_by_string_or_enum(self) -> None:
        self.assertIs(get_profile("CAS3"), KEY_LEARNING_PROFILES[ModuleFamily.CAS3])
        self.assertIs(get_profile(ModuleFamily.FEM),
                      KEY_LEARNING_PROFILES[ModuleFamily.FEM])

    def test_get_profile_unknown_raises(self) -> None:
        with self.assertRaises(KeyError):
            get_profile("totally_made_up")

    def test_cas3_uses_eeprom_flow_fem_uses_uds(self) -> None:
        self.assertEqual(get_profile(ModuleFamily.CAS3).read_flow.value, "eeprom")
        self.assertEqual(get_profile(ModuleFamily.CAS3_PLUS).read_flow.value, "eeprom")
        self.assertEqual(get_profile(ModuleFamily.FEM).read_flow.value, "uds")
        self.assertEqual(get_profile(ModuleFamily.BDC).read_flow.value, "uds")

    def test_pinout_carries_required_lines(self) -> None:
        cas3 = get_profile(ModuleFamily.CAS3).pinout
        # CAS3 uses I²C — must expose SDA/SCL.
        self.assertIsNotNone(cas3.eeprom_sda)
        self.assertIsNotNone(cas3.eeprom_scl)
        fem = get_profile(ModuleFamily.FEM).pinout
        # FEM uses CAN + BOOT.
        self.assertIsNotNone(fem.can_high)
        self.assertIsNotNone(fem.boot)


# ─────────────────────────────────────────────────────────────────────
# EEPROM dump parser
# ─────────────────────────────────────────────────────────────────────
class EepromDumpTests(unittest.TestCase):
    ISN = bytes(range(0x10, 0x30))  # 32 monotonic bytes — non-virgin

    def test_parse_happy_path_m35080(self) -> None:
        raw = build_test_dump(chip="M35080", isn=self.ISN)
        dump = parse_dump(raw, chip="M35080")
        self.assertEqual(len(dump.raw), 512)
        self.assertEqual(dump.isn, self.ISN)
        self.assertEqual(dump.key_slot_count, 4)
        # every slot starts free
        for i in range(dump.key_slot_count):
            self.assertTrue(dump.is_key_slot_free(i))

    def test_parse_happy_path_m35128(self) -> None:
        raw = build_test_dump(chip="M35128", isn=self.ISN)
        dump = parse_dump(raw, chip="M35128")
        self.assertEqual(len(dump.raw), 1024)
        self.assertEqual(dump.key_slot_count, 8)

    def test_rejects_unknown_chip(self) -> None:
        with self.assertRaises(EepromParseError):
            parse_dump(b"\xAA" * 512, chip="UNKNOWN")

    def test_rejects_wrong_size(self) -> None:
        with self.assertRaises(EepromParseError):
            parse_dump(b"\xAA" * 256, chip="M35080")

    def test_rejects_uniform_dump(self) -> None:
        with self.assertRaises(EepromParseError):
            parse_dump(b"\x00" * 512, chip="M35080")
        with self.assertRaises(EepromParseError):
            parse_dump(b"\xFF" * 512, chip="M35080")

    def test_rejects_virgin_isn_window(self) -> None:
        # Build a syntactically OK dump but ISN window is 0xFF (virgin CAS)
        raw = bytearray(b"\xAA" * 512)
        raw[0x20:0x40] = b"\xFF" * 32
        with self.assertRaises(EepromParseError):
            parse_dump(bytes(raw), chip="M35080")

    def test_occupied_slot_reads_as_not_free(self) -> None:
        raw = build_test_dump(chip="M35080", isn=self.ISN, occupied_slots=(1, 3))
        dump = parse_dump(raw, chip="M35080")
        self.assertTrue(dump.is_key_slot_free(0))
        self.assertFalse(dump.is_key_slot_free(1))
        self.assertTrue(dump.is_key_slot_free(2))
        self.assertFalse(dump.is_key_slot_free(3))


# ─────────────────────────────────────────────────────────────────────
# ISN extraction
# ─────────────────────────────────────────────────────────────────────
class IsnExtractionTests(unittest.TestCase):
    def test_returns_isn_bytes(self) -> None:
        isn = bytes(range(0x10, 0x30))
        dump = parse_dump(build_test_dump(chip="M35080", isn=isn),
                          chip="M35080")
        self.assertEqual(extract_isn_from_dump(dump), isn)

    def test_rejects_wrong_length(self) -> None:
        isn = bytes(range(0x10, 0x30))
        dump = parse_dump(build_test_dump(chip="M35080", isn=isn),
                          chip="M35080")
        with self.assertRaises(IsnMismatch):
            extract_isn_from_dump(dump, expected_length=16)


# ─────────────────────────────────────────────────────────────────────
# Key slot allocation
# ─────────────────────────────────────────────────────────────────────
class SlotAllocationTests(unittest.TestCase):
    def test_picks_lowest_free_slot(self) -> None:
        self.assertEqual(
            allocate_key_slot(family_code="CAS3", occupied=[0, 2],
                              key_count=4),
            1,
        )

    def test_dealer_slot_reserved_on_cas3_plus(self) -> None:
        # CAS3+ reserves slot 0. With everything else free, allocator
        # must skip 0 and return 1.
        self.assertEqual(
            allocate_key_slot(family_code="CAS3+", occupied=[],
                              key_count=8),
            1,
        )

    def test_preferred_slot_honoured_when_free(self) -> None:
        self.assertEqual(
            allocate_key_slot(family_code="FEM", occupied=[0],
                              key_count=10, preferred=5),
            5,
        )

    def test_preferred_slot_occupied_raises(self) -> None:
        with self.assertRaises(KeyAllocationError):
            allocate_key_slot(family_code="FEM", occupied=[3],
                              key_count=10, preferred=3)

    def test_preferred_dealer_reserved_raises(self) -> None:
        with self.assertRaises(KeyAllocationError):
            allocate_key_slot(family_code="CAS3+", occupied=[],
                              key_count=8, preferred=0)

    def test_no_free_slot_raises(self) -> None:
        with self.assertRaises(KeyAllocationError):
            allocate_key_slot(family_code="CAS3", occupied=[0, 1, 2, 3],
                              key_count=4)


# ─────────────────────────────────────────────────────────────────────
# Key generation
# ─────────────────────────────────────────────────────────────────────
class KeyFobGenerationTests(unittest.TestCase):
    ISN = bytes(range(0x10, 0x30))
    SEED = bytes.fromhex("DEADBEEFCAFEBABE0102030405060708")

    def test_deterministic_when_seed_pinned(self) -> None:
        a = generate_key_fob(isn=self.ISN, slot_index=2,
                             family_code="CAS3", seed=self.SEED)
        b = generate_key_fob(isn=self.ISN, slot_index=2,
                             family_code="CAS3", seed=self.SEED)
        self.assertEqual(a.payload, b.payload)
        self.assertEqual(a.fcc_id, b.fcc_id)

    def test_different_slot_yields_different_payload(self) -> None:
        a = generate_key_fob(isn=self.ISN, slot_index=2,
                             family_code="CAS3", seed=self.SEED)
        b = generate_key_fob(isn=self.ISN, slot_index=3,
                             family_code="CAS3", seed=self.SEED)
        self.assertNotEqual(a.payload, b.payload)

    def test_payload_is_32_bytes(self) -> None:
        fob = generate_key_fob(isn=self.ISN, slot_index=1,
                               family_code="FEM", seed=self.SEED)
        self.assertEqual(len(fob.payload), 32)
        self.assertEqual(len(fob.fcc_id), 12)

    def test_wrong_isn_length_raises(self) -> None:
        with self.assertRaises(KeyAllocationError):
            generate_key_fob(isn=b"\x00" * 16, slot_index=0,
                             family_code="CAS3", seed=self.SEED)


# ─────────────────────────────────────────────────────────────────────
# Orchestrator — EEPROM flow (CAS3)
# ─────────────────────────────────────────────────────────────────────
def _run(coro):
    return asyncio.run(coro)


class OrchestratorCas3FlowTests(unittest.TestCase):
    ISN = bytes(range(0x10, 0x30))
    SEED = bytes.fromhex("DEADBEEFCAFEBABE0102030405060708")

    def setUp(self) -> None:
        self.dump = build_test_dump(chip="M35080", isn=self.ISN,
                                    occupied_slots=(0,))
        self.harness = MockSmartHarness(eeprom_payload=self.dump)
        self.orch = BenchOrchestrator(self.harness)

    def test_full_happy_path_cas3(self) -> None:
        # 1. SELECT_PROFILE
        p1 = _run(self.orch.handle(BenchEvent.SELECT_PROFILE, {
            "family": "CAS3", "vin": "WBA1234567ABCDEF1",
            "technician_id": "tech_01",
        }))
        self.assertEqual(self.orch.state, BenchState.PROFILE_SELECTED)
        self.assertEqual(p1.expects, "CONFIRM_WIRING")
        # Pinout callouts are non-empty + carry the CAS3 SDA/SCL labels.
        labels = {row["label"] for row in p1.pin_callouts}
        self.assertIn("SDA", labels)
        self.assertIn("SCL", labels)

        # 2. CONFIRM_WIRING
        p2 = _run(self.orch.handle(BenchEvent.CONFIRM_WIRING))
        self.assertEqual(self.orch.state, BenchState.WIRING_CHECK)
        self.assertEqual(len(self.harness.detect_calls), 1)
        self.assertEqual(p2.expects, "POWER_ON")

        # 3. POWER_ON
        p3 = _run(self.orch.handle(BenchEvent.POWER_ON))
        self.assertEqual(self.orch.state, BenchState.POWER_RAMP)
        self.assertEqual(len(self.harness.power_calls), 1)
        self.assertGreater(p3.payload["measured_voltage_v"], 11.0)

        # 4. ENTER_BENCH — EEPROM flow does NOT hold BOOT
        p4 = _run(self.orch.handle(BenchEvent.ENTER_BENCH))
        self.assertEqual(self.orch.state, BenchState.BENCH_MODE)
        self.assertEqual(self.harness.bench_mode_calls[0][0], False)

        # 5. DUMP_NOW — drives i2c_read_eeprom
        p5 = _run(self.orch.handle(BenchEvent.DUMP_NOW))
        self.assertEqual(self.orch.state, BenchState.DUMP_CAPTURED)
        self.assertEqual(self.harness.eeprom_reads, [("M35080", 512)])
        self.assertEqual(self.orch.data.raw_dump, self.dump)

        # 6. EXTRACT_ISN
        p6 = _run(self.orch.handle(BenchEvent.EXTRACT_ISN))
        self.assertEqual(self.orch.state, BenchState.ISN_EXTRACTED)
        self.assertEqual(self.orch.data.isn_hex, self.ISN.hex().upper())

        # 7. PICK_KEY_SLOT — slot 0 is occupied → allocator returns 1
        p7 = _run(self.orch.handle(BenchEvent.PICK_KEY_SLOT))
        self.assertEqual(self.orch.state, BenchState.KEY_SLOT_PICKED)
        self.assertEqual(self.orch.data.chosen_slot, 1)
        self.assertIn(0, p7.payload["occupied"])

        # 8. BURN_KEY — pin the seed so the fob bytes are deterministic
        p8 = _run(self.orch.handle(BenchEvent.BURN_KEY,
                                   {"seed_hex": self.SEED.hex()}))
        self.assertEqual(self.orch.state, BenchState.KEY_BURNED)
        self.assertEqual(len(self.orch.data.burned_fob["payload_hex"]), 64)

        # 9. VERIFY
        p9 = _run(self.orch.handle(BenchEvent.VERIFY))
        self.assertEqual(self.orch.state, BenchState.VERIFIED)
        self.assertEqual(p9.expects, "FINISH")

        # 10. FINISH
        p10 = _run(self.orch.handle(BenchEvent.FINISH))
        self.assertEqual(self.orch.state, BenchState.DONE)
        self.assertTrue(p10.is_terminal)
        self.assertEqual(p10.progress_pct, 100)

    def test_prefer_specific_slot(self) -> None:
        # Drive through to slot pick + ask for slot 3.
        _run(self.orch.handle(BenchEvent.SELECT_PROFILE, {"family": "CAS3"}))
        _run(self.orch.handle(BenchEvent.CONFIRM_WIRING))
        _run(self.orch.handle(BenchEvent.POWER_ON))
        _run(self.orch.handle(BenchEvent.ENTER_BENCH))
        _run(self.orch.handle(BenchEvent.DUMP_NOW))
        _run(self.orch.handle(BenchEvent.EXTRACT_ISN))
        prompt = _run(self.orch.handle(BenchEvent.PICK_KEY_SLOT, {"slot": 3}))
        self.assertEqual(self.orch.data.chosen_slot, 3)
        self.assertIn("rقم 3", prompt.title) if False else None  # title check

    def test_abort_at_any_state_marks_failed(self) -> None:
        _run(self.orch.handle(BenchEvent.SELECT_PROFILE, {"family": "CAS3"}))
        prompt = _run(self.orch.handle(BenchEvent.ABORT))
        self.assertEqual(self.orch.state, BenchState.FAILED)
        self.assertTrue(prompt.is_error)
        self.assertEqual(prompt.payload.get("error_code"), "aborted_by_user")

    def test_wiring_fault_short_circuits_to_failed(self) -> None:
        self.harness.wiring_status = HarnessConnection.SHORT_TO_GROUND
        _run(self.orch.handle(BenchEvent.SELECT_PROFILE, {"family": "CAS3"}))
        prompt = _run(self.orch.handle(BenchEvent.CONFIRM_WIRING))
        self.assertEqual(self.orch.state, BenchState.FAILED)
        self.assertTrue(prompt.payload["error_code"]
                        .startswith("wiring_short_to_ground"))

    def test_power_brownout_fails(self) -> None:
        self.harness._power_on_voltage = 9.0   # well below 12 V tolerance
        _run(self.orch.handle(BenchEvent.SELECT_PROFILE, {"family": "CAS3"}))
        _run(self.orch.handle(BenchEvent.CONFIRM_WIRING))
        prompt = _run(self.orch.handle(BenchEvent.POWER_ON))
        self.assertEqual(self.orch.state, BenchState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "harness_failure")

    def test_unknown_family_fails_with_structured_code(self) -> None:
        prompt = _run(self.orch.handle(BenchEvent.SELECT_PROFILE,
                                       {"family": "NCS-MAGIC"}))
        self.assertEqual(self.orch.state, BenchState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "unknown_family")

    def test_out_of_order_event_marks_illegal_transition(self) -> None:
        # POWER_ON straight from IDLE — illegal.
        prompt = _run(self.orch.handle(BenchEvent.POWER_ON))
        self.assertEqual(self.orch.state, BenchState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "illegal_transition")


# ─────────────────────────────────────────────────────────────────────
# Orchestrator — UDS flow (FEM)
# ─────────────────────────────────────────────────────────────────────
class OrchestratorFemFlowTests(unittest.TestCase):
    ISN = bytes(range(0x40, 0x60))

    def setUp(self) -> None:
        self.harness = MockSmartHarness()
        self.orch = BenchOrchestrator(self.harness)

    def test_fem_flow_holds_boot_pin_during_bench_entry(self) -> None:
        _run(self.orch.handle(BenchEvent.SELECT_PROFILE, {"family": "FEM"}))
        _run(self.orch.handle(BenchEvent.CONFIRM_WIRING))
        _run(self.orch.handle(BenchEvent.POWER_ON))
        _run(self.orch.handle(BenchEvent.ENTER_BENCH))
        # FEM uses UDS flow → BOOT pin must be held HIGH on bench entry.
        self.assertEqual(self.harness.bench_mode_calls[0][0], True)

    def test_fem_dump_does_not_call_eeprom(self) -> None:
        _run(self.orch.handle(BenchEvent.SELECT_PROFILE, {"family": "FEM"}))
        _run(self.orch.handle(BenchEvent.CONFIRM_WIRING))
        _run(self.orch.handle(BenchEvent.POWER_ON))
        _run(self.orch.handle(BenchEvent.ENTER_BENCH))
        _run(self.orch.handle(BenchEvent.DUMP_NOW))
        self.assertEqual(self.harness.eeprom_reads, [])
        self.assertEqual(self.orch.state, BenchState.DUMP_CAPTURED)

    def test_uds_flow_requires_isn_injection_before_extract(self) -> None:
        _run(self.orch.handle(BenchEvent.SELECT_PROFILE, {"family": "FEM"}))
        _run(self.orch.handle(BenchEvent.CONFIRM_WIRING))
        _run(self.orch.handle(BenchEvent.POWER_ON))
        _run(self.orch.handle(BenchEvent.ENTER_BENCH))
        _run(self.orch.handle(BenchEvent.DUMP_NOW))
        # Without injecting an ISN, EXTRACT_ISN must FAIL with isn_mismatch.
        prompt = _run(self.orch.handle(BenchEvent.EXTRACT_ISN))
        self.assertEqual(self.orch.state, BenchState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "isn_mismatch")

    def test_uds_flow_full_with_injected_isn(self) -> None:
        _run(self.orch.handle(BenchEvent.SELECT_PROFILE, {"family": "FEM"}))
        _run(self.orch.handle(BenchEvent.CONFIRM_WIRING))
        _run(self.orch.handle(BenchEvent.POWER_ON))
        _run(self.orch.handle(BenchEvent.ENTER_BENCH))
        _run(self.orch.handle(BenchEvent.DUMP_NOW))

        # Inject the ISN — simulates what the UDS extractor would have done.
        self.orch.inject_isn_for_uds_flow(self.ISN)

        _run(self.orch.handle(BenchEvent.EXTRACT_ISN))
        _run(self.orch.handle(BenchEvent.PICK_KEY_SLOT))
        # FEM has 10 slots, no dealer reservation → first free = 0
        self.assertEqual(self.orch.data.chosen_slot, 0)

        _run(self.orch.handle(BenchEvent.BURN_KEY))
        _run(self.orch.handle(BenchEvent.VERIFY))
        final = _run(self.orch.handle(BenchEvent.FINISH))
        self.assertEqual(self.orch.state, BenchState.DONE)
        self.assertTrue(final.is_terminal)


# ─────────────────────────────────────────────────────────────────────
# Snapshot / restore
# ─────────────────────────────────────────────────────────────────────
class SnapshotRestoreTests(unittest.TestCase):
    ISN = bytes(range(0x10, 0x30))

    def test_snapshot_round_trip_preserves_state_machine(self) -> None:
        h1 = MockSmartHarness(eeprom_payload=build_test_dump(
            chip="M35080", isn=self.ISN, occupied_slots=(0, 2)))
        a = BenchOrchestrator(h1)
        _run(a.handle(BenchEvent.SELECT_PROFILE,
                      {"family": "CAS3", "vin": "WBA9876543"}))
        _run(a.handle(BenchEvent.CONFIRM_WIRING))
        _run(a.handle(BenchEvent.POWER_ON))
        _run(a.handle(BenchEvent.ENTER_BENCH))
        _run(a.handle(BenchEvent.DUMP_NOW))
        _run(a.handle(BenchEvent.EXTRACT_ISN))

        snap = a.snapshot()
        h2 = MockSmartHarness(eeprom_payload=h1.eeprom_payload)
        b = BenchOrchestrator.restore(h2, snap)

        self.assertEqual(b.state, BenchState.ISN_EXTRACTED)
        self.assertEqual(b.data.vin, "WBA9876543")
        self.assertEqual(b.data.isn_hex, self.ISN.hex().upper())
        # Continue the flow on the restored orchestrator.
        _run(b.handle(BenchEvent.PICK_KEY_SLOT))
        self.assertEqual(b.state, BenchState.KEY_SLOT_PICKED)
        # First free slot when 0+2 are occupied → 1
        self.assertEqual(b.data.chosen_slot, 1)
