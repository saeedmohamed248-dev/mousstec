"""Generic service-reset orchestrator.

One forward-only state machine interprets ANY `ServiceProcedure` from the
catalog (oil reset, EPB service, SAS calibration, DPF regen, throttle
adaptation). Chatbot-friendly the same way the premium orchestrators are.

  IDLE ─START─▶ PREREQ_OK ─RUN─▶ COMPLETED ─FINISH─▶ DONE
                   │               │
                   └───────┬───────┘
                           ▼
                         FAILED

  • START   — load the procedure by code, entitlement gate
              (feature 'service_resets'), SafetyGate probe against the
              procedure's SafetyRequirement. Unentitled / unsafe → FAILED.
  • RUN      — extended session, optional security access, then execute
              every ResetStep in order. Any rejected/failed routine →
              FAILED (the grant is NOT consumed — nothing was changed).
  • FINISH   — consume one grant use, emit the procedure's success
              message + collected step results.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..services.entitlement_guard import AbstractEntitlementGuard

from ..premium.safety_checks import AbstractSafetyGate, SafetyReport
from .procedures import (
    PROCEDURE_CATALOG,
    ResetStep,
    ServiceProcedure,
    StepKind,
    get_procedure,
)
from .reset_provider import (
    AbstractResetProvider,
    ResetTransportError,
    RoutineRejected,
    SecurityAccessDenied,
)

log = logging.getLogger(__name__)


class ResetState(str, enum.Enum):
    IDLE      = "idle"
    PREREQ_OK = "prereq_ok"
    COMPLETED = "completed"
    DONE      = "done"
    FAILED    = "failed"


_ALLOWED: dict[ResetState, set[ResetState]] = {
    ResetState.IDLE:      {ResetState.PREREQ_OK, ResetState.FAILED},
    ResetState.PREREQ_OK: {ResetState.COMPLETED, ResetState.FAILED},
    ResetState.COMPLETED: {ResetState.DONE, ResetState.FAILED},
    ResetState.DONE:      set(),
    ResetState.FAILED:    set(),
}


class IllegalResetTransition(Exception):
    pass


class ResetEvent(str, enum.Enum):
    START  = "start"
    RUN    = "run"
    FINISH = "finish"
    ABORT  = "abort"


_PROGRESS = {
    ResetState.IDLE: 0, ResetState.PREREQ_OK: 25,
    ResetState.COMPLETED: 90, ResetState.DONE: 100, ResetState.FAILED: 0,
}


@dataclass
class ResetData:
    procedure_code: str = ""
    vin: str = ""
    technician_id: str = ""
    safety_voltage_v: float = 0.0
    step_results: list[dict] = field(default_factory=list)
    error_code: str = ""
    error_detail: str = ""


@dataclass
class ResetPrompt:
    state: ResetState
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


class ServiceResetOrchestrator:
    def __init__(self, *, safety: AbstractSafetyGate,
                 provider: AbstractResetProvider,
                 data: Optional[ResetData] = None,
                 state: ResetState = ResetState.IDLE,
                 entitlement: Optional["AbstractEntitlementGuard"] = None,
                 ) -> None:
        self.safety = safety
        self.provider = provider
        self.data = data or ResetData()
        self.state = state
        self.entitlement = entitlement
        self._procedure: Optional[ServiceProcedure] = None
        if self.data.procedure_code:
            self._procedure = get_procedure(self.data.procedure_code)

    @property
    def procedure(self) -> Optional[ServiceProcedure]:
        return self._procedure

    # ── helpers ────────────────────────────────────────────────────────
    def _advance(self, to: ResetState) -> None:
        if to not in _ALLOWED[self.state]:
            raise IllegalResetTransition(
                f"{self.state.value} → {to.value} not allowed",
            )
        log.info("reset transition", extra={
            "from": self.state.value, "to": to.value,
            "proc": self.data.procedure_code, "vin": self.data.vin,
        })
        self.state = to

    def _fail(self, code: str, detail: str) -> ResetPrompt:
        self.data.error_code = code
        self.data.error_detail = detail
        self.state = ResetState.FAILED
        log.warning("reset failure", extra={"code": code, "detail": detail})
        return ResetPrompt(
            state=ResetState.FAILED,
            title="فشل تنفيذ الخدمة",
            body=detail,
            expects="ابعت ABORT لإغلاق الجلسة.",
            progress_pct=0,
            payload={"error_code": code},
            is_terminal=True, is_error=True,
        )

    # ── dispatch ───────────────────────────────────────────────────────
    async def handle(self, event: ResetEvent | str,
                     payload: Optional[dict] = None) -> ResetPrompt:
        if isinstance(event, str):
            try:
                event = ResetEvent(event)
            except ValueError:
                return self._fail("unknown_event", f"Unknown event {event!r}")
        payload = payload or {}
        try:
            return await self._dispatch(event, payload)
        except IllegalResetTransition as e:
            return self._fail("illegal_transition", str(e))
        except ResetTransportError as e:
            return self._fail("transport_error", str(e))
        except SecurityAccessDenied as e:
            return self._fail("security_denied", str(e))
        except RoutineRejected as e:
            return self._fail("routine_rejected", str(e))
        except Exception as e:                       # pragma: no cover
            log.exception("reset unexpected")
            return self._fail("unexpected", repr(e))

    async def _dispatch(self, event: ResetEvent, payload: dict) -> ResetPrompt:
        if event == ResetEvent.ABORT:
            return self._fail("aborted_by_user", "Aborted by technician.")
        if event == ResetEvent.START:
            return await self._start(payload)
        if event == ResetEvent.RUN:
            return await self._run()
        if event == ResetEvent.FINISH:
            return self._finish()
        return self._fail("unhandled_event", f"event {event!r} not handled")

    # ── 1. START ───────────────────────────────────────────────────────
    async def _start(self, payload: dict) -> ResetPrompt:
        if self.state != ResetState.IDLE:
            raise IllegalResetTransition(
                f"START only valid in IDLE (now {self.state.value})",
            )
        code = (payload.get("procedure_code") or "").strip()
        proc = get_procedure(code)
        if proc is None:
            return self._fail(
                "unknown_procedure",
                f"مفيش خدمة بالكود {code!r}. المتاح: "
                + ", ".join(sorted(PROCEDURE_CATALOG)),
            )
        self._procedure = proc
        self.data.procedure_code = code
        self.data.vin = (payload.get("vin") or "").strip().upper()
        self.data.technician_id = (payload.get("technician_id") or "").strip()

        # Entitlement gate BEFORE any bus chatter.
        if self.entitlement is not None:
            entitled, reason = self.entitlement.check()
            if not entitled:
                return self._fail("not_entitled", reason)

        report: SafetyReport = await self.safety.probe(
            require=proc.safety.to_require())
        self.data.safety_voltage_v = report.voltage_v
        if not report.ok:
            return self._fail(
                "prereq_failed",
                "الشروط مش مظبوطة: " + " | ".join(report.refusal_reasons),
            )

        self._advance(ResetState.PREREQ_OK)
        return ResetPrompt(
            state=self.state,
            title=f"{proc.name_ar} — الشروط سليمة ✅",
            body=(f"{proc.preflight_ar}\n\nالجهد: {report.voltage_v:.2f} V. "
                  f"اضغط RUN لتنفيذ الخدمة ({len(proc.steps)} خطوة)."),
            expects="RUN",
            progress_pct=_PROGRESS[self.state],
            payload={
                "procedure": proc.to_dict(),
                "voltage_v": report.voltage_v,
            },
        )

    # ── 2. RUN ─────────────────────────────────────────────────────────
    async def _run(self) -> ResetPrompt:
        if self.state != ResetState.PREREQ_OK:
            raise IllegalResetTransition(
                f"RUN only valid in PREREQ_OK (now {self.state.value})",
            )
        proc = self._procedure
        assert proc is not None

        await self.provider.enter_extended_session()
        if proc.needs_security_access:
            await self.provider.unlock_security(vin=self.data.vin)

        self.data.step_results = []
        for idx, step in enumerate(proc.steps):
            outcome = await self._execute_step(step)
            self.data.step_results.append({
                "index": idx,
                "label_ar": step.label_ar,
                "kind": step.kind.value,
                "ok": outcome.ok,
                "result": outcome.result,
            })
            if not outcome.ok:
                return self._fail(
                    "step_failed",
                    f"الخطوة «{step.label_ar}» رجعت نتيجة فاشلة. "
                    f"الموديول رفض إتمام العملية — أعد المحاولة أو ABORT.",
                )

        self._advance(ResetState.COMPLETED)
        return ResetPrompt(
            state=self.state,
            title="تم التنفيذ — في انتظار التأكيد",
            body=("كل الخطوات اتنفذت بنجاح. اضغط FINISH لإغلاق الخدمة "
                  "وتسجيلها."),
            expects="FINISH",
            progress_pct=_PROGRESS[self.state],
            payload={"step_results": list(self.data.step_results)},
        )

    async def _execute_step(self, step: ResetStep):
        if step.kind == StepKind.ROUTINE_START:
            return await self.provider.run_routine_start(step.rid)
        if step.kind == StepKind.ROUTINE_RESULT:
            return await self.provider.run_routine_result(step.rid)
        if step.kind == StepKind.READ_DID:
            return await self.provider.read_did(step.did)
        if step.kind == StepKind.WRITE_DID:
            return await self.provider.write_did(step.did, b"")
        # Should never happen — catalog is closed.
        raise RoutineRejected(f"unknown step kind {step.kind!r}")

    # ── 3. FINISH ──────────────────────────────────────────────────────
    def _finish(self) -> ResetPrompt:
        if self.state != ResetState.COMPLETED:
            raise IllegalResetTransition(
                f"FINISH only valid in COMPLETED (now {self.state.value})",
            )
        self._advance(ResetState.DONE)
        proc = self._procedure
        assert proc is not None

        if self.entitlement is not None:
            op_ref = f"{proc.code}-{self.data.vin or 'no-vin'}"
            self.entitlement.consume(vin=self.data.vin, operation_ref=op_ref)

        return ResetPrompt(
            state=self.state,
            title=f"{proc.name_ar} ✅",
            body=proc.success_message_ar,
            expects="",
            progress_pct=100,
            payload={
                "procedure_code": proc.code,
                "vin": self.data.vin,
                "step_results": list(self.data.step_results),
            },
            is_terminal=True,
        )

    # ── snapshot / restore ─────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "data": {
                "procedure_code": self.data.procedure_code,
                "vin": self.data.vin,
                "technician_id": self.data.technician_id,
                "safety_voltage_v": self.data.safety_voltage_v,
                "step_results": list(self.data.step_results),
                "error_code": self.data.error_code,
                "error_detail": self.data.error_detail,
            },
        }

    @classmethod
    def restore(cls, *, safety: AbstractSafetyGate,
                provider: AbstractResetProvider,
                snapshot: dict[str, Any],
                entitlement: Optional["AbstractEntitlementGuard"] = None,
                ) -> "ServiceResetOrchestrator":
        s = snapshot["data"]
        data = ResetData(
            procedure_code=s.get("procedure_code", ""),
            vin=s.get("vin", ""),
            technician_id=s.get("technician_id", ""),
            safety_voltage_v=float(s.get("safety_voltage_v") or 0.0),
            step_results=list(s.get("step_results") or []),
            error_code=s.get("error_code", ""),
            error_detail=s.get("error_detail", ""),
        )
        return cls(safety=safety, provider=provider, data=data,
                   state=ResetState(snapshot["state"]), entitlement=entitlement)
