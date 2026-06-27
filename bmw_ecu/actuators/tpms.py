"""TPMS (RDC) read + relearn — tyre-pressure sensor service.

BMW's RDC module learns one wireless sensor ID per wheel position. After a
tyre rotation or a sensor swap the IDs no longer match their positions, so
the technician runs a *relearn*: read what each sensor currently reports
(pressure / temperature / battery / its 4-byte ID), then teach the RDC the
position→ID map again.

This is the SAME orchestrator shape as the rest of bmw_ecu — a forward-only
state machine over an abstract transport, hardware-free in tests, gated by
the saleable feature 'tpms_service':

  IDLE ─READ─▶ READ_DONE ─RELEARN─▶ RELEARNED ─FINISH─▶ DONE
                                                            │
                       (ABORT / error / bad-precondition)   ▼
                                                          FAILED

  • READ    — entitlement gate + SafetyGate probe, then read all four
              sensors. → READ_DONE (shows the technician each wheel).
  • RELEARN — extended session, optional security, run the RDC relearn
              routine binding each position to the ID just read.
  • FINISH  — consume the grant once. → DONE.
"""
from __future__ import annotations

import abc
import enum
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..services.entitlement_guard import AbstractEntitlementGuard

from ..premium.safety_checks import (
    AbstractSafetyGate,
    IgnitionState,
    SafetyReport,
    SafetyRequirement,
)

log = logging.getLogger(__name__)


TPMS_FEATURE = "tpms_service"
TPMS_MODULE = "rdc"            # Reifendruckkontrolle (see scan.module_map)

# Wheel positions in the canonical read order.
WHEEL_POSITIONS: tuple[str, ...] = ("FL", "FR", "RL", "RR")

# A relearn just talks to the RDC over the bus — engine off, P, normal rail.
_TPMS_SAFETY = SafetyRequirement(
    voltage_min_v=12.0,
    ignition_in=(IgnitionState.KOEO,),
)

# Passenger-car cold-pressure sanity band (bar). Outside this we flag the
# wheel so the technician inflates/deflates before trusting the relearn.
RECOMMENDED_MIN_BAR = 2.0
RECOMMENDED_MAX_BAR = 3.2


# ─────────────────────────────────────────────────────────────────────
class TpmsTransportError(Exception):
    """Bus-level failure — the RDC module isn't answering."""


class TpmsSecurityDenied(Exception):
    """RDC refused security access before the relearn routine."""


class TpmsRelearnRejected(Exception):
    """The RDC refused to bind a sensor (e.g. ID not seen on the antenna)."""


@dataclass(frozen=True)
class TpmsSensor:
    position: str
    sensor_id: str
    pressure_bar: float
    temp_c: float
    battery_ok: bool

    @property
    def pressure_ok(self) -> bool:
        return RECOMMENDED_MIN_BAR <= self.pressure_bar <= RECOMMENDED_MAX_BAR

    @property
    def healthy(self) -> bool:
        return self.battery_ok and self.pressure_ok and bool(self.sensor_id)

    def to_dict(self) -> dict:
        return {
            "position": self.position,
            "sensor_id": self.sensor_id,
            "pressure_bar": round(self.pressure_bar, 2),
            "temp_c": round(self.temp_c, 1),
            "battery_ok": self.battery_ok,
            "pressure_ok": self.pressure_ok,
            "healthy": self.healthy,
        }


@dataclass(frozen=True)
class TpmsReadResult:
    sensors: tuple[TpmsSensor, ...]

    @property
    def all_healthy(self) -> bool:
        return bool(self.sensors) and all(s.healthy for s in self.sensors)

    @property
    def weak_batteries(self) -> tuple[str, ...]:
        return tuple(s.position for s in self.sensors if not s.battery_ok)

    @property
    def out_of_range(self) -> tuple[str, ...]:
        return tuple(s.position for s in self.sensors if not s.pressure_ok)

    def to_dict(self) -> dict:
        return {
            "sensors": [s.to_dict() for s in self.sensors],
            "all_healthy": self.all_healthy,
            "weak_batteries": list(self.weak_batteries),
            "out_of_range": list(self.out_of_range),
        }


