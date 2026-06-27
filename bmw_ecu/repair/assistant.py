"""AI Repair Assistant — the self-verifying loop, made saleable.

Drives the brain's Generator → Verifier pair, combines each hypothesis's
two independent confidence signals, and lets a single **confidence
threshold** decide what gets surfaced as a confident recommendation
versus what gets flagged for a technician's eyes. That threshold is the
whole point: the workshop doesn't hand-review every AI answer — only the
sub-threshold minority is flagged, the confident majority is auto-accepted.

  IDLE ─ANALYZE─▶ ANALYZED ─FINISH─▶ DONE
                     │
        (ABORT / no-evidence / not-entitled / error)
                     ▼
                   FAILED

  • ANALYZE — entitlement gate ('ai_repair_assistant'), build the
              RepairCase from the payload, run generate()+verify() for
              every hypothesis, rank them. → ANALYZED.
  • FINISH  — consume the grant once (the analysis was delivered). → DONE.

final_confidence = generator_confidence × (0.5 + evidence_score), clamped.
A hypothesis the Verifier marks `contradicted` can never be accepted, no
matter how eager the Generator was.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..services.entitlement_guard import AbstractEntitlementGuard

from .brain import (
    AbstractRepairBrain,
    Critique,
    Hypothesis,
    RepairCase,
)

log = logging.getLogger(__name__)


AI_REPAIR_FEATURE = "ai_repair_assistant"
DEFAULT_THRESHOLD = 0.6


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def combine_confidence(generator_confidence: float,
                       evidence_score: float,
                       *, contradicted: bool) -> float:
    """Fuse the Generator's prior with the Verifier's evidence.

    Neutral evidence (0.5) leaves the prior untouched; corroboration
    lifts it, refutation cuts it. A contradicted hypothesis is floored."""
    if contradicted:
        return 0.0
    return round(_clamp(generator_confidence * (0.5 + evidence_score),
                        0.0, 0.98), 3)


# ─────────────────────────────────────────────────────────────────────
@dataclass
class Recommendation:
    rank: int
    hypothesis: Hypothesis
    critique: Critique
    final_confidence: float
    accepted: bool

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "final_confidence": round(self.final_confidence, 3),
            "accepted": self.accepted,
            "hypothesis": self.hypothesis.to_dict(),
            "critique": self.critique.to_dict(),
        }


@dataclass
class RepairPlan:
    case: RepairCase
    recommendations: tuple[Recommendation, ...]
    threshold: float

    @property
    def accepted(self) -> tuple[Recommendation, ...]:
        return tuple(r for r in self.recommendations if r.accepted)

    @property
    def needs_review(self) -> tuple[Recommendation, ...]:
        return tuple(r for r in self.recommendations if not r.accepted)

    @property
    def top(self) -> Optional[Recommendation]:
        return self.recommendations[0] if self.recommendations else None

    @property
    def has_safety_item(self) -> bool:
        return any(r.hypothesis.needs_safety_note
                   for r in self.recommendations)

    def headline_ar(self) -> str:
        if not self.recommendations:
            return ("مفيش سبب واضح من الأكواد/الأعراض المدخلة — محتاج "
                    "فحص يدوي أو بيانات أكتر.")
        top = self.top
        assert top is not None
        pct = int(round(top.final_confidence * 100))
        if top.accepted:
            head = (f"أرجح سبب ({pct}% ثقة): {top.hypothesis.cause_ar} "
                    f"الحل: {top.hypothesis.fix_ar}")
        else:
            head = (f"أعلى احتمال ({pct}% ثقة) لكنه تحت حد التأكيد — "
                    f"محتاج فحص: {top.hypothesis.cause_ar}")
        if self.has_safety_item:
            head += " ⚠️ فيه بند أمان — راجع الملاحظة قبل أي مسح."
        return head

    def to_dict(self) -> dict:
        return {
            "case": self.case.to_dict(),
            "threshold": self.threshold,
            "recommendations": [r.to_dict() for r in self.recommendations],
            "accepted_count": len(self.accepted),
            "needs_review_count": len(self.needs_review),
            "has_safety_item": self.has_safety_item,
            "headline_ar": self.headline_ar(),
        }


# ─────────────────────────────────────────────────────────────────────
class AssistantState(str, enum.Enum):
    IDLE     = "idle"
    ANALYZED = "analyzed"
    DONE     = "done"
    FAILED   = "failed"


_ALLOWED: dict[AssistantState, set[AssistantState]] = {
    AssistantState.IDLE:     {AssistantState.ANALYZED, AssistantState.FAILED},
    AssistantState.ANALYZED: {AssistantState.DONE, AssistantState.FAILED},
    AssistantState.DONE:     set(),
    AssistantState.FAILED:   set(),
}


class IllegalAssistantTransition(Exception):
    pass


class AssistantEvent(str, enum.Enum):
    ANALYZE = "analyze"
    FINISH  = "finish"
    ABORT   = "abort"


_PROGRESS = {
    AssistantState.IDLE: 0, AssistantState.ANALYZED: 70,
    AssistantState.DONE: 100, AssistantState.FAILED: 0,
}


@dataclass
class AssistantData:
    vin: str = ""
    technician_id: str = ""
    threshold: float = DEFAULT_THRESHOLD
    case: Optional[RepairCase] = None
    plan: Optional[RepairPlan] = None
    error_code: str = ""
    error_detail: str = ""


@dataclass
class AssistantPrompt:
    state: AssistantState
    title: str
    body: str
    expects: str = ""
    progress_pct: int = 0
    payload: dict = field(default_factory=dict)
    is_terminal: bool = False
    is_error: bool = False

    def to_dict(self) -> dict:
        return {
            "state": self.state.value, "title": self.title, "body": self.body,
            "expects": self.expects, "progress_pct": self.progress_pct,
            "payload": dict(self.payload),
            "is_terminal": self.is_terminal, "is_error": self.is_error,
        }


class RepairAssistantOrchestrator:
    def __init__(self, *, brain: AbstractRepairBrain,
                 data: Optional[AssistantData] = None,
                 state: AssistantState = AssistantState.IDLE,
                 entitlement: Optional["AbstractEntitlementGuard"] = None,
                 ) -> None:
        self.brain = brain
        self.data = data or AssistantData()
        self.state = state
        self.entitlement = entitlement

    # ── helpers ────────────────────────────────────────────────────────
    def _advance(self, to: AssistantState) -> None:
        if to not in _ALLOWED[self.state]:
            raise IllegalAssistantTransition(
                f"{self.state.value} → {to.value} not allowed",
            )
        self.state = to

    def _fail(self, code: str, detail: str) -> AssistantPrompt:
        self.data.error_code = code
        self.data.error_detail = detail
        self.state = AssistantState.FAILED
        log.warning("assistant failure", extra={"code": code})
        return AssistantPrompt(
            state=AssistantState.FAILED,
            title="تعذّر تحليل العطل",
            body=detail,
            expects="ابعت ABORT للإغلاق أو ANALYZE من جديد بمدخلات أوضح.",
            progress_pct=0,
            payload={"error_code": code},
            is_terminal=True, is_error=True,
        )

    # ── dispatch ───────────────────────────────────────────────────────
    async def handle(self, event: AssistantEvent | str,
                     payload: Optional[dict] = None) -> AssistantPrompt:
        if isinstance(event, str):
            try:
                event = AssistantEvent(event)
            except ValueError:
                return self._fail("unknown_event", f"Unknown event {event!r}")
        payload = payload or {}
        try:
            return await self._dispatch(event, payload)
        except IllegalAssistantTransition as e:
            return self._fail("illegal_transition", str(e))
        except Exception as e:                       # pragma: no cover
            log.exception("assistant unexpected")
            return self._fail("unexpected", repr(e))

    async def _dispatch(self, event: AssistantEvent,
                        payload: dict) -> AssistantPrompt:
        if event == AssistantEvent.ABORT:
            return self._fail("aborted_by_user", "Aborted by technician.")
        if event == AssistantEvent.ANALYZE:
            return await self._analyze(payload)
        if event == AssistantEvent.FINISH:
            return await self._finish()
        return self._fail("unhandled_event", f"event {event!r} not handled")

    # ── 1. ANALYZE ─────────────────────────────────────────────────────
    async def _analyze(self, payload: dict) -> AssistantPrompt:
        if self.state != AssistantState.IDLE:
            raise IllegalAssistantTransition(
                f"ANALYZE only valid in IDLE (now {self.state.value})",
            )
        case = self._build_case(payload)
        self.data.case = case
        self.data.vin = case.vin
        self.data.technician_id = (
            payload.get("technician_id") or self.data.technician_id or "").strip()
        if "threshold" in payload:
            self.data.threshold = _clamp(float(payload["threshold"]), 0.0, 1.0)

        if not case.has_evidence:
            return self._fail(
                "no_evidence",
                "لازم تدخل كود عطل واحد على الأقل أو وصف للعرض عشان أحلل.",
            )

        if self.entitlement is not None:
            entitled, reason = self.entitlement.check()
            if not entitled:
                return self._fail("not_entitled", reason)

        plan = self._run_loop(case, self.data.threshold)
        self.data.plan = plan
        self._advance(AssistantState.ANALYZED)
        return AssistantPrompt(
            state=self.state,
            title="تحليل مبدئي جاهز 🤖",
            body=plan.headline_ar(),
            expects="FINISH لاعتماد التقرير، أو ABORT.",
            progress_pct=_PROGRESS[self.state],
            payload={"plan": plan.to_dict()},
        )

    def _build_case(self, payload: dict) -> RepairCase:
        codes = tuple(
            str(c).strip().upper()
            for c in (payload.get("dtc_codes") or [])
            if str(c).strip())
        return RepairCase(
            dtc_codes=codes,
            symptoms=str(payload.get("symptoms") or "").strip(),
            anomaly_pids=tuple(payload.get("anomaly_pids") or ()),
            normal_pids=tuple(payload.get("normal_pids") or ()),
            vin=str(payload.get("vin") or "").strip().upper(),
        )

    def _run_loop(self, case: RepairCase, threshold: float) -> RepairPlan:
        """Generator → Verifier → combine → rank → threshold."""
        hypotheses = self.brain.generate(case)
        scored: list[Recommendation] = []
        for h in hypotheses:
            critique = self.brain.verify(case, h)
            final = combine_confidence(
                h.generator_confidence, critique.evidence_score,
                contradicted=critique.contradicted)
            accepted = (critique.grounded and not critique.contradicted
                        and final >= threshold)
            scored.append(Recommendation(
                rank=0, hypothesis=h, critique=critique,
                final_confidence=final, accepted=accepted))
        # Rank by fused confidence, then assign 1-based ranks.
        scored.sort(key=lambda r: r.final_confidence, reverse=True)
        for i, rec in enumerate(scored, start=1):
            rec.rank = i
        return RepairPlan(case=case, recommendations=tuple(scored),
                          threshold=threshold)

    # ── 2. FINISH ──────────────────────────────────────────────────────
    async def _finish(self) -> AssistantPrompt:
        if self.state != AssistantState.ANALYZED:
            raise IllegalAssistantTransition(
                f"FINISH only valid in ANALYZED (now {self.state.value})",
            )
        self._advance(AssistantState.DONE)
        plan = self.data.plan
        if plan is None and self.data.case is not None:
            # Restored mid-flow: the plan is derived, rebuild it deterministically.
            plan = self._run_loop(self.data.case, self.data.threshold)
            self.data.plan = plan
        assert plan is not None
        if self.entitlement is not None:
            op_ref = f"airepair-{self.data.vin or 'no-vin'}"
            self.entitlement.consume(vin=self.data.vin, operation_ref=op_ref)
        return AssistantPrompt(
            state=self.state,
            title="اتعمد تقرير الإصلاح ✅",
            body=(f"اتسجّل {len(plan.accepted)} سبب مؤكد و"
                  f"{len(plan.needs_review)} محتاج فحص يدوي. ابدأ بأعلى ثقة "
                  f"ونزّل بالترتيب."),
            expects="",
            progress_pct=100,
            payload={"plan": plan.to_dict()},
            is_terminal=True,
        )

    # ── snapshot / restore ─────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "data": {
                "vin": self.data.vin,
                "technician_id": self.data.technician_id,
                "threshold": self.data.threshold,
                "case": self.data.case.to_dict() if self.data.case else None,
                "plan": self.data.plan.to_dict() if self.data.plan else None,
                "error_code": self.data.error_code,
                "error_detail": self.data.error_detail,
            },
        }

    @classmethod
    def restore(cls, *, brain: AbstractRepairBrain,
                snapshot: dict[str, Any],
                entitlement: Optional["AbstractEntitlementGuard"] = None,
                ) -> "RepairAssistantOrchestrator":
        s = snapshot["data"]
        case = None
        c = s.get("case")
        if c:
            case = RepairCase(
                dtc_codes=tuple(c.get("dtc_codes") or ()),
                symptoms=c.get("symptoms", ""),
                anomaly_pids=tuple(c.get("anomaly_pids") or ()),
                normal_pids=tuple(c.get("normal_pids") or ()),
                vin=c.get("vin", ""),
            )
        # The plan is a derived artefact — it is NOT rehydrated (a fresh
        # ANALYZE rebuilds it deterministically). Restoring mid-flow keeps
        # the case + state so the UI can re-render or re-finish.
        data = AssistantData(
            vin=s.get("vin", ""),
            technician_id=s.get("technician_id", ""),
            threshold=float(s.get("threshold") or DEFAULT_THRESHOLD),
            case=case,
            error_code=s.get("error_code", ""),
            error_detail=s.get("error_detail", ""),
        )
        return cls(brain=brain, data=data,
                   state=AssistantState(snapshot["state"]),
                   entitlement=entitlement)
