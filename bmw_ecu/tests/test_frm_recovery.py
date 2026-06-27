"""FRM3 Footwell Module recovery — unit tests.

Pure-Python, zero hardware. Drives the orchestrator end-to-end through
MockBdmTransport + a synthesised corrupted dump. Covers:

  • Profile catalog (E90 / E70 / R56)
  • D-Flash corruption analyzer (healthy / partial / severe / unreadable)
  • Cloud rebuild (VIN injection, FA carry-over, checksum recompute)
  • SALAPA / FA payload builder (validation, dedupe, size caps)
  • Orchestrator happy path + every failure mode
  • Snapshot / restore round-trip
"""
from __future__ import annotations

import asyncio
import unittest

from bmw_ecu.legacy import (
    AbstractBdmTransport,
    BdmConnectionError,
    BdmReadError,
    BdmWriteError,
    CloudRebuildError,
    CorruptionLevel,
    FRM_PROFILES,
    FaPayload,
    FrmRecoveryEvent,
    FrmRecoveryOrchestrator,
    FrmRecoveryState,
    FrmVariant,
    MockBdmTransport,
    SalapaInjectionError,
    analyze_dflash,
    build_fa_payload,
    get_frm_profile,
    parse_salapa,
    rebuild_dflash,
)
from bmw_ecu.legacy.cloud_rebuild import build_template_blob
from bmw_ecu.legacy.dflash_corruption import _xor_checksum


def _run(coro):
    return asyncio.run(coro)


# Convenience VIN that passes the validator.
_VIN = "WBA12345678901234"


# ─────────────────────────────────────────────────────────────────────
# Profile catalog
# ─────────────────────────────────────────────────────────────────────
class FrmProfileCatalogTests(unittest.TestCase):
    def test_every_variant_has_profile(self) -> None:
        for v in FrmVariant:
            self.assertIn(v, FRM_PROFILES, f"profile missing for {v.value}")

    def test_get_profile_by_string(self) -> None:
        self.assertIs(get_frm_profile("E90_FRM3"),
                      FRM_PROFILES[FrmVariant.E90_FRM3])

    def test_get_profile_unknown_raises(self) -> None:
        with self.assertRaises(KeyError):
            get_frm_profile("NCS_MAGIC")

    def test_mini_r56_has_smaller_fa_block(self) -> None:
        # Critical detail — get this wrong and the checksum
        # over-counts on rebuild.
        self.assertEqual(get_frm_profile(FrmVariant.R56_FRM3).fa_length, 192)
        self.assertEqual(get_frm_profile(FrmVariant.E90_FRM3).fa_length, 256)


