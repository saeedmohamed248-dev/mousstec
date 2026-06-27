"""The reasoning core — Generator + Verifier behind one ABC.

Saeed's standing rule: don't build a human-review queue, build a
self-verifying loop. So the "brain" exposes the two halves of that loop
as abstract methods, and the assistant (assistant.py) drives them:

  • generate(case)         → ranked Hypotheses (the Generator).
  • verify(case, hypothesis) → a Critique (the Verifier).

Both are abstract so production can wire a real LLM (grounded on
knowledge.py via a retrieval prompt) while every test runs against the
deterministic `MockRepairBrain`, which reproduces the SAME grounded
behaviour with pure Python — no network, no tokens, no flakiness.

The Generator is constrained to the KB: it only emits hypotheses that
point at a `RepairEntry`. The Verifier re-derives the evidence
independently (it re-reads the case's DTCs + live-data anomalies against
the entry it's handed) so a weak or contradicted hypothesis is caught
before it ever reaches the technician.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field

from . import knowledge
from .knowledge import RepairEntry


# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RepairCase:
    """Everything known about the car at the moment of the question."""
    dtc_codes: tuple[str, ...] = ()       # confirmed/pending codes from scan
    symptoms: str = ""                    # free-text technician note (AR/EN)
    anomaly_pids: tuple[str, ...] = ()    # live PIDs currently OUT of range
    normal_pids: tuple[str, ...] = ()     # live PIDs measured and IN range
    vin: str = ""

    @property
    def has_evidence(self) -> bool:
        return bool(self.dtc_codes or self.symptoms.strip())

    def to_dict(self) -> dict:
        return {
            "dtc_codes": list(self.dtc_codes),
            "symptoms": self.symptoms,
            "anomaly_pids": list(self.anomaly_pids),
            "normal_pids": list(self.normal_pids),
            "vin": self.vin,
        }


@dataclass
class Hypothesis:
    """One candidate cause, grounded on a KB entry, from the Generator."""
    entry_key: str
    cause_ar: str
    cause_en: str
    fix_ar: str
    fix_en: str
    parts: tuple[str, ...]
    matched_dtcs: tuple[str, ...]
    matched_symptoms: tuple[str, ...]
    generator_confidence: float          # 0..1 prior from the Generator
    needs_safety_note: bool = False

    def to_dict(self) -> dict:
        return {
            "entry_key": self.entry_key,
            "cause_ar": self.cause_ar,
            "cause_en": self.cause_en,
            "fix_ar": self.fix_ar,
            "fix_en": self.fix_en,
            "parts": list(self.parts),
            "matched_dtcs": list(self.matched_dtcs),
            "matched_symptoms": list(self.matched_symptoms),
            "generator_confidence": round(self.generator_confidence, 3),
            "needs_safety_note": self.needs_safety_note,
        }


@dataclass
class Critique:
    """The Verifier's independent assessment of a hypothesis."""
    grounded: bool                       # backed by a real KB entry?
    evidence_score: float                # 0..1, 0.5 = neutral
    contradicted: bool                   # live data actively refutes it?
    notes_ar: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "grounded": self.grounded,
            "evidence_score": round(self.evidence_score, 3),
            "contradicted": self.contradicted,
            "notes_ar": list(self.notes_ar),
        }


# ─────────────────────────────────────────────────────────────────────
class AbstractRepairBrain(abc.ABC):
    @abc.abstractmethod
    def generate(self, case: RepairCase) -> list[Hypothesis]:
        """Propose ranked, KB-grounded hypotheses for the case."""

    @abc.abstractmethod
    def verify(self, case: RepairCase, hypothesis: Hypothesis) -> Critique:
        """Independently critique one hypothesis against the case."""


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ─────────────────────────────────────────────────────────────────────
@dataclass
class MockRepairBrain(AbstractRepairBrain):
    """Deterministic stand-in for the real LLM.

    Generator: gathers every KB entry triggered by the case's DTCs or
    symptoms, scores each by how many independent signals point at it.

    Verifier: re-reads the entry's `confirm_pids` against the case's live
    data — a corroborating anomaly raises the score, an in-range reading
    that the cause should have disturbed lowers it (and, if it kills the
    score entirely, marks the hypothesis contradicted).

    `confidence_boost` lets a test nudge the Generator priors without
    editing the KB; it's added to every base prior before clamping.
    """
    confidence_boost: float = 0.0
    generate_calls: int = 0
    verify_calls: int = 0

    # ── Generator ──────────────────────────────────────────────────────
    def generate(self, case: RepairCase) -> list[Hypothesis]:
        self.generate_calls += 1
        candidates: dict[str, RepairEntry] = {}
        for code in case.dtc_codes:
            for e in knowledge.entries_for_dtc(code):
                candidates[e.key] = e
        for e in knowledge.entries_for_symptom(case.symptoms):
            candidates[e.key] = e

        hyps: list[Hypothesis] = []
        sym = case.symptoms.strip().lower()
        for e in candidates.values():
            matched_dtcs = tuple(
                c.strip().upper() for c in case.dtc_codes
                if c.strip().upper() in e.trigger_dtcs)
            matched_syms = tuple(
                kw for kw in e.trigger_symptoms if kw and kw in sym)
            signals = len(matched_dtcs) + (1 if matched_syms else 0)
            # Each extra independent signal nudges the prior up.
            conf = _clamp(
                e.base_confidence + 0.10 * max(0, signals - 1)
                + self.confidence_boost, 0.0, 0.95)
            hyps.append(Hypothesis(
                entry_key=e.key,
                cause_ar=e.cause_ar, cause_en=e.cause_en,
                fix_ar=e.fix_ar, fix_en=e.fix_en, parts=e.parts,
                matched_dtcs=matched_dtcs, matched_symptoms=matched_syms,
                generator_confidence=conf,
                needs_safety_note=e.needs_safety_note,
            ))
        hyps.sort(key=lambda h: h.generator_confidence, reverse=True)
        return hyps

    # ── Verifier ───────────────────────────────────────────────────────
    def verify(self, case: RepairCase, hypothesis: Hypothesis) -> Critique:
        self.verify_calls += 1
        entry = knowledge.get_entry(hypothesis.entry_key)
        if entry is None:
            return Critique(grounded=False, evidence_score=0.0,
                            contradicted=True,
                            notes_ar=("الفرضية مش مرتبطة بأي مرجع معروف.",))

        notes: list[str] = []
        score = 0.5                       # neutral prior
        corroborated = [p for p in entry.confirm_pids
                        if p in case.anomaly_pids]
        refuted = [p for p in entry.confirm_pids
                   if p in case.normal_pids]
        for p in corroborated:
            score += 0.25
            notes.append(f"قراءة {p} خارج المدى — بتأكد السبب.")
        for p in refuted:
            score -= 0.25
            notes.append(f"قراءة {p} طبيعية — بتضعّف السبب ده.")
        score = _clamp(score)

        # No DTC AND no symptom backing → the Generator over-reached.
        if not hypothesis.matched_dtcs and not hypothesis.matched_symptoms:
            return Critique(grounded=True, evidence_score=score,
                            contradicted=True,
                            notes_ar=tuple(notes) + (
                                "مفيش كود ولا عرض مرتبط — محتاج تأكيد يدوي.",))

        contradicted = bool(refuted) and not corroborated and score <= 0.25
        return Critique(grounded=True, evidence_score=score,
                        contradicted=contradicted, notes_ar=tuple(notes))