# ─────────────────────────────────────────────────────────────────────
class AbstractTpmsProvider(abc.ABC):
    @abc.abstractmethod
    async def enter_extended_session(self) -> None: ...

    @abc.abstractmethod
    async def unlock_security(self, *, vin: str) -> None: ...

    @abc.abstractmethod
    async def read_sensors(self) -> tuple[TpmsSensor, ...]:
        """Read every wheel sensor the RDC currently hears."""

    @abc.abstractmethod
    async def relearn(self, *, bindings: dict[str, str]) -> bool:
        """Teach the RDC the position→sensor_id map. Raise
        TpmsRelearnRejected if a binding can't be written."""


@dataclass
class MockTpmsProvider(AbstractTpmsProvider):
    """Deterministic test double.

    Config:
      • sensors          — the readings read_sensors() returns.
      • bus_down         — enter_extended_session raises.
      • deny_security    — unlock_security raises.
      • reject_relearn   — relearn() raises TpmsRelearnRejected.

    Records:
      • session_calls / security_calls / read_calls / relearn_calls.
    """
    sensors: tuple[TpmsSensor, ...] = ()
    bus_down: bool = False
    deny_security: bool = False
    reject_relearn: bool = False

    session_calls: int = 0
    security_calls: list[str] = field(default_factory=list)
    read_calls: int = 0
    relearn_calls: list[dict] = field(default_factory=list)

    async def enter_extended_session(self) -> None:
        self.session_calls += 1
        if self.bus_down:
            raise TpmsTransportError(
                "موديول الـ RDC مش بيرد — اتأكد من الكونتاكت والباص."
            )

    async def unlock_security(self, *, vin: str) -> None:
        self.security_calls.append(vin)
        if self.deny_security:
            raise TpmsSecurityDenied(
                "الـ RDC رفض security access قبل عملية الـ relearn."
            )

    async def read_sensors(self) -> tuple[TpmsSensor, ...]:
        self.read_calls += 1
        return tuple(self.sensors)

    async def relearn(self, *, bindings: dict[str, str]) -> bool:
        self.relearn_calls.append(dict(bindings))
        if self.reject_relearn:
            raise TpmsRelearnRejected(
                "الـ RDC مش لاقي إشارة من أحد الحساسات — لِف العربية شوية "
                "وجرّب تاني."
            )
        return True


# ─────────────────────────────────────────────────────────────────────
class TpmsState(str, enum.Enum):
    IDLE      = "idle"
    READ_DONE = "read_done"
    RELEARNED = "relearned"
    DONE      = "done"
    FAILED    = "failed"


_ALLOWED: dict[TpmsState, set[TpmsState]] = {
    TpmsState.IDLE:      {TpmsState.READ_DONE, TpmsState.FAILED},
    TpmsState.READ_DONE: {TpmsState.RELEARNED, TpmsState.FAILED},
    TpmsState.RELEARNED: {TpmsState.DONE, TpmsState.FAILED},
    TpmsState.DONE:      set(),
    TpmsState.FAILED:    set(),
}


class IllegalTpmsTransition(Exception):
    pass


class TpmsEvent(str, enum.Enum):
    READ    = "read"
    RELEARN = "relearn"
    FINISH  = "finish"
    ABORT   = "abort"


_PROGRESS = {
    TpmsState.IDLE: 0, TpmsState.READ_DONE: 40,
    TpmsState.RELEARNED: 80, TpmsState.DONE: 100, TpmsState.FAILED: 0,
}


@dataclass
class TpmsData:
    vin: str = ""
    technician_id: str = ""
    needs_security: bool = False
    safety_voltage_v: float = 0.0
    sensors: tuple[TpmsSensor, ...] = ()
    relearned: bool = False
    error_code: str = ""
    error_detail: str = ""


@dataclass
class TpmsPrompt:
    state: TpmsState
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


