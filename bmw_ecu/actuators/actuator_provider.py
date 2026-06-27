"""Transport for bidirectional (IO-control) tests + a hardware-free mock.

UDS InputOutputControlByIdentifier (0x2F) lets the tester seize an output,
drive it, and hand control back. The critical safety invariant: control
is ALWAYS returned to the ECU — a forced-on fan or fuel pump left running
is dangerous. The orchestrator enforces that; this provider exposes the
two halves (`io_activate` / `io_return`) plus a feedback read so the
orchestrator can confirm the ECU saw the output move.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional


class ActuatorTransportError(Exception):
    """Bus-level failure (module not answering)."""


class ActuatorSecurityDenied(Exception):
    """Security access rejected — many actuator DIDs are protected."""


class ActuatorControlRejected(Exception):
    """The ECU refused to hand over control of the output."""


@dataclass
class ActuatorFeedback:
    ok: bool
    detail: dict = field(default_factory=dict)


class AbstractActuatorProvider(abc.ABC):
    @abc.abstractmethod
    async def enter_extended_session(self) -> None: ...

    @abc.abstractmethod
    async def unlock_security(self, *, vin: str) -> None: ...

    @abc.abstractmethod
    async def io_activate(self, io_did: int, *, kind: str,
                          duration_s: int) -> ActuatorFeedback:
        """Seize + drive the output. `kind` ∈ activate/pulse/cycle."""

    @abc.abstractmethod
    async def io_return(self, io_did: int) -> ActuatorFeedback:
        """Return control of the output to the ECU. MUST be idempotent —
        the orchestrator calls it on the normal path AND on abort."""


# ─────────────────────────────────────────────────────────────────────
@dataclass
class MockActuatorProvider(AbstractActuatorProvider):
    """Deterministic test double.

    Config:
      • bus_down        — enter_extended_session raises.
      • deny_security   — unlock_security raises.
      • reject_dids     — io_activate raises ActuatorControlRejected.
      • feedback        — {io_did: dict} echoed in io_activate feedback.

    Records:
      • session_calls / security_calls / activate_calls / return_calls.
    """
    bus_down: bool = False
    deny_security: bool = False
    reject_dids: tuple[int, ...] = ()
    feedback: dict[int, dict] = field(default_factory=dict)

    session_calls: int = 0
    security_calls: list[str] = field(default_factory=list)
    activate_calls: list[tuple[int, str, int]] = field(default_factory=list)
    return_calls: list[int] = field(default_factory=list)

    async def enter_extended_session(self) -> None:
        self.session_calls += 1
        if self.bus_down:
            raise ActuatorTransportError(
                "الموديول مش بيرد — اتأكد من الكونتاكت والكابل."
            )

    async def unlock_security(self, *, vin: str) -> None:
        self.security_calls.append(vin)
        if self.deny_security:
            raise ActuatorSecurityDenied(
                "الموديول رفض security access قبل اختبار المُشغّل."
            )

    async def io_activate(self, io_did: int, *, kind: str,
                          duration_s: int) -> ActuatorFeedback:
        self.activate_calls.append((io_did, kind, duration_s))
        if io_did in self.reject_dids:
            raise ActuatorControlRejected(
                f"الـ ECU رفض تسليم التحكم في 0x{io_did:04X}."
            )
        return ActuatorFeedback(ok=True,
                                detail=dict(self.feedback.get(io_did, {})))

    async def io_return(self, io_did: int) -> ActuatorFeedback:
        self.return_calls.append(io_did)
        return ActuatorFeedback(ok=True)
