"""Transport contract for service-reset procedures + a hardware-free mock.

The generic orchestrator drives ANY procedure through this one interface:
open an extended diagnostic session, optionally unlock security access,
run RoutineControl routines, and read/write data identifiers. Production
wires it over the existing UdsClient (services/uds); tests inject
`MockResetProvider`, which records every call and can be told to fail a
specific routine so refusal paths are deterministic.

`RoutineOutcome` carries a success flag + an opaque result payload (e.g.
DPF soot mass, EPB caliper position) the orchestrator surfaces verbatim.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional


class ResetTransportError(Exception):
    """Bus-level failure (no module, lost comms)."""


class SecurityAccessDenied(Exception):
    """The module rejected the security-access handshake."""


class RoutineRejected(Exception):
    """The module returned a negative response to a routine/DID op."""


@dataclass
class RoutineOutcome:
    ok: bool
    result: dict = field(default_factory=dict)


class AbstractResetProvider(abc.ABC):
    @abc.abstractmethod
    async def enter_extended_session(self) -> None: ...

    @abc.abstractmethod
    async def unlock_security(self, *, vin: str) -> None: ...

    @abc.abstractmethod
    async def run_routine_start(self, rid: int) -> RoutineOutcome: ...

    @abc.abstractmethod
    async def run_routine_result(self, rid: int) -> RoutineOutcome: ...

    @abc.abstractmethod
    async def read_did(self, did: int) -> RoutineOutcome: ...

    @abc.abstractmethod
    async def write_did(self, did: int, payload: bytes) -> RoutineOutcome: ...


# ─────────────────────────────────────────────────────────────────────
@dataclass
class MockResetProvider(AbstractResetProvider):
    """Deterministic test double.

    Config:
      • deny_security    — unlock_security() raises SecurityAccessDenied.
      • fail_rids        — RIDs whose start/result returns ok=False.
      • reject_rids      — RIDs whose start RAISES RoutineRejected.
      • bus_down         — enter_extended_session raises ResetTransportError.
      • results          — {rid_or_did: dict} payloads echoed back.

    Records:
      • session_calls / security_calls / routine_starts /
        routine_results / did_reads / did_writes.
    """
    deny_security: bool = False
    fail_rids: tuple[int, ...] = ()
    reject_rids: tuple[int, ...] = ()
    bus_down: bool = False
    results: dict[int, dict] = field(default_factory=dict)

    session_calls: int = 0
    security_calls: list[str] = field(default_factory=list)
    routine_starts: list[int] = field(default_factory=list)
    routine_results: list[int] = field(default_factory=list)
    did_reads: list[int] = field(default_factory=list)
    did_writes: list[tuple[int, bytes]] = field(default_factory=list)

    async def enter_extended_session(self) -> None:
        self.session_calls += 1
        if self.bus_down:
            raise ResetTransportError(
                "الموديول مش بيرد — اتأكد من الكونتاكت والكابل."
            )

    async def unlock_security(self, *, vin: str) -> None:
        self.security_calls.append(vin)
        if self.deny_security:
            raise SecurityAccessDenied(
                "الموديول رفض security access — جرّب تاني أو راجع الـ seed/key."
            )

    async def run_routine_start(self, rid: int) -> RoutineOutcome:
        self.routine_starts.append(rid)
        if rid in self.reject_rids:
            raise RoutineRejected(
                f"الموديول رفض الروتين 0x{rid:04X} (negative response)."
            )
        return RoutineOutcome(ok=rid not in self.fail_rids,
                              result=dict(self.results.get(rid, {})))

    async def run_routine_result(self, rid: int) -> RoutineOutcome:
        self.routine_results.append(rid)
        return RoutineOutcome(ok=rid not in self.fail_rids,
                              result=dict(self.results.get(rid, {})))

    async def read_did(self, did: int) -> RoutineOutcome:
        self.did_reads.append(did)
        return RoutineOutcome(ok=True, result=dict(self.results.get(did, {})))

    async def write_did(self, did: int, payload: bytes) -> RoutineOutcome:
        self.did_writes.append((did, payload))
        return RoutineOutcome(ok=True, result=dict(self.results.get(did, {})))
