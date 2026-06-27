"""Shared pre-condition gate for the premium trio.

Every premium service (EGS reset, ACSM clear, CBS battery register)
shares the same pre-flight vocabulary:

  • voltage_v          : current 12V rail measured at the OBD pin
  • gear               : transmission selector state (P/R/N/D/...)
  • ignition           : OFF / KOEO / KOER
  • recent_dtcs        : DTCs read in the last X seconds — used to
                         block a reset when the module reports a fresh
                         fault (e.g. ACSM with an active deployed bag)
  • airbag_modules     : list of (id, state) tuples — the ACSM clear
                         flow refuses to run when ANY airbag module
                         reports DEPLOYED or DISCONNECTED.

`SafetyGate` is the ABC each module's orchestrator consults; production
wires it to the Mousstec Breakout Box's bus snooper, tests use the
deterministic `MockSafetyGate`.

`SafetyReport.refusal_reasons` is the single list every orchestrator
inspects: an empty list means "proceed", any element means "block this
state transition + show the technician the reasons verbatim".
"""
from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from typing import Optional


class GearPosition(str, enum.Enum):
    P = "P"
    R = "R"
    N = "N"
    D = "D"
    UNKNOWN = "?"


class IgnitionState(str, enum.Enum):
    OFF = "off"
    KOEO = "koeo"   # Key On, Engine Off
    KOER = "koer"   # Key On, Engine Running


class AirbagModuleState(str, enum.Enum):
    OK = "ok"
    DEPLOYED = "deployed"
    DISCONNECTED = "disconnected"
    SHORTED = "shorted"
    UNKNOWN = "unknown"


class SafetyViolation(Exception):
    """Raised by `require()` helpers when a pre-condition fails hard."""


@dataclass(frozen=True)
class SafetyRequirement:
    """Declarative pre-condition set that maps onto the SafetyGate
    `require` dict. Shared by every procedure-driven feature (service
    resets, bidirectional actuator tests, TPMS) so the safety vocabulary
    lives in ONE place next to the gate that enforces it.

    Only the fields a procedure actually cares about are set; the rest
    fall back to the gate's defaults."""
    voltage_min_v: float = 12.0
    voltage_max_v: float = 14.8
    gear_in: tuple[GearPosition, ...] = (GearPosition.P,)
    ignition_in: tuple[IgnitionState, ...] = (IgnitionState.KOEO,)
    forbidden_dtcs: tuple[str, ...] = ()

    def to_require(self) -> dict:
        return {
            "voltage_min_v": self.voltage_min_v,
            "voltage_max_v": self.voltage_max_v,
            "gear_in": list(self.gear_in),
            "ignition_in": list(self.ignition_in),
            "forbidden_dtcs": tuple(self.forbidden_dtcs),
        }


@dataclass(frozen=True)
class SafetyReport:
    """Outcome of a SafetyGate probe. `refusal_reasons` is the canonical
    list orchestrators iterate over — empty = OK, non-empty = block."""
    voltage_v: float
    gear: GearPosition
    ignition: IgnitionState
    recent_dtcs: tuple[str, ...] = ()
    airbag_modules: tuple[tuple[str, AirbagModuleState], ...] = ()
    refusal_reasons: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.refusal_reasons


# ─────────────────────────────────────────────────────────────────────
class AbstractSafetyGate(abc.ABC):
    """One method, many constraints. Implementations:
      - read voltage / gear / ignition from the live bus,
      - request a fresh DTC snapshot,
      - poll the airbag module states,
      - compute `refusal_reasons` per the requirements dict the caller
        passes in.
    """

    @abc.abstractmethod
    async def probe(self, *, require: dict) -> SafetyReport:
        """Probe the vehicle + classify against `require`.

        `require` keys (all optional — orchestrator passes what it needs):
          voltage_min_v       : float, default 11.5
          voltage_max_v       : float, default 14.8
          gear_in             : iterable of GearPosition that's acceptable
          ignition_in         : iterable of IgnitionState that's acceptable
          forbidden_dtcs      : iterable of DTC strings that must NOT appear
          forbid_deployed_bag : bool, when True any DEPLOYED airbag → refuse
        """


# ─────────────────────────────────────────────────────────────────────
# Mock — pure-Python, deterministic. Tests configure the values then
# drive an orchestrator through one or more probe() calls.
# ─────────────────────────────────────────────────────────────────────
@dataclass
class MockSafetyGate(AbstractSafetyGate):
    """Tests construct with the desired snapshot. `probe()` honours the
    same `require` dict shape as production so the orchestrator code
    path is identical."""
    voltage_v: float = 12.6
    gear: GearPosition = GearPosition.P
    ignition: IgnitionState = IgnitionState.KOEO
    recent_dtcs: tuple[str, ...] = ()
    airbag_modules: tuple[tuple[str, AirbagModuleState], ...] = ()
    probe_calls: list[dict] = field(default_factory=list)

    async def probe(self, *, require: dict) -> SafetyReport:
        self.probe_calls.append(dict(require))
        reasons: list[str] = []

        vmin = float(require.get("voltage_min_v", 11.5))
        vmax = float(require.get("voltage_max_v", 14.8))
        if self.voltage_v < vmin:
            reasons.append(
                f"voltage too low: {self.voltage_v:.2f} V < min {vmin:.2f} V",
            )
        if self.voltage_v > vmax:
            reasons.append(
                f"voltage too high: {self.voltage_v:.2f} V > max {vmax:.2f} V",
            )

        gear_allowed = require.get("gear_in")
        if gear_allowed is not None and self.gear not in tuple(gear_allowed):
            allowed = "/".join(g.value for g in gear_allowed)
            reasons.append(
                f"gear must be one of [{allowed}], got {self.gear.value}",
            )

        ign_allowed = require.get("ignition_in")
        if ign_allowed is not None and self.ignition not in tuple(ign_allowed):
            allowed = "/".join(i.value for i in ign_allowed)
            reasons.append(
                f"ignition must be one of [{allowed}], got {self.ignition.value}",
            )

        forbid_dtcs = set(require.get("forbidden_dtcs") or ())
        seen_bad = forbid_dtcs.intersection(self.recent_dtcs)
        if seen_bad:
            reasons.append(
                f"forbidden DTCs present: {sorted(seen_bad)}",
            )

        if require.get("forbid_deployed_bag"):
            deployed = [mid for (mid, st) in self.airbag_modules
                        if st in (AirbagModuleState.DEPLOYED,
                                  AirbagModuleState.SHORTED,
                                  AirbagModuleState.DISCONNECTED)]
            if deployed:
                reasons.append(
                    f"airbag module(s) NOT safe to clear: {deployed} — "
                    "physical inspection required before any reset.",
                )

        return SafetyReport(
            voltage_v=self.voltage_v,
            gear=self.gear,
            ignition=self.ignition,
            recent_dtcs=tuple(self.recent_dtcs),
            airbag_modules=tuple(self.airbag_modules),
            refusal_reasons=tuple(reasons),
        )
