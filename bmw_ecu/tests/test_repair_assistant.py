"""AI Repair Assistant — pure-Python, zero DB, zero LLM, zero hardware.

Drives the self-verifying loop (MockRepairBrain Generator+Verifier) through
the RepairAssistantOrchestrator, asserting:
  • the knowledge base is well-formed and its DTC/PID keys line up with
    scan.dtc_decoder + scan.live_data,
  • the Generator only emits KB-grounded hypotheses, ranked by confidence,
  • the Verifier corroborates on a matching live anomaly, weakens on an
    in-range reading, and marks a hypothesis CONTRADICTED when live data
    refutes it,
  • the confidence threshold splits accepted vs needs-review,
  • entitlement (check at ANALYZE, consume once on FINISH; never consume on
    a failed/blocked analysis),
  • the failure + illegal-transition + snapshot/restore paths.
"""
from __future__ import annotations

import asyncio
import unittest

from bmw_ecu.repair import (
    AI_REPAIR_FEATURE,
    DEFAULT_THRESHOLD,
    AssistantEvent,
    AssistantState,
    Critique,
    Hypothesis,
    MockRepairBrain,
    RepairAssistantOrchestrator,
    RepairCase,
    all_entries,
    combine_confidence,
    entries_for_dtc,
    entries_for_symptom,
    get_entry,
)
from bmw_ecu.scan.dtc_decoder import _KNOWN as DTC_KNOWN
from bmw_ecu.scan.live_data import PID_CATALOG
from bmw_ecu.services.entitlement_guard import MockEntitlementGuard


def _run(coro):
    return asyncio.run(coro)


_VIN = "WBA12345678901234"


def _orch(*, brain=None, entitlement=None):
    return RepairAssistantOrchestrator(
        brain=brain or MockRepairBrain(), entitlement=entitlement)


# ─────────────────────────────────────────────────────────────────────
# Knowledge base
# ─────────────────────────────────────────────────────────────────────
class KnowledgeBaseTests(unittest.TestCase):
    def test_entries_well_formed(self) -> None:
        for e in all_entries():
            with self.subTest(e.key):
                self.assertTrue(e.cause_ar and e.cause_en)
                self.assertTrue(e.fix_ar and e.fix_en)
                self.assertTrue(e.trigger_dtcs or e.trigger_symptoms)
                self.assertGreaterEqual(e.base_confidence, 0.0)
                self.assertLessEqual(e.base_confidence, 1.0)

    def test_confirm_pids_exist_in_live_catalog(self) -> None:
        for e in all_entries():
            for p in e.confirm_pids:
                with self.subTest(f"{e.key}:{p}"):
                    self.assertIn(p, PID_CATALOG)

    def test_trigger_dtcs_are_plausible_codes(self) -> None:
        # Every trigger DTC is a SAE-style code; many are in the decoder KB.
        overlap = 0
        for e in all_entries():
            for c in e.trigger_dtcs:
                self.assertRegex(c, r"^[PCBU][0-9A-F]{4}$")
                if c in DTC_KNOWN:
                    overlap += 1
        self.assertGreater(overlap, 0)

    def test_entries_for_dtc(self) -> None:
        keys = {e.key for e in entries_for_dtc("P0301")}
        self.assertIn("coil_pack_failure", keys)
        self.assertIn("spark_plug_worn", keys)

    def test_entries_for_symptom_matches_substring(self) -> None:
        keys = {e.key for e in entries_for_symptom("العميل بيقول فيه خبط واضح")}
        self.assertIn("coil_pack_failure", keys)

    def test_entries_for_symptom_empty(self) -> None:
        self.assertEqual(entries_for_symptom(""), ())
        self.assertEqual(entries_for_symptom("   "), ())

    def test_safety_entries_flagged(self) -> None:
        self.assertTrue(get_entry("crash_data_stored").needs_safety_note)
        self.assertTrue(get_entry("wheel_speed_sensor").needs_safety_note)
        self.assertFalse(get_entry("coil_pack_failure").needs_safety_note)


