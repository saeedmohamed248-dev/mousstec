"""Bidirectional (actuator) test orchestrator.

Interactive, three-beat flow — the technician is in the loop:

  IDLE ─START─▶ ARMED ─ACTIVATE─▶ ACTIVE ─CONFIRM─▶ DONE
                  │                  │
                  │   (ABORT / error always returns control first)
                  └────────┬─────────┘
                           ▼
                         FAILED

  • START    — load the test, entitlement gate ('bidirectional_tests'),
               SafetyGate probe. → ARMED.
  • ACTIVATE — extended session, optional security, then SEIZE + drive
               the output (io_activate). → ACTIVE. The prompt asks the
               technician the test's observe-question.
  • CONFIRM  — RETURN control to the ECU (always), record the
               technician's working=yes/no answer. → DONE. The grant is
               consumed because the diagnostic test WAS performed
               (pass or fail is itself the useful result).

SAFETY INVARIANT: whenever the machine is in ACTIVE and anything ends the
session (CONFIRM, ABORT, or an unexpected error), control is handed back
to the ECU first so no output is left forced on.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..services.entitlement_guard import AbstractEntitlementGuard

from ..premium.safety_checks import AbstractSafetyGate, SafetyReport
from .actuator_catalog import ACTUATOR_CATALOG, ActuatorTest, get_actuator
from .actuator_provider import (
    AbstractActuatorProvider,
    ActuatorControlRejected,
    ActuatorSecurityDenied,
    ActuatorTransportError,
)

log = logging.getLogger(__name__)


class ActuatorState(str, enum.Enum):
    IDLE   = "idle"
    ARMED  = "armed"
    ACTIVE = "active"
    DONE   = "done"
    FAILED = "failed"


_ALLOWED: dict[ActuatorState, set[ActuatorState]] = {
    ActuatorState.IDLE:   {ActuatorState.ARMED, ActuatorState.FAILED},
    ActuatorState.ARMED:  {ActuatorState.ACTIVE, ActuatorState.FAILED},
    ActuatorState.ACTIVE: {ActuatorState.DONE, ActuatorState.FAILED},
    ActuatorState.DONE:   set(),
    ActuatorState.FAILED: set(),
}


class IllegalActuatorTransition(Exception):
    pass


class ActuatorEvent(str, enum.Enum):
    START    = "start"
    ACTIVATE = "activate"
    CONFIRM  = "confirm"
    ABORT    = "abort"


_PROGRESS = {
    ActuatorState.IDLE: 0, ActuatorState.ARMED: 30,
    ActuatorState.ACTIVE: 70, ActuatorState.DONE: 100, ActuatorState.FAILED: 0,
}


@dataclass
class ActuatorData:
    actuator_code: str = ""
    vin: str = ""
    technician_id: str = ""
    safety_voltage_v: float = 0.0
    working: Optional[bool] = None     # technician's verdict
    feedback: dict = field(default_factory=dict)
    control_returned: bool = False
    error_code: str = ""
    error_detail: str = ""


@dataclass
class ActuatorPrompt:
    state: ActuatorState
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


class ActuatorTestOrchestrator:
    def __init__(self, *, safety: AbstractSafetyGate,
                 provider: AbstractActuatorProvider,
                 data: Optional[ActuatorData] = None,
                 state: ActuatorState = ActuatorState.IDLE,
                 entitlement: Optional["AbstractEntitlementGuard"] = None,
                 ) -> None:
        self.safety = safety
        self.provider = provider
        self.data = data or ActuatorData()
        self.state = state
        self.entitlement = entitlement
        self._test: Optional[ActuatorTest] = (
            get_actuator(self.data.actuator_code)
            if self.data.actuator_code else None)

    @property
    def test(self) -> Optional[ActuatorTest]:
        return self._test

    # ── helpers ────────────────────────────────────────────────────────
    def _advance(self, to: ActuatorState) -> None:
        if to not in _ALLOWED[self.state]:
            raise IllegalActuatorTransition(
                f"{self.state.value} → {to.value} not allowed",
            )
        log.info("actuator transition", extra={
            "from": self.state.value, "to": to.value,
            "actuator": self.data.actuator_code, "vin": self.data.vin,
        })
        self.state = to

    async def _safe_return_control(self) -> None:
        """Best-effort: hand the output back to the ECU. Used on the
        normal CONFIRM path AND whenever a session ends mid-ACTIVE."""
        if self.data.control_returned or self._test is None:
            return
        try:
            await self.provider.io_return(self._test.io_did)
        except Exception:               # pragma: no cover
            log.exception("io_return failed during cleanup")
        finally:
            self.data.control_returned = True

    def _fail(self, code: str, detail: str) -> ActuatorPrompt:
        self.data.error_code = code
        self.data.error_detail = detail
        self.state = ActuatorState.FAILED
        log.warning("actuator failure", extra={"code": code, "detail": detail})
        return ActuatorPrompt(
            state=ActuatorState.FAILED,
            title="فشل اختبار المُشغّل",
            body=detail,
            expects="ابعت ABORT لإغلاق الجلسة.",
            progress_pct=0,
            payload={"error_code": code,
                     "control_returned": self.data.control_returned},
            is_terminal=True, is_error=True,
        )

    # ── dispatch ───────────────────────────────────────────────────────
    async def handle(self, event: ActuatorEvent | str,
                     payload: Optional[dict] = None) -> ActuatorPrompt:
        if isinstance(event, str):
            try:
                event = ActuatorEvent(event)
            except ValueError:
                return self._fail("unknown_event", f"Unknown event {event!r}")
        payload = payload or {}
        try:
            return await self._dispatch(event, payload)
        except (IllegalActuatorTransition, ActuatorTransportError,
                ActuatorSecurityDenied, ActuatorControlRejected) as e:
            # If we were driving an output, give control back before failing.
            if self.state == ActuatorState.ACTIVE:
                await self._safe_return_control()
            code = {
                IllegalActuatorTransition: "illegal_transition",
                ActuatorTransportError: "transport_error",
                ActuatorSecurityDenied: "security_denied",
                ActuatorControlRejected: "control_rejected",
            }[type(e)]
            return self._fail(code, str(e))
        except Exception as e:                       # pragma: no cover
            if self.state == ActuatorState.ACTIVE:
                await self._safe_return_control()
            log.exception("actuator unexpected")
            return self._fail("unexpected", repr(e))

    async def _dispatch(self, event: ActuatorEvent,
                        payload: dict) -> ActuatorPrompt:
        if event == ActuatorEvent.ABORT:
            if self.state == ActuatorState.ACTIVE:
                await self._safe_return_control()
            return self._fail("aborted_by_user", "Aborted by technician.")
        if event == ActuatorEvent.START:
            return await self._start(payload)
        if event == ActuatorEvent.ACTIVATE:
            return await self._activate()
        if event == ActuatorEvent.CONFIRM:
            return await self._confirm(payload)
        return self._fail("unhandled_event", f"event {event!r} not handled")

    # ── 1. START ───────────────────────────────────────────────────────
    async def _start(self, payload: dict) -> ActuatorPrompt:
        if self.state != ActuatorState.IDLE:
            raise IllegalActuatorTransition(
                f"START only valid in IDLE (now {self.state.value})",
            )
        code = (payload.get("actuator_code") or "").strip()
        test = get_actuator(code)
        if test is None:
            return self._fail(
                "unknown_actuator",
                f"مفيش اختبار بالكود {code!r}. المتاح: "
                + ", ".join(sorted(ACTUATOR_CATALOG)),
            )
        self._test = test
        self.data.actuator_code = code
        self.data.vin = (payload.get("vin") or "").strip().upper()
        self.data.technician_id = (payload.get("technician_id") or "").strip()

        if self.entitlement is not None:
            entitled, reason = self.entitlement.check()
            if not entitled:
                return self._fail("not_entitled", reason)

        report: SafetyReport = await self.safety.probe(
            require=test.safety.to_require())
        self.data.safety_voltage_v = report.voltage_v
        if not report.ok:
            return self._fail(
                "prereq_failed",
                "الشروط مش مظبوطة: " + " | ".join(report.refusal_reasons),
            )

        self._advance(ActuatorState.ARMED)
        return ActuatorPrompt(
            state=self.state,
            title=f"{test.name_ar} — جاهز ✅",
            body=(f"الجهد: {report.voltage_v:.2f} V. اضغط ACTIVATE عشان "
                  f"نشغّل «{test.name_ar}» لمدة {test.default_duration_s} ثانية "
                  f"وانت بتتفرج عليه."),
            expects="ACTIVATE",
            progress_pct=_PROGRESS[self.state],
            payload={"actuator": test.to_dict(), "voltage_v": report.voltage_v},
        )

    # ── 2. ACTIVATE ────────────────────────────────────────────────────
    async def _activate(self) -> ActuatorPrompt:
        if self.state != ActuatorState.ARMED:
            raise IllegalActuatorTransition(
                f"ACTIVATE only valid in ARMED (now {self.state.value})",
            )
        test = self._test
        assert test is not None

        await self.provider.enter_extended_session()
        if test.needs_security:
            await self.provider.unlock_security(vin=self.data.vin)

        fb = await self.provider.io_activate(
            test.io_did, kind=test.control_kind.value,
            duration_s=test.default_duration_s)
        self.data.feedback = dict(fb.detail)
        self.data.control_returned = False
        self._advance(ActuatorState.ACTIVE)
        return ActuatorPrompt(
            state=self.state,
            title="المُشغّل شغّال — اتفرّج 👀",
            body=test.observe_question_ar,
            expects="CONFIRM (working=true/false)",
            progress_pct=_PROGRESS[self.state],
            payload={"feedback": self.data.feedback,
                     "observe_question_ar": test.observe_question_ar},
        )

    # ── 3. CONFIRM ─────────────────────────────────────────────────────
    async def _confirm(self, payload: dict) -> ActuatorPrompt:
        if self.state != ActuatorState.ACTIVE:
            raise IllegalActuatorTransition(
                f"CONFIRM only valid in ACTIVE (now {self.state.value})",
            )
        # Always return control to the ECU first.
        await self._safe_return_control()

        working = bool(payload.get("working", False))
        self.data.working = working
        self._advance(ActuatorState.DONE)
        test = self._test
        assert test is not None

        # The diagnostic test WAS performed → consume regardless of verdict.
        if self.entitlement is not None:
            op_ref = f"{test.code}-{self.data.vin or 'no-vin'}"
            self.entitlement.consume(vin=self.data.vin, operation_ref=op_ref)

        if working:
            body = (f"تمام — «{test.name_ar}» شغّال كويس. التحكم رجع للـ ECU "
                    f"والنتيجة اتسجّلت: سليم ✅.")
        else:
            body = (f"«{test.name_ar}» مردّش/مشتغلش صح. النتيجة اتسجّلت: "
                    f"يحتاج فحص 🔧. ده بيأكد إن العطل في المُشغّل نفسه أو "
                    f"دائرته مش في حاجة تانية.")
        return ActuatorPrompt(
            state=self.state,
            title="انتهى الاختبار",
            body=body,
            expects="",
            progress_pct=100,
            payload={
                "actuator_code": test.code,
                "vin": self.data.vin,
                "working": working,
                "control_returned": self.data.control_returned,
            },
            is_terminal=True,
        )

    # ── snapshot / restore ─────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "data": {
                "actuator_code": self.data.actuator_code,
                "vin": self.data.vin,
                "technician_id": self.data.technician_id,
                "safety_voltage_v": self.data.safety_voltage_v,
                "working": self.data.working,
                "feedback": dict(self.data.feedback),
                "control_returned": self.data.control_returned,
                "error_code": self.data.error_code,
                "error_detail": self.data.error_detail,
            },
        }

    @classmethod
    def restore(cls, *, safety: AbstractSafetyGate,
                provider: AbstractActuatorProvider,
                snapshot: dict[str, Any],
                entitlement: Optional["AbstractEntitlementGuard"] = None,
                ) -> "ActuatorTestOrchestrator":
        s = snapshot["data"]
        data = ActuatorData(
            actuator_code=s.get("actuator_code", ""),
            vin=s.get("vin", ""),
            technician_id=s.get("technician_id", ""),
            safety_voltage_v=float(s.get("safety_voltage_v") or 0.0),
            working=s.get("working"),
            feedback=dict(s.get("feedback") or {}),
            control_returned=bool(s.get("control_returned")),
            error_code=s.get("error_code", ""),
            error_detail=s.get("error_detail", ""),
        )
        return cls(safety=safety, provider=provider, data=data,
                   state=ActuatorState(snapshot["state"]),
                   entitlement=entitlement)
