"""EGS ISN Reset — ZF 8HP automatic transmission.

When a workshop swaps in a USED 8HP gearbox (e.g. from a salvage yard),
the EGS (ElectroniSche Getriebe Steuerung — transmission control unit)
holds the previous car's ISN bound to its DME. Without clearing that
binding, the donor gearbox refuses to pair with the recipient's DME —
the dashboard throws "Transmission Malfunction" and the car limp-modes.

This module drives the OBD/Bench flow that clears the ISN binding so
the recipient's DME can re-pair on next ignition cycle.

UDS sequence (modelled, not real-bus driven in tests)
------------------------------------------------------
  1. DiagnosticSessionControl 0x10 0x03 (Extended Diagnostic).
  2. SecurityAccess 0x27 (challenge / response — handled at the
     transport layer; orchestrator just records that it succeeded).
  3. RoutineControl 0x31 0x01 <RID> where RID is the ZF "clear ISN
     binding" routine (manufacturer-specific, kept opaque here).
  4. ReadDataByIdentifier 0x22 to confirm the bound-ISN field is now
     0xFF padding instead of the previous DME's hash.

Production wires a real `EgsServiceProvider` (UDS client + security
access) under the orchestrator's `provider` slot. Tests use
`MockEgsServiceProvider` which records every call so assertions can
verify the orchestrator drove the right sequence.

Pre-conditions (enforced via SafetyGate)
----------------------------------------
  • Gear in P (lever locked — prevents accidental roll-away during
    the bus chatter window).
  • Voltage 12.0–14.8 V (a brown-out mid-routine bricks the EGS).
  • Ignition KOEO (engine NOT running — RPM noise jams 0x27).
  • No active gearbox DTCs that signal physical damage
    (P0700, P0717, P0731 etc. would indicate a broken gearbox, not
    a swap candidate).
"""
from __future__ import annotations

import abc
import enum
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..services.entitlement_guard import AbstractEntitlementGuard

