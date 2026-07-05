"""Engine-swap diagnosis — pure-Python unit tests (no Django, no hardware).

Covers the real scenario the technician hit: an N18 DME swapped into an
R56, car won't start, ISTA reads N14. The diagnosis must:
  • flag the no-start as an ISN (immobiliser) mismatch, not a coding fault;
  • flag align-ISN as BENCH-only for MEVD17/N18 (bot can't do it over OBD);
  • flag the FA N14→N18 update as OBD (the bot CAN do it over the cable);
  • never claim the car will start while the ISN pair mismatches;
  • stay honest when an ISN read is virgin/unreadable.
"""
from __future__ import annotations

import unittest

from bmw_ecu.repair.swap_diagnosis import (
    diagnose_engine_swap,
    SwapAction,
)


CAS_ISN = bytes(range(0x10, 0x30))       # 32-byte car immobiliser ISN
DME_ISN_DIFFERENT = bytes(range(0x40, 0x60))  # used DME carries another ISN


class EngineSwapDiagnosisTests(unittest.TestCase):
    def _actions_by_key(self, diag) -> dict[str, SwapAction]:
        return {a.key: a for a in diag.actions}

    def test_n18_swap_no_start_is_isn_mismatch_bench_only(self) -> None:
        diag = diagnose_engine_swap(
            cas_isn=CAS_ISN, dme_isn=DME_ISN_DIFFERENT,
            fa_engine="N14", dme_reported_engine="N18",
            dme_requires_bench=True,            # MEVD17/N18
        )
        self.assertTrue(diag.isn_mismatch)
        self.assertFalse(diag.will_start)
        acts = self._actions_by_key(diag)
        # align_isn present, bench-only, bot cannot do it over the cable.
        self.assertIn("align_isn", acts)
        self.assertEqual(acts["align_isn"].where, "bench")
        self.assertFalse(acts["align_isn"].bot_can_do)

    def test_fa_engine_mismatch_is_obd_and_bot_can_do(self) -> None:
        diag = diagnose_engine_swap(
            cas_isn=CAS_ISN, dme_isn=DME_ISN_DIFFERENT,
            fa_engine="N14", dme_reported_engine="N18",
            dme_requires_bench=True,
        )
        self.assertTrue(diag.fa_engine_mismatch)
        acts = self._actions_by_key(diag)
        self.assertIn("update_fa", acts)
        self.assertEqual(acts["update_fa"].where, "obd")
        self.assertTrue(acts["update_fa"].bot_can_do)
        # The summary names both engines so the tech sees the swap at a glance.
        self.assertIn("N14", diag.summary_ar)
        self.assertIn("N18", diag.summary_ar)

    def test_matching_isn_means_immobiliser_not_the_cause(self) -> None:
        diag = diagnose_engine_swap(
            cas_isn=CAS_ISN, dme_isn=CAS_ISN,
            fa_engine="N18", dme_reported_engine="N18",
            dme_requires_bench=True,
        )
        self.assertFalse(diag.isn_mismatch)
        self.assertTrue(diag.will_start)
        self.assertEqual(diag.actions, ())     # nothing to remediate

    def test_virgin_isn_is_unconfirmed_not_a_false_match(self) -> None:
        diag = diagnose_engine_swap(
            cas_isn=CAS_ISN, dme_isn=b"\xFF" * 32,   # unreadable / virgin
            fa_engine="N18", dme_reported_engine="N18",
            dme_requires_bench=True,
        )
        self.assertFalse(diag.isn_mismatch)
        self.assertTrue(diag.isn_unconfirmed)
        self.assertFalse(diag.will_start)          # can't promise a start
        self.assertIn("align_isn", self._actions_by_key(diag))

    def test_non_bench_dme_allows_obd_isn_alignment(self) -> None:
        # A DME whose ISN IS writable over UDS (requires_bench=False) → the
        # align step is OBD and the bot can drive it over the cable.
        diag = diagnose_engine_swap(
            cas_isn=CAS_ISN, dme_isn=DME_ISN_DIFFERENT,
            fa_engine="N20", dme_reported_engine="N20",
            dme_requires_bench=False,
        )
        acts = self._actions_by_key(diag)
        self.assertEqual(acts["align_isn"].where, "obd")
        self.assertTrue(acts["align_isn"].bot_can_do)


if __name__ == "__main__":
    unittest.main()