# ─────────────────────────────────────────────────────────────────────
# D-Flash corruption analyzer
# ─────────────────────────────────────────────────────────────────────
class CorruptionAnalyzerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = get_frm_profile(FrmVariant.E90_FRM3)
        # Start from a template — by construction, healthy.
        self.healthy = bytearray(build_template_blob(self.profile))
        # Inject a real VIN so analyzer flags vin_recoverable.
        self.healthy[self.profile.vin_offset:
                     self.profile.vin_offset + 17] = _VIN.encode()
        # Recompute checksum after VIN edit so the dump stays internally
        # consistent.
        body = bytes(self.healthy[:self.profile.checksum_offset])
        self.healthy[self.profile.checksum_offset] = _xor_checksum(body)

    def test_healthy_dump_classified_clean(self) -> None:
        report = analyze_dflash(bytes(self.healthy), profile=self.profile)
        self.assertEqual(report.level, CorruptionLevel.HEALTHY)
        self.assertTrue(report.vin_recoverable)
        self.assertTrue(report.fa_recoverable)
        self.assertTrue(report.checksum_ok)
        self.assertFalse(report.needs_rebuild)
        self.assertEqual(report.confidence, 1.0)

    def test_wrong_size_is_unreadable(self) -> None:
        report = analyze_dflash(bytes(256), profile=self.profile)
        self.assertEqual(report.level, CorruptionLevel.UNREADABLE)
        self.assertEqual(report.confidence, 0.0)

    def test_uniform_ff_is_unreadable(self) -> None:
        report = analyze_dflash(b"\xFF" * self.profile.dflash_size,
                                profile=self.profile)
        self.assertEqual(report.level, CorruptionLevel.UNREADABLE)

    def test_corrupted_fa_partial(self) -> None:
        # Wipe the FA block → analyzer should call PARTIAL. The
        # template's synthetic FA happens to XOR to the same byte as
        # a 0xFF wipe (parity quirk), so we ALSO flip one body byte
        # to force checksum_ok=False — that's a more realistic field
        # corruption signature than a clean FA wipe.
        corrupt = bytearray(self.healthy)
        for i in range(self.profile.fa_offset,
                       self.profile.fa_offset + self.profile.fa_length):
            corrupt[i] = 0xFF
        corrupt[0x600] ^= 0xAA       # force XOR mismatch
        report = analyze_dflash(bytes(corrupt), profile=self.profile)
        self.assertEqual(report.level, CorruptionLevel.PARTIAL)
        self.assertTrue(report.vin_recoverable)
        self.assertFalse(report.fa_recoverable)
        self.assertFalse(report.checksum_ok)

    def test_garbled_vin_severe(self) -> None:
        corrupt = bytearray(self.healthy)
        # Wipe VIN window with non-VIN bytes.
        corrupt[self.profile.vin_offset:
                self.profile.vin_offset + 17] = b"\x80" * 17
        # Add some uniform runs (brown-out signature) to push down past PARTIAL
        for run in range(3):
            start = 0x300 + run * 0x400
            for i in range(start, start + 300):
                corrupt[i] = 0xFF
        report = analyze_dflash(bytes(corrupt), profile=self.profile)
        # Per the heuristic: vin garbled + ≥3 uniform runs → severe at best.
        self.assertIn(report.level,
                      (CorruptionLevel.SEVERE, CorruptionLevel.UNREADABLE))
        self.assertFalse(report.vin_recoverable)


# ─────────────────────────────────────────────────────────────────────
# Cloud rebuild
# ─────────────────────────────────────────────────────────────────────
class CloudRebuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = get_frm_profile(FrmVariant.E90_FRM3)
        self.dump = b"\xFF" * self.profile.dflash_size

    def test_rebuild_injects_vin(self) -> None:
        result = rebuild_dflash(profile=self.profile,
                                corrupted_dump=self.dump, vin=_VIN)
        rebuilt = result.rebuilt_bytes
        self.assertEqual(len(rebuilt), self.profile.dflash_size)
        vin_window = rebuilt[self.profile.vin_offset:
                             self.profile.vin_offset + 17]
        self.assertEqual(vin_window, _VIN.encode())

    def test_rebuild_checksum_is_correct(self) -> None:
        result = rebuild_dflash(profile=self.profile,
                                corrupted_dump=self.dump, vin=_VIN)
        body = result.rebuilt_bytes[:self.profile.checksum_offset]
        self.assertEqual(
            result.rebuilt_bytes[self.profile.checksum_offset],
            _xor_checksum(body),
        )

    def test_rebuild_carries_over_fa_when_recoverable(self) -> None:
        # Stage a dump with a recognisable pattern in the FA window.
        dump = bytearray(b"\xFF" * self.profile.dflash_size)
        marker = bytes(i & 0xFE for i in range(self.profile.fa_length))
        dump[self.profile.fa_offset:
             self.profile.fa_offset + self.profile.fa_length] = marker
        result = rebuild_dflash(profile=self.profile,
                                corrupted_dump=bytes(dump),
                                vin=_VIN, fa_recoverable=True)
        self.assertTrue(result.fa_carried_over)
        rebuilt_fa = result.rebuilt_bytes[
            self.profile.fa_offset:
            self.profile.fa_offset + self.profile.fa_length]
        self.assertEqual(rebuilt_fa, marker)

    def test_rebuild_falls_back_to_template_fa(self) -> None:
        result = rebuild_dflash(profile=self.profile,
                                corrupted_dump=self.dump,
                                vin=_VIN, fa_recoverable=False)
        self.assertFalse(result.fa_carried_over)

    def test_rebuild_rejects_bad_vin(self) -> None:
        with self.assertRaises(CloudRebuildError):
            rebuild_dflash(profile=self.profile,
                           corrupted_dump=self.dump, vin="too-short")
        with self.assertRaises(CloudRebuildError):
            rebuild_dflash(profile=self.profile,
                           corrupted_dump=self.dump,
                           vin="wba12345678901234")  # lowercase

    def test_rebuild_rejects_wrong_dump_length(self) -> None:
        with self.assertRaises(CloudRebuildError):
            rebuild_dflash(profile=self.profile,
                           corrupted_dump=b"\xFF" * 256, vin=_VIN)