# ─────────────────────────────────────────────────────────────────────
# combine_confidence
# ─────────────────────────────────────────────────────────────────────
class CombineConfidenceTests(unittest.TestCase):
    def test_neutral_evidence_preserves_prior(self) -> None:
        self.assertAlmostEqual(combine_confidence(0.6, 0.5, contradicted=False),
                               0.6, places=3)

    def test_corroboration_lifts(self) -> None:
        self.assertGreater(combine_confidence(0.6, 1.0, contradicted=False),
                           0.6)

    def test_refutation_cuts(self) -> None:
        self.assertLess(combine_confidence(0.6, 0.0, contradicted=False), 0.6)

    def test_contradicted_floors_to_zero(self) -> None:
        self.assertEqual(combine_confidence(0.95, 1.0, contradicted=True), 0.0)

    def test_clamped_to_ceiling(self) -> None:
        self.assertLessEqual(combine_confidence(0.95, 1.0, contradicted=False),
                             0.98)


# ─────────────────────────────────────────────────────────────────────
# Brain — Generator + Verifier
# ─────────────────────────────────────────────────────────────────────
class BrainTests(unittest.TestCase):
    def test_generator_only_emits_grounded(self) -> None:
        brain = MockRepairBrain()
        case = RepairCase(dtc_codes=("P0301",))
        hyps = brain.generate(case)
        self.assertTrue(hyps)
        for h in hyps:
            self.assertIsNotNone(get_entry(h.entry_key))

    def test_generator_unknown_code_yields_nothing(self) -> None:
        brain = MockRepairBrain()
        self.assertEqual(brain.generate(RepairCase(dtc_codes=("P9999",))), [])

    def test_generator_sorted_desc(self) -> None:
        brain = MockRepairBrain()
        hyps = brain.generate(RepairCase(dtc_codes=("P0301", "P0171")))
        confs = [h.generator_confidence for h in hyps]
        self.assertEqual(confs, sorted(confs, reverse=True))

    def test_multiple_signals_raise_prior(self) -> None:
        brain = MockRepairBrain()
        only_dtc = brain.generate(RepairCase(dtc_codes=("P0301",)))
        coil_a = next(h for h in only_dtc if h.entry_key == "coil_pack_failure")
        both = brain.generate(RepairCase(dtc_codes=("P0301",),
                                         symptoms="فيه خبط"))
        coil_b = next(h for h in both if h.entry_key == "coil_pack_failure")
        self.assertGreater(coil_b.generator_confidence,
                           coil_a.generator_confidence)

    def test_verifier_corroborates_on_anomaly(self) -> None:
        brain = MockRepairBrain()
        case = RepairCase(dtc_codes=("P0171",), anomaly_pids=("stft_b1",))
        h = next(x for x in brain.generate(case) if x.entry_key == "vacuum_leak")
        crit = brain.verify(case, h)
        self.assertGreater(crit.evidence_score, 0.5)
        self.assertFalse(crit.contradicted)

    def test_verifier_weakens_on_normal_reading(self) -> None:
        brain = MockRepairBrain()
        case = RepairCase(dtc_codes=("P0171",), normal_pids=("stft_b1",))
        h = next(x for x in brain.generate(case) if x.entry_key == "vacuum_leak")
        crit = brain.verify(case, h)
        self.assertLess(crit.evidence_score, 0.5)

    def test_verifier_contradicts_when_all_refuted(self) -> None:
        brain = MockRepairBrain()
        case = RepairCase(dtc_codes=("P0128",), normal_pids=("coolant_temp",))
        h = next(x for x in brain.generate(case)
                 if x.entry_key == "thermostat_stuck_open")
        crit = brain.verify(case, h)
        self.assertTrue(crit.contradicted)

    def test_verifier_rejects_ungrounded_hypothesis(self) -> None:
        brain = MockRepairBrain()
        fake = Hypothesis(
            entry_key="does_not_exist", cause_ar="", cause_en="",
            fix_ar="", fix_en="", parts=(), matched_dtcs=(),
            matched_symptoms=(), generator_confidence=0.9)
        crit = brain.verify(RepairCase(dtc_codes=("P0301",)), fake)
        self.assertFalse(crit.grounded)
        self.assertTrue(crit.contradicted)

    def test_call_counters(self) -> None:
        brain = MockRepairBrain()
        case = RepairCase(dtc_codes=("P0301",))
        hyps = brain.generate(case)
        for h in hyps:
            brain.verify(case, h)
        self.assertEqual(brain.generate_calls, 1)
        self.assertEqual(brain.verify_calls, len(hyps))