from .safety_checks import (
    AbstractSafetyGate,
    GearPosition,
    IgnitionState,
    SafetyReport,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# State machine
# ─────────────────────────────────────────────────────────────────────
class EgsIsnState(str, enum.Enum):
    IDLE              = "idle"
    PREREQ_OK         = "prereq_ok"
    CURRENT_ISN_READ  = "current_isn_read"
    RESET_REQUESTED   = "reset_requested"
    VERIFIED          = "verified"
    DONE              = "done"
    FAILED            = "failed"


_ALLOWED: dict[EgsIsnState, set[EgsIsnState]] = {
    EgsIsnState.IDLE:             {EgsIsnState.PREREQ_OK, EgsIsnState.FAILED},
    EgsIsnState.PREREQ_OK:        {EgsIsnState.CURRENT_ISN_READ, EgsIsnState.FAILED},
    EgsIsnState.CURRENT_ISN_READ: {EgsIsnState.RESET_REQUESTED, EgsIsnState.FAILED},
    EgsIsnState.RESET_REQUESTED:  {EgsIsnState.VERIFIED, EgsIsnState.FAILED},
    EgsIsnState.VERIFIED:         {EgsIsnState.DONE, EgsIsnState.FAILED},
    EgsIsnState.DONE:             set(),
    EgsIsnState.FAILED:           set(),
}


class IllegalEgsTransition(Exception):
    pass


class EgsResetRefused(Exception):
    """Raised when the gearbox / safety gate refuses the reset."""


# ─────────────────────────────────────────────────────────────────────
# Service provider — wraps the UDS layer for the orchestrator.
# ─────────────────────────────────────────────────────────────────────
class AbstractEgsServiceProvider(abc.ABC):
    """Production wires a UdsClient + SecurityAccess + a real routine
    invocation; tests inject MockEgsServiceProvider for deterministic
    behaviour."""

    @abc.abstractmethod
    async def enter_extended_session(self) -> None: ...

    @abc.abstractmethod
    async def unlock_security(self, *, vin: str) -> None: ...

    @abc.abstractmethod
    async def read_bound_isn(self) -> bytes:
        """Return the 32-byte ISN field. 0xFF padding = unbound."""

    @abc.abstractmethod
    async def request_isn_clear(self) -> None: ...

    @abc.abstractmethod
    async def restart_module(self) -> None: ...


@dataclass
class MockEgsServiceProvider(AbstractEgsServiceProvider):
    """Test double. Tests pre-load the ISN state and the orchestrator
    drives the flow; we record every call so assertions verify the
    sequence."""
    initial_isn: bytes = field(default_factory=lambda: bytes(32))
    refuse_clear: bool = False
    extended_calls: int = 0
    security_calls: list[str] = field(default_factory=list)
    read_calls: int = 0
    clear_calls: int = 0
    restart_calls: int = 0
    cleared: bool = False

    async def enter_extended_session(self) -> None:
        self.extended_calls += 1

    async def unlock_security(self, *, vin: str) -> None:
        self.security_calls.append(vin)

    async def read_bound_isn(self) -> bytes:
        self.read_calls += 1
        if self.cleared:
            return b"\xFF" * 32
        return self.initial_isn

    async def request_isn_clear(self) -> None:
        self.clear_calls += 1
        if self.refuse_clear:
            raise EgsResetRefused(
                "ZF 8HP refused the clear-ISN routine (negative response). "
                "Gearbox may be locked by a previous failed reset.",
            )
        self.cleared = True

    async def restart_module(self) -> None:
        self.restart_calls += 1


# ─────────────────────────────────────────────────────────────────────
@dataclass
class EgsIsnData:
    vin: str = ""
    technician_id: str = ""
    safety_voltage_v: float = 0.0
    bound_isn_hex_before: str = ""
    bound_isn_hex_after: str = ""
    notes: list[str] = field(default_factory=list)
    error_code: str = ""
    error_detail: str = ""


@dataclass
class EgsIsnPrompt:
    state: EgsIsnState
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


class EgsIsnEvent(str, enum.Enum):
    CHECK_PREREQS   = "check_prereqs"
    READ_BOUND_ISN  = "read_bound_isn"
    REQUEST_CLEAR   = "request_clear"
    VERIFY          = "verify"
    FINISH          = "finish"
    ABORT           = "abort"


_PROGRESS = {
    EgsIsnState.IDLE: 0, EgsIsnState.PREREQ_OK: 20,
    EgsIsnState.CURRENT_ISN_READ: 40, EgsIsnState.RESET_REQUESTED: 70,
    EgsIsnState.VERIFIED: 90, EgsIsnState.DONE: 100,
    EgsIsnState.FAILED: 0,
}


# ZF 8HP gearbox damage indicators — block the reset if any are active.
EGS_DAMAGE_DTCS = (
    "P0700", "P0717", "P0731", "P0741", "P0782", "P0796", "P0900",
)


# ─────────────────────────────────────────────────────────────────────
class EgsIsnOrchestrator:
    def __init__(self, *, safety: AbstractSafetyGate,
                 provider: AbstractEgsServiceProvider,
                 data: Optional[EgsIsnData] = None,
                 state: EgsIsnState = EgsIsnState.IDLE,
                 entitlement: Optional["AbstractEntitlementGuard"] = None,
                 ) -> None:
        self.safety = safety
        self.provider = provider
        self.data = data or EgsIsnData()
        self.state = state
        # Entitlement gate (granular SaaS) — check() before advancing
        # past IDLE, consume() on FINISH.
        self.entitlement = entitlement

    def _advance(self, to: EgsIsnState) -> None:
        if to not in _ALLOWED[self.state]:
            raise IllegalEgsTransition(
                f"{self.state.value} → {to.value} not allowed",
            )
        log.info("egs transition", extra={
            "from": self.state.value, "to": to.value, "vin": self.data.vin,
        })
        self.state = to

    def _fail(self, code: str, detail: str) -> EgsIsnPrompt:
        self.data.error_code = code
        self.data.error_detail = detail
        self.state = EgsIsnState.FAILED
        log.warning("egs failure", extra={"code": code, "detail": detail})
        return EgsIsnPrompt(
            state=EgsIsnState.FAILED,
            title="فشل مسح الـ ISN",
            body=detail,
            expects="ابعت ABORT لإغلاق الجلسة.",
            progress_pct=0,
            payload={"error_code": code},
            is_terminal=True, is_error=True,
        )

    async def handle(self, event: EgsIsnEvent | str,
                     payload: Optional[dict] = None) -> EgsIsnPrompt:
        if isinstance(event, str):
            try:
                event = EgsIsnEvent(event)
            except ValueError:
                return self._fail("unknown_event", f"Unknown event {event!r}")
        payload = payload or {}
        try:
            return await self._dispatch(event, payload)
        except IllegalEgsTransition as e:
            return self._fail("illegal_transition", str(e))
        except EgsResetRefused as e:
            return self._fail("reset_refused", str(e))
        except Exception as e:                          # pragma: no cover
            log.exception("egs unexpected")
            return self._fail("unexpected", repr(e))

    async def _dispatch(self, event: EgsIsnEvent, payload: dict
                        ) -> EgsIsnPrompt:
        if event == EgsIsnEvent.ABORT:
            return self._fail("aborted_by_user", "Aborted by technician.")
        if event == EgsIsnEvent.CHECK_PREREQS:
            return await self._check_prereqs(payload)
        if event == EgsIsnEvent.READ_BOUND_ISN:
            return await self._read_bound_isn()
        if event == EgsIsnEvent.REQUEST_CLEAR:
            return await self._request_clear()
        if event == EgsIsnEvent.VERIFY:
            return await self._verify()
        if event == EgsIsnEvent.FINISH:
            return self._finish()
        return self._fail("unhandled_event", f"event {event!r} not handled")

    # ── 1. CHECK_PREREQS ──────────────────────────────────────
    async def _check_prereqs(self, payload: dict) -> EgsIsnPrompt:
        if self.state != EgsIsnState.IDLE:
            raise IllegalEgsTransition(
                f"CHECK_PREREQS only valid in IDLE (now {self.state.value})",
            )
        self.data.vin = (payload.get("vin") or "").strip().upper()
        self.data.technician_id = (payload.get("technician_id") or "").strip()

        # Entitlement gate — block unentitled sessions BEFORE any
        # bus chatter or UDS handshake.
        if self.entitlement is not None:
            entitled, reason = self.entitlement.check()
            if not entitled:
                return self._fail("not_entitled", reason)

        report: SafetyReport = await self.safety.probe(require={
            "voltage_min_v": 12.0, "voltage_max_v": 14.8,
            "gear_in": [GearPosition.P],
            "ignition_in": [IgnitionState.KOEO],
            "forbidden_dtcs": EGS_DAMAGE_DTCS,
        })
        self.data.safety_voltage_v = report.voltage_v
        if not report.ok:
            return self._fail(
                "prereq_failed",
                "الشروط مش مظبوطة: " + " | ".join(report.refusal_reasons),
            )
        self._advance(EgsIsnState.PREREQ_OK)
        return EgsIsnPrompt(
            state=self.state,
            title="الشروط سليمة ✅",
            body=(
                f"الـ EGS جاهز للقراءة. الجهد: {report.voltage_v:.2f} V، "
                f"الـ gear: P، الـ ignition: KOEO. اضغط READ_BOUND_ISN."
            ),
            expects="READ_BOUND_ISN",
            progress_pct=_PROGRESS[self.state],
            payload={"voltage_v": report.voltage_v},
        )

    # ── 2. READ_BOUND_ISN ─────────────────────────────────────
    async def _read_bound_isn(self) -> EgsIsnPrompt:
        if self.state != EgsIsnState.PREREQ_OK:
            raise IllegalEgsTransition(
                f"READ_BOUND_ISN only valid in PREREQ_OK (now {self.state.value})",
            )
        await self.provider.enter_extended_session()
        await self.provider.unlock_security(vin=self.data.vin)
        isn = await self.provider.read_bound_isn()
        if len(isn) != 32:
            return self._fail(
                "bad_isn_length",
                f"ZF returned {len(isn)}-byte ISN, expected 32.",
            )
        self.data.bound_isn_hex_before = isn.hex().upper()
        self._advance(EgsIsnState.CURRENT_ISN_READ)
        already_clear = all(b == 0xFF for b in isn)
        if already_clear:
            body = (
                "الـ EGS فاضي بالفعل (32 byte = 0xFF). الـ gearbox ده مش "
                "متربط بأي DME — مفيش حاجة نمسحها. اضغط REQUEST_CLEAR "
                "لو لسه عاوز تـ force الـ routine، أو ABORT لو ده غير متوقع."
            )
        else:
            body = (
                f"الـ ISN الحالي يبدأ بـ {self.data.bound_isn_hex_before[:8]}…. "
                f"اضغط REQUEST_CLEAR عشان نمسح الربط من على الـ EGS."
            )
        return EgsIsnPrompt(
            state=self.state,
            title="قراءة الـ ISN الحالي",
            body=body,
            expects="REQUEST_CLEAR",
            progress_pct=_PROGRESS[self.state],
            payload={
                "isn_first_octet": self.data.bound_isn_hex_before[:2],
                "already_clear": already_clear,
            },
        )

    # ── 3. REQUEST_CLEAR ──────────────────────────────────────
    async def _request_clear(self) -> EgsIsnPrompt:
        if self.state != EgsIsnState.CURRENT_ISN_READ:
            raise IllegalEgsTransition(
                f"REQUEST_CLEAR only valid in CURRENT_ISN_READ (now {self.state.value})",
            )
        await self.provider.request_isn_clear()
        self._advance(EgsIsnState.RESET_REQUESTED)
        return EgsIsnPrompt(
            state=self.state,
            title="تم إرسال الـ clear routine",
            body=(
                "الـ EGS قبل الـ routine. اضغط VERIFY عشان نقرأ الـ ISN "
                "تاني ونتأكد إنه بقى 0xFF padding."
            ),
            expects="VERIFY",
            progress_pct=_PROGRESS[self.state],
            payload={},
        )

    # ── 4. VERIFY ─────────────────────────────────────────────
    async def _verify(self) -> EgsIsnPrompt:
        if self.state != EgsIsnState.RESET_REQUESTED:
            raise IllegalEgsTransition(
                f"VERIFY only valid in RESET_REQUESTED (now {self.state.value})",
            )
        isn = await self.provider.read_bound_isn()
        self.data.bound_isn_hex_after = isn.hex().upper()
        if not all(b == 0xFF for b in isn):
            return self._fail(
                "verify_mismatch",
                "بعد الـ clear الـ ISN لسه فيه bytes غير 0xFF — الـ routine "
                "ما اشتغلتش. كرّر REQUEST_CLEAR أو اعمل ABORT.",
            )
        await self.provider.restart_module()
        self._advance(EgsIsnState.VERIFIED)
        return EgsIsnPrompt(
            state=self.state,
            title="تم المسح ✅",
            body=(
                "الـ EGS رد بـ 32 byte = 0xFF — الربط القديم اتمسح. "
                "تم عمل restart للـ module. اضغط FINISH."
            ),
            expects="FINISH",
            progress_pct=_PROGRESS[self.state],
            payload={"isn_after_hex": self.data.bound_isn_hex_after},
        )

    # ── 5. FINISH ─────────────────────────────────────────────
    def _finish(self) -> EgsIsnPrompt:
        if self.state != EgsIsnState.VERIFIED:
            raise IllegalEgsTransition(
                f"FINISH only valid in VERIFIED (now {self.state.value})",
            )
        self._advance(EgsIsnState.DONE)

        # Entitlement consume — EGS ISN cleared + verified successfully.
        if self.entitlement is not None:
            op_ref = f"egs-{self.data.vin or 'no-vin'}"
            self.entitlement.consume(vin=self.data.vin, operation_ref=op_ref)

        return EgsIsnPrompt(
            state=self.state,
            title="انتهت العملية 🎉",
            body=(
                "اقفل الـ ignition ثم شغّل العربية. الـ DME الجديد هيـ "
                "pair مع الـ gearbox تلقائياً في أول دورة. الـ session "
                "محفوظة في الـ Cloud Sync."
            ),
            expects="",
            progress_pct=100,
            payload={
                "vin": self.data.vin,
                "isn_before": self.data.bound_isn_hex_before,
                "isn_after": self.data.bound_isn_hex_after,
            },
            is_terminal=True,
        )

    # ── Snapshot / restore ─────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "data": {
                "vin": self.data.vin,
                "technician_id": self.data.technician_id,
                "safety_voltage_v": self.data.safety_voltage_v,
                "bound_isn_hex_before": self.data.bound_isn_hex_before,
                "bound_isn_hex_after": self.data.bound_isn_hex_after,
                "notes": list(self.data.notes),
                "error_code": self.data.error_code,
                "error_detail": self.data.error_detail,
            },
        }

    @classmethod
    def restore(cls, *, safety: AbstractSafetyGate,
                provider: AbstractEgsServiceProvider,
                snapshot: dict[str, Any]) -> "EgsIsnOrchestrator":
        s = snapshot["data"]
        data = EgsIsnData(
            vin=s.get("vin", ""),
            technician_id=s.get("technician_id", ""),
            safety_voltage_v=float(s.get("safety_voltage_v") or 0.0),
            bound_isn_hex_before=s.get("bound_isn_hex_before", ""),
            bound_isn_hex_after=s.get("bound_isn_hex_after", ""),
            notes=list(s.get("notes") or []),
            error_code=s.get("error_code", ""),
            error_detail=s.get("error_detail", ""),
        )
        return cls(safety=safety, provider=provider,
                   data=data, state=EgsIsnState(snapshot["state"]))