# ─────────────────────────────────────────────────────────────────────
# SALAPA / FA payload
# ─────────────────────────────────────────────────────────────────────
class SalapaPayloadTests(unittest.TestCase):
    def test_parse_normalises_and_dedupes(self) -> None:
        codes = parse_salapa(["5da", "  6FL", "5DA", "", None, "524"])
        self.assertEqual(sorted(c.code for c in codes), ["524", "5DA", "6FL"])

    def test_invalid_code_raises(self) -> None:
        with self.assertRaises(SalapaInjectionError):
            parse_salapa(["abcd"])    # 4 chars
        with self.assertRaises(SalapaInjectionError):
            parse_salapa(["5d!"])     # punctuation

    def test_build_payload_sorts_and_terminates(self) -> None:
        payload: FaPayload = build_fa_payload(["6FL", "5DA", "524"])
        self.assertEqual(payload.sorted_codes, ("524", "5DA", "6FL"))
        self.assertEqual(payload.raw_bytes, b"5245DA6FL\x00")

    def test_build_payload_empty_raises(self) -> None:
        with self.assertRaises(SalapaInjectionError):
            build_fa_payload([])

    def test_build_payload_too_big_raises(self) -> None:
        # 100 random codes vs max_codes=80
        codes = [f"{i:03d}" for i in range(100)]
        with self.assertRaises(SalapaInjectionError):
            build_fa_payload(codes, max_codes=80)

    def test_build_payload_respects_max_bytes_cap(self) -> None:
        # 30 codes × 3 bytes + nul = 91 bytes > max_bytes=64
        codes = [f"{i:03d}" for i in range(30)]
        with self.assertRaises(SalapaInjectionError):
            build_fa_payload(codes, max_codes=80, max_bytes=64)


# ─────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────
def _make_corrupted_dump(profile, vin: str) -> bytearray:
    """A plausibly-corrupted dump: VIN survives, FA is wiped, checksum
    no longer matches body — analyzer should classify PARTIAL."""
    dump = bytearray(build_template_blob(profile))
    dump[profile.vin_offset:profile.vin_offset + 17] = vin.encode()
    # Wipe the FA window — corrupted by brown-out.
    for i in range(profile.fa_offset, profile.fa_offset + profile.fa_length):
        dump[i] = 0xFF
    # Don't recompute checksum — emulates the after-corruption state.
    return dump


class OrchestratorHappyPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = get_frm_profile(FrmVariant.E90_FRM3)
        self.dump = _make_corrupted_dump(self.profile, _VIN)
        # Memory passed in is shared between read/write so the verify
        # step sees what flash_back wrote.
        self.bdm = MockBdmTransport(memory=bytearray(self.dump),
                                    dflash_base=self.profile.dflash_base)
        self.orch = FrmRecoveryOrchestrator(self.bdm)

    def test_full_happy_path_e90(self) -> None:
        p1 = _run(self.orch.handle(FrmRecoveryEvent.SELECT_MODEL, {
            "variant": "E90_FRM3", "vin": _VIN, "technician_id": "tech_99",
        }))
        self.assertEqual(self.orch.state, FrmRecoveryState.MODEL_SELECTED)
        self.assertEqual(p1.expects, "CONNECT_BDM")
        self.assertEqual(p1.payload["bdm_clock_khz"], 800)

        p2 = _run(self.orch.handle(FrmRecoveryEvent.CONNECT_BDM))
        self.assertEqual(self.orch.state, FrmRecoveryState.BDM_CONNECTED)
        self.assertEqual(len(self.bdm.connect_calls), 1)
        self.assertEqual(self.bdm.connect_calls[0]["reset_low_ms"], 100)

        p3 = _run(self.orch.handle(FrmRecoveryEvent.READ_DFLASH))
        self.assertEqual(self.orch.state, FrmRecoveryState.DFLASH_READ)
        self.assertEqual(len(self.bdm.read_calls), 1)
        self.assertEqual(self.bdm.read_calls[0][1], self.profile.dflash_size)

        p4 = _run(self.orch.handle(FrmRecoveryEvent.ANALYZE))
        self.assertEqual(self.orch.state, FrmRecoveryState.CORRUPTION_ANALYZED)
        self.assertEqual(p4.payload["level"], "partial")
        self.assertTrue(p4.payload["vin_recoverable"])
        self.assertFalse(p4.payload["fa_recoverable"])

        p5 = _run(self.orch.handle(FrmRecoveryEvent.REBUILD,
                                   {"vin": _VIN}))
        self.assertEqual(self.orch.state, FrmRecoveryState.CLOUD_REBUILT)
        # Sanity: rebuilt blob VIN survived round-trip
        rebuilt = self.orch.data.rebuilt_bytes
        self.assertEqual(
            rebuilt[self.profile.vin_offset:
                    self.profile.vin_offset + 17].decode(),
            _VIN,
        )

        p6 = _run(self.orch.handle(FrmRecoveryEvent.FLASH_BACK))
        self.assertEqual(self.orch.state, FrmRecoveryState.DFLASH_FLASHED)
        self.assertEqual(len(self.bdm.write_calls), 1)
        self.assertEqual(self.bdm.write_calls[0][1],
                         self.profile.dflash_size)

        p7 = _run(self.orch.handle(FrmRecoveryEvent.INJECT_VO_FA,
                                   {"fa_codes": ["5DA", "6FL", "524"]}))
        self.assertEqual(self.orch.state, FrmRecoveryState.VO_FA_INJECTED)
        self.assertEqual(list(self.orch.data.fa_codes),
                         ["524", "5DA", "6FL"])

        p8 = _run(self.orch.handle(FrmRecoveryEvent.VERIFY))
        self.assertEqual(self.orch.state, FrmRecoveryState.VERIFIED)

        p9 = _run(self.orch.handle(FrmRecoveryEvent.FINISH))
        self.assertEqual(self.orch.state, FrmRecoveryState.DONE)
        self.assertTrue(p9.is_terminal)
        self.assertEqual(p9.progress_pct, 100)
        self.assertEqual(p9.payload["fa_codes"], ["524", "5DA", "6FL"])


class OrchestratorFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = get_frm_profile(FrmVariant.E90_FRM3)
        self.dump = _make_corrupted_dump(self.profile, _VIN)

    def _select_model(self, orch, variant="E90_FRM3"):
        return _run(orch.handle(FrmRecoveryEvent.SELECT_MODEL,
                                {"variant": variant, "vin": _VIN}))

    def test_bdm_connect_failure_marks_failed(self) -> None:
        bdm = MockBdmTransport(memory=bytearray(self.dump),
                               dflash_base=self.profile.dflash_base,
                               simulate_no_connect=True)
        orch = FrmRecoveryOrchestrator(bdm)
        self._select_model(orch)
        prompt = _run(orch.handle(FrmRecoveryEvent.CONNECT_BDM))
        self.assertEqual(orch.state, FrmRecoveryState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "bdm_connection_error")

    def test_unreadable_dump_blocks_rebuild(self) -> None:
        # Memory is all 0xFF — analyzer must declare UNREADABLE.
        bdm = MockBdmTransport(
            memory=bytearray(b"\xFF" * self.profile.dflash_size),
            dflash_base=self.profile.dflash_base,
        )
        orch = FrmRecoveryOrchestrator(bdm)
        self._select_model(orch)
        _run(orch.handle(FrmRecoveryEvent.CONNECT_BDM))
        _run(orch.handle(FrmRecoveryEvent.READ_DFLASH))
        prompt = _run(orch.handle(FrmRecoveryEvent.ANALYZE))
        self.assertEqual(orch.state, FrmRecoveryState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "dump_unreadable")

    def test_unknown_variant_fails_with_structured_code(self) -> None:
        bdm = MockBdmTransport(memory=bytearray(self.dump),
                               dflash_base=self.profile.dflash_base)
        orch = FrmRecoveryOrchestrator(bdm)
        prompt = _run(orch.handle(FrmRecoveryEvent.SELECT_MODEL,
                                  {"variant": "NCS-MAGIC", "vin": _VIN}))
        self.assertEqual(orch.state, FrmRecoveryState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "unknown_variant")

    def test_illegal_transition_marks_failed(self) -> None:
        bdm = MockBdmTransport(memory=bytearray(self.dump),
                               dflash_base=self.profile.dflash_base)
        orch = FrmRecoveryOrchestrator(bdm)
        # Try CONNECT_BDM without SELECT_MODEL first.
        prompt = _run(orch.handle(FrmRecoveryEvent.CONNECT_BDM))
        self.assertEqual(orch.state, FrmRecoveryState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "illegal_transition")

    def test_write_verify_failure_marks_failed(self) -> None:
        bdm = MockBdmTransport(
            memory=bytearray(self.dump),
            dflash_base=self.profile.dflash_base,
            simulate_write_error_at=self.profile.dflash_base + 0x100,
        )
        orch = FrmRecoveryOrchestrator(bdm)
        self._select_model(orch)
        _run(orch.handle(FrmRecoveryEvent.CONNECT_BDM))
        _run(orch.handle(FrmRecoveryEvent.READ_DFLASH))
        _run(orch.handle(FrmRecoveryEvent.ANALYZE))
        _run(orch.handle(FrmRecoveryEvent.REBUILD, {"vin": _VIN}))
        prompt = _run(orch.handle(FrmRecoveryEvent.FLASH_BACK))
        self.assertEqual(orch.state, FrmRecoveryState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "bdm_write_error")

    def test_abort_at_any_state_marks_failed(self) -> None:
        bdm = MockBdmTransport(memory=bytearray(self.dump),
                               dflash_base=self.profile.dflash_base)
        orch = FrmRecoveryOrchestrator(bdm)
        self._select_model(orch)
        prompt = _run(orch.handle(FrmRecoveryEvent.ABORT))
        self.assertEqual(orch.state, FrmRecoveryState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "aborted_by_user")

    def test_empty_fa_codes_at_inject_step_fails(self) -> None:
        bdm = MockBdmTransport(memory=bytearray(self.dump),
                               dflash_base=self.profile.dflash_base)
        orch = FrmRecoveryOrchestrator(bdm)
        self._select_model(orch)
        _run(orch.handle(FrmRecoveryEvent.CONNECT_BDM))
        _run(orch.handle(FrmRecoveryEvent.READ_DFLASH))
        _run(orch.handle(FrmRecoveryEvent.ANALYZE))
        _run(orch.handle(FrmRecoveryEvent.REBUILD, {"vin": _VIN}))
        _run(orch.handle(FrmRecoveryEvent.FLASH_BACK))
        prompt = _run(orch.handle(FrmRecoveryEvent.INJECT_VO_FA,
                                  {"fa_codes": []}))
        self.assertEqual(orch.state, FrmRecoveryState.FAILED)
        self.assertEqual(prompt.payload["error_code"], "salapa_error")