# ─────────────────────────────────────────────────────────────────────
# Assistant — happy path + ranking
# ─────────────────────────────────────────────────────────────────────
class AssistantHappyPathTests(unittest.TestCase):
    def test_analyze_then_finish(self) -> None:
        o = _orch()
        p = _run(o.handle(AssistantEvent.ANALYZE,
                          {"vin": _VIN, "dtc_codes": ["P0301"],
                           "symptoms": "خبط"}))
        self.assertEqual(p.state, AssistantState.ANALYZED)
        self.assertGreaterEqual(p.payload["plan"]["accepted_count"], 1)
        p = _run(o.handle(AssistantEvent.FINISH))
        self.assertEqual(p.state, AssistantState.DONE)
        self.assertTrue(p.is_terminal)

    def test_recommendations_ranked_one_based(self) -> None:
        o = _orch()
        p = _run(o.handle(AssistantEvent.ANALYZE,
                          {"dtc_codes": ["P0301", "P0171"]}))
        recs = p.payload["plan"]["recommendations"]
        self.assertEqual([r["rank"] for r in recs],
                         list(range(1, len(recs) + 1)))
        finals = [r["final_confidence"] for r in recs]
        self.assertEqual(finals, sorted(finals, reverse=True))

    def test_corroborating_anomaly_promotes_to_accepted(self) -> None:
        # vacuum_leak below threshold on its own, accepted once corroborated.
        bare = _orch()
        p1 = _run(bare.handle(AssistantEvent.ANALYZE,
                              {"dtc_codes": ["P0171"]}))
        vac_bare = next(r for r in p1.payload["plan"]["recommendations"]
                        if r["hypothesis"]["entry_key"] == "vacuum_leak")

        boosted = _orch()
        p2 = _run(boosted.handle(AssistantEvent.ANALYZE,
                                 {"dtc_codes": ["P0171"],
                                  "anomaly_pids": ["stft_b1", "ltft_b1"]}))
        vac_boost = next(r for r in p2.payload["plan"]["recommendations"]
                         if r["hypothesis"]["entry_key"] == "vacuum_leak")
        self.assertGreater(vac_boost["final_confidence"],
                           vac_bare["final_confidence"])

    def test_contradicted_lands_in_needs_review(self) -> None:
        o = _orch()
        p = _run(o.handle(AssistantEvent.ANALYZE,
                          {"dtc_codes": ["P0128"],
                           "normal_pids": ["coolant_temp"]}))
        rec = next(r for r in p.payload["plan"]["recommendations"]
                   if r["hypothesis"]["entry_key"] == "thermostat_stuck_open")
        self.assertFalse(rec["accepted"])
        self.assertTrue(rec["critique"]["contradicted"])

    def test_threshold_override_changes_split(self) -> None:
        # A very high threshold pushes everything into needs-review.
        o = _orch()
        p = _run(o.handle(AssistantEvent.ANALYZE,
                          {"dtc_codes": ["P0301"], "threshold": 0.99}))
        self.assertEqual(p.payload["plan"]["accepted_count"], 0)

    def test_safety_item_flagged_in_plan(self) -> None:
        o = _orch()
        p = _run(o.handle(AssistantEvent.ANALYZE, {"dtc_codes": ["B1018"]}))
        self.assertTrue(p.payload["plan"]["has_safety_item"])