class TpmsRelearnOrchestrator:
    """One forward-only machine: read sensors → relearn → finish."""

    def __init__(self, *, safety: AbstractSafetyGate,
                 provider: AbstractTpmsProvider,
                 data: Optional[TpmsData] = None,
                 state: TpmsState = TpmsState.IDLE,
                 needs_security: bool = False,
                 entitlement: Optional["AbstractEntitlementGuard"] = None,
                 ) -> None:
        self.safety = safety
        self.provider = provider
        self.data = data or TpmsData(needs_security=needs_security)
        self.state = state
        self.entitlement = entitlement

    # ── helpers ────────────────────────────────────────────────────────
    def _advance(self, to: TpmsState) -> None:
        if to not in _ALLOWED[self.state]:
            raise IllegalTpmsTransition(
                f"{self.state.value} → {to.value} not allowed",
            )
        log.info("tpms transition", extra={
            "from": self.state.value, "to": to.value, "vin": self.data.vin,
        })
        self.state = to

    def _fail(self, code: str, detail: str) -> TpmsPrompt:
        self.data.error_code = code
        self.data.error_detail = detail
        self.state = TpmsState.FAILED
        log.warning("tpms failure", extra={"code": code, "detail": detail})
        return TpmsPrompt(
            state=TpmsState.FAILED,
            title="فشل خدمة حساسات الإطارات (TPMS)",
            body=detail,
            expects="ابعت ABORT لإغلاق الجلسة.",
            progress_pct=0,
            payload={"error_code": code},
            is_terminal=True, is_error=True,
        )

    # ── dispatch ───────────────────────────────────────────────────────
    async def handle(self, event: TpmsEvent | str,
                     payload: Optional[dict] = None) -> TpmsPrompt:
        if isinstance(event, str):
            try:
                event = TpmsEvent(event)
            except ValueError:
                return self._fail("unknown_event", f"Unknown event {event!r}")
        payload = payload or {}
        try:
            return await self._dispatch(event, payload)
        except (IllegalTpmsTransition, TpmsTransportError,
                TpmsSecurityDenied, TpmsRelearnRejected) as e:
            code = {
                IllegalTpmsTransition: "illegal_transition",
                TpmsTransportError: "transport_error",
                TpmsSecurityDenied: "security_denied",
                TpmsRelearnRejected: "relearn_rejected",
            }[type(e)]
            return self._fail(code, str(e))
        except Exception as e:                       # pragma: no cover
            log.exception("tpms unexpected")
            return self._fail("unexpected", repr(e))

    async def _dispatch(self, event: TpmsEvent, payload: dict) -> TpmsPrompt:
        if event == TpmsEvent.ABORT:
            return self._fail("aborted_by_user", "Aborted by technician.")
        if event == TpmsEvent.READ:
            return await self._read(payload)
        if event == TpmsEvent.RELEARN:
            return await self._relearn()
        if event == TpmsEvent.FINISH:
            return await self._finish()
        return self._fail("unhandled_event", f"event {event!r} not handled")

    # ── 1. READ ────────────────────────────────────────────────────────
    async def _read(self, payload: dict) -> TpmsPrompt:
        if self.state != TpmsState.IDLE:
            raise IllegalTpmsTransition(
                f"READ only valid in IDLE (now {self.state.value})",
            )
        self.data.vin = (payload.get("vin") or self.data.vin or "").strip().upper()
        self.data.technician_id = (
            payload.get("technician_id") or self.data.technician_id or "").strip()

        if self.entitlement is not None:
            entitled, reason = self.entitlement.check()
            if not entitled:
                return self._fail("not_entitled", reason)

        report: SafetyReport = await self.safety.probe(
            require=_TPMS_SAFETY.to_require())
        self.data.safety_voltage_v = report.voltage_v
        if not report.ok:
            return self._fail(
                "prereq_failed",
                "الشروط مش مظبوطة: " + " | ".join(report.refusal_reasons),
            )

        sensors = await self.provider.read_sensors()
        self.data.sensors = tuple(sensors)
        result = TpmsReadResult(self.data.sensors)
        self._advance(TpmsState.READ_DONE)

        notes: list[str] = []
        if result.weak_batteries:
            notes.append("بطارية ضعيفة في: " + ", ".join(result.weak_batteries))
        if result.out_of_range:
            notes.append("ضغط غير مظبوط في: " + ", ".join(result.out_of_range))
        body = "اتقرأت بيانات الحساسات. " + (
            " | ".join(notes) if notes
            else "كل الحساسات سليمة والضغط في المعدل ✅."
        ) + " اضغط RELEARN عشان نعيد ربط كل حساس بمكانه."

        return TpmsPrompt(
            state=self.state,
            title="قراءة حساسات الإطارات",
            body=body,
            expects="RELEARN",
            progress_pct=_PROGRESS[self.state],
            payload={"read": result.to_dict()},
        )

    # ── 2. RELEARN ─────────────────────────────────────────────────────
    async def _relearn(self) -> TpmsPrompt:
        if self.state != TpmsState.READ_DONE:
            raise IllegalTpmsTransition(
                f"RELEARN only valid in READ_DONE (now {self.state.value})",
            )
        await self.provider.enter_extended_session()
        if self.data.needs_security:
            await self.provider.unlock_security(vin=self.data.vin)

        bindings = {s.position: s.sensor_id for s in self.data.sensors}
        await self.provider.relearn(bindings=bindings)
        self.data.relearned = True
        self._advance(TpmsState.RELEARNED)
        return TpmsPrompt(
            state=self.state,
            title="تم ربط الحساسات",
            body=("اتعمل relearn لكل حساس على مكانه الصحيح. اضغط FINISH "
                  "لإنهاء الجلسة وتسجيل الخدمة."),
            expects="FINISH",
            progress_pct=_PROGRESS[self.state],
            payload={"bindings": bindings},
        )

    # ── 3. FINISH ──────────────────────────────────────────────────────
    async def _finish(self) -> TpmsPrompt:
        if self.state != TpmsState.RELEARNED:
            raise IllegalTpmsTransition(
                f"FINISH only valid in RELEARNED (now {self.state.value})",
            )
        self._advance(TpmsState.DONE)
        if self.entitlement is not None:
            op_ref = f"tpms-{self.data.vin or 'no-vin'}"
            self.entitlement.consume(vin=self.data.vin, operation_ref=op_ref)
        return TpmsPrompt(
            state=self.state,
            title="اكتملت خدمة الـ TPMS ✅",
            body=("اتظبطت حساسات الإطارات وكل واحد اترَبط بمكانه. لو فيه "
                  "بطارية ضعيفة اتعرضت فوق، انصح العميل بتغيير الحساس قريب."),
            expects="",
            progress_pct=100,
            payload={"vin": self.data.vin, "relearned": self.data.relearned},
            is_terminal=True,
        )

    # ── snapshot / restore ─────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "data": {
                "vin": self.data.vin,
                "technician_id": self.data.technician_id,
                "needs_security": self.data.needs_security,
                "safety_voltage_v": self.data.safety_voltage_v,
                "sensors": [s.to_dict() for s in self.data.sensors],
                "relearned": self.data.relearned,
                "error_code": self.data.error_code,
                "error_detail": self.data.error_detail,
            },
        }

    @classmethod
    def restore(cls, *, safety: AbstractSafetyGate,
                provider: AbstractTpmsProvider,
                snapshot: dict[str, Any],
                entitlement: Optional["AbstractEntitlementGuard"] = None,
                ) -> "TpmsRelearnOrchestrator":
        s = snapshot["data"]
        sensors = tuple(
            TpmsSensor(
                position=d["position"], sensor_id=d["sensor_id"],
                pressure_bar=float(d["pressure_bar"]),
                temp_c=float(d["temp_c"]), battery_ok=bool(d["battery_ok"]),
            )
            for d in (s.get("sensors") or [])
        )
        data = TpmsData(
            vin=s.get("vin", ""),
            technician_id=s.get("technician_id", ""),
            needs_security=bool(s.get("needs_security")),
            safety_voltage_v=float(s.get("safety_voltage_v") or 0.0),
            sensors=sensors,
            relearned=bool(s.get("relearned")),
            error_code=s.get("error_code", ""),
            error_detail=s.get("error_detail", ""),
        )
        return cls(safety=safety, provider=provider, data=data,
                   state=TpmsState(snapshot["state"]),
                   entitlement=entitlement)