# ─────────────────────────────────────────────────────────────────────
# Snapshot / restore
# ─────────────────────────────────────────────────────────────────────
class SnapshotRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = get_frm_profile(FrmVariant.R56_FRM3)
        self.dump = _make_corrupted_dump(self.profile, _VIN)
        self.bdm = MockBdmTransport(memory=bytearray(self.dump),
                                    dflash_base=self.profile.dflash_base)
        self.orch = FrmRecoveryOrchestrator(self.bdm)

    def test_round_trip_preserves_state(self) -> None:
        _run(self.orch.handle(FrmRecoveryEvent.SELECT_MODEL,
                              {"variant": "R56_FRM3", "vin": _VIN}))
        _run(self.orch.handle(FrmRecoveryEvent.CONNECT_BDM))
        _run(self.orch.handle(FrmRecoveryEvent.READ_DFLASH))
        _run(self.orch.handle(FrmRecoveryEvent.ANALYZE))

        snap = self.orch.snapshot()
        bdm2 = MockBdmTransport(memory=bytearray(self.dump),
                                dflash_base=self.profile.dflash_base)
        # Restored orchestrator needs a connected BDM to continue.
        _run(bdm2.connect(bdm_clock_khz=self.profile.bdm_clock_khz,
                          reset_low_ms=self.profile.reset_low_ms))
        b = FrmRecoveryOrchestrator.restore(bdm2, snap)
        self.assertEqual(b.state, FrmRecoveryState.CORRUPTION_ANALYZED)
        self.assertEqual(b.data.variant, FrmVariant.R56_FRM3)
        self.assertEqual(b.data.vin, _VIN)
        self.assertEqual(b.data.raw_dump, bytes(self.dump))

        # Continue: REBUILD on the restored orchestrator works.
        _run(b.handle(FrmRecoveryEvent.REBUILD, {"vin": _VIN}))
        self.assertEqual(b.state, FrmRecoveryState.CLOUD_REBUILT)


# ─────────────────────────────────────────────────────────────────────
# BDM transport — mock-specific safety nets
# ─────────────────────────────────────────────────────────────────────
class MockBdmTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = get_frm_profile(FrmVariant.E90_FRM3)
        self.bdm = MockBdmTransport(
            memory=bytearray(b"\xAA" * self.profile.dflash_size),
            dflash_base=self.profile.dflash_base,
        )

    def test_read_before_connect_raises(self) -> None:
        with self.assertRaises(BdmReadError):
            _run(self.bdm.read_dflash(address=self.profile.dflash_base,
                                      length=8))

    def test_write_before_connect_raises(self) -> None:
        with self.assertRaises(BdmWriteError):
            _run(self.bdm.write_dflash(address=self.profile.dflash_base,
                                       data=b"\x00\x01"))

    def test_address_out_of_range_raises(self) -> None:
        _run(self.bdm.connect())
        with self.assertRaises(BdmReadError):
            # Way past the end of the window.
            _run(self.bdm.read_dflash(address=self.profile.dflash_base
                                      + self.profile.dflash_size + 100,
                                      length=1))

    def test_round_trip_byte(self) -> None:
        _run(self.bdm.connect())
        addr = self.profile.dflash_base + 0x100
        _run(self.bdm.write_byte(addr, 0x42))
        self.assertEqual(_run(self.bdm.read_byte(addr)), 0x42)