# ─────────────────────────────────────────────────────────────────────
# Assistant — failure paths
# ─────────────────────────────────────────────────────────────────────
class AssistantFailureTests(unittest.TestCase):
    def test_no_evidence(self) -> None:
        o = _orch()
        p = _run(o.handle(AssistantEvent.ANALYZE, {}))
        self.assertEqual(p.state, AssistantState.FAILED)
        self.assertEqual(p.payload["error_code"], "no_evidence")

    def test_finish_before_analyze_illegal(self) -> None:
        o = _orch()
        p = _run(o.handle(AssistantEvent.FINISH))
        self.assertEqual(p.payload["error_code"], "illegal_transition")

    def test_double_analyze_illegal(self) -> None:
        o = _orch()
        _run(o.handle(AssistantEvent.ANALYZE, {"dtc_codes": ["P0301"]}))
        p = _run(o.handle(AssistantEvent.ANALYZE, {"dtc_codes": ["P0301"]}))
        self.assertEqual(p.payload["error_code"], "illegal_transition")

    def test_abort(self) -> None:
        o = _orch()
        _run(o.handle(AssistantEvent.ANALYZE, {"dtc_codes": ["P0301"]}))
        p = _run(o.handle(AssistantEvent.ABORT))
        self.assertEqual(p.state, AssistantState.FAILED)

    def test_unknown_event(self) -> None:
        o = _orch()
        p = _run(o.handle("diagnose_please"))
        self.assertEqual(p.payload["error_code"], "unknown_event")

    def test_unknown_codes_still_analyze_with_empty_plan(self) -> None:
        # Evidence present (a code) but nothing in the KB → empty, honest plan.
        o = _orch()
        p = _run(o.handle(AssistantEvent.ANALYZE, {"dtc_codes": ["P9999"]}))
        self.assertEqual(p.state, AssistantState.ANALYZED)
        self.assertEqual(p.payload["plan"]["recommendations"], [])
        self.assertIn("مفيش سبب واضح", p.payload["plan"]["headline_ar"])


# ─────────────────────────────────────────────────────────────────────
# Assistant — entitlement
# ─────────────────────────────────────────────────────────────────────
class AssistantEntitlementTests(unittest.TestCase):
    def test_unentitled_blocks_analyze(self) -> None:
        ent = MockEntitlementGuard(feature_code=AI_REPAIR_FEATURE,
                                   entitled_result=False)
        o = _orch(entitlement=ent)
        p = _run(o.handle(AssistantEvent.ANALYZE, {"dtc_codes": ["P0301"]}))
        self.assertEqual(p.payload["error_code"], "not_entitled")
        self.assertEqual(ent.consume_calls, [])

    def test_consume_once_on_finish(self) -> None:
        ent = MockEntitlementGuard(feature_code=AI_REPAIR_FEATURE)
        o = _orch(entitlement=ent)
        _run(o.handle(AssistantEvent.ANALYZE,
                      {"vin": _VIN, "dtc_codes": ["P0301"]}))
        _run(o.handle(AssistantEvent.FINISH))
        self.assertEqual(len(ent.consume_calls), 1)
        self.assertEqual(ent.consume_calls[0]["operation_ref"],
                         f"airepair-{_VIN}")

    def test_no_evidence_does_not_consume(self) -> None:
        ent = MockEntitlementGuard(feature_code=AI_REPAIR_FEATURE)
        o = _orch(entitlement=ent)
        _run(o.handle(AssistantEvent.ANALYZE, {}))
        self.assertEqual(ent.consume_calls, [])

    def test_check_happens_after_evidence_validation(self) -> None:
        # An empty case fails fast WITHOUT spending an entitlement check.
        ent = MockEntitlementGuard(feature_code=AI_REPAIR_FEATURE)
        o = _orch(entitlement=ent)
        _run(o.handle(AssistantEvent.ANALYZE, {}))
        self.assertEqual(ent.check_calls, 0)

    def test_snapshot_restore_then_finish(self) -> None:
        ent = MockEntitlementGuard(feature_code=AI_REPAIR_FEATURE)
        o = _orch(entitlement=ent)
        _run(o.handle(AssistantEvent.ANALYZE,
                      {"vin": _VIN, "dtc_codes": ["P0301"]}))
        snap = o.snapshot()
        self.assertEqual(snap["state"], "analyzed")
        o2 = RepairAssistantOrchestrator.restore(
            brain=MockRepairBrain(), snapshot=snap,
            entitlement=MockEntitlementGuard(feature_code=AI_REPAIR_FEATURE))
        self.assertEqual(o2.state, AssistantState.ANALYZED)
        self.assertEqual(o2.data.case.dtc_codes, ("P0301",))
        # Plan is derived — rebuilt deterministically on finish.
        p = _run(o2.handle(AssistantEvent.FINISH))
        self.assertEqual(p.state, AssistantState.DONE)
        self.assertGreaterEqual(p.payload["plan"]["accepted_count"], 1)


if __name__ == "__main__":
    unittest.main()
