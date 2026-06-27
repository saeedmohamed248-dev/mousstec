"""Premium technical services trio: EGS ISN reset, ACSM crash reset,
CBS battery manager.

These three modules round out the chatbot-guided workshop suite the
Mousstec storefront sells. Each follows the same shape as the
bench_orchestrator (sub-commit 4) and frm_recovery (sub-commit 5):

  • Forward-only state machine.
  • Async `handle(event, payload)` that dispatches to a private
    transition method per state.
  • `Prompt` dataclass shaped so the existing chatbot UI renders all
    three with one component.
  • `snapshot()` / `restore()` pair so a WizardSession can persist the
    in-flight session between requests.
  • Mock-driven test path — zero hardware needed.

Why a shared safety gate
------------------------
All three workflows share the same pre-condition vocabulary: stable
battery voltage, gear in P (for the transmission flow), ignition state
(KOEO/KOER), absence of recent crash data. `safety_checks.SafetyGate`
is the single ABC that exposes these probes; production wires it to
the Mousstec Breakout Box's CAN-bus inspector while tests use
`MockSafetyGate` to assert orchestrator behaviour on each failure
mode independently.
"""
from __future__ import annotations

from .acsm_crash_reset import (
    AcsmCrashOrchestrator,
    AcsmCrashState,
    AcsmCrashEvent,
    AcsmCrashPrompt,
    AcsmCrashData,
    AcsmSafetyBlocked,
    IllegalAcsmTransition,
)
from .cbs_battery_manager import (
    BatteryType,
    BatterySpec,
    CbsBatteryOrchestrator,
    CbsBatteryState,
    CbsBatteryEvent,
    CbsBatteryPrompt,
    CbsBatteryData,
    IllegalCbsTransition,
)
from .egs_isn_reset import (
    EgsIsnOrchestrator,
    EgsIsnState,
    EgsIsnEvent,
    EgsIsnPrompt,
    EgsIsnData,
    EgsResetRefused,
    IllegalEgsTransition,
)
from .safety_checks import (
    AbstractSafetyGate,
    AirbagModuleState,
    GearPosition,
    IgnitionState,
    MockSafetyGate,
    SafetyReport,
    SafetyViolation,
)

__all__ = [
    # safety gate
    "AbstractSafetyGate",
    "AirbagModuleState",
    "GearPosition",
    "IgnitionState",
    "MockSafetyGate",
    "SafetyReport",
    "SafetyViolation",
    # EGS
    "EgsIsnOrchestrator",
    "EgsIsnState",
    "EgsIsnEvent",
    "EgsIsnPrompt",
    "EgsIsnData",
    "EgsResetRefused",
    "IllegalEgsTransition",
    # ACSM
    "AcsmCrashOrchestrator",
    "AcsmCrashState",
    "AcsmCrashEvent",
    "AcsmCrashPrompt",
    "AcsmCrashData",
    "AcsmSafetyBlocked",
    "IllegalAcsmTransition",
    # CBS
    "BatteryType",
    "BatterySpec",
    "CbsBatteryOrchestrator",
    "CbsBatteryState",
    "CbsBatteryEvent",
    "CbsBatteryPrompt",
    "CbsBatteryData",
    "IllegalCbsTransition",
]
