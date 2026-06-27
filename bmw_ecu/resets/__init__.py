"""Service Resets — oil / EPB / SAS / DPF / throttle adaptation.

A declarative procedure catalog (procedures.py) interpreted by ONE generic
orchestrator (reset_orchestrator.py) over an abstract transport
(reset_provider.py). Hardware-free + entitlement-gated behind the single
saleable feature 'service_resets'.
"""
from __future__ import annotations

from .procedures import (
    PROCEDURE_CATALOG,
    SERVICE_RESETS_FEATURE,
    ResetStep,
    SafetyRequirement,
    ServiceProcedure,
    StepKind,
    all_procedures,
    get_procedure,
)
from .reset_orchestrator import (
    ResetData,
    ResetEvent,
    ResetPrompt,
    ResetState,
    ServiceResetOrchestrator,
)
from .reset_provider import (
    AbstractResetProvider,
    MockResetProvider,
    ResetTransportError,
    RoutineOutcome,
    RoutineRejected,
    SecurityAccessDenied,
)

__all__ = [
    # procedures
    "PROCEDURE_CATALOG", "SERVICE_RESETS_FEATURE", "ResetStep",
    "SafetyRequirement", "ServiceProcedure", "StepKind", "all_procedures",
    "get_procedure",
    # orchestrator
    "ResetData", "ResetEvent", "ResetPrompt", "ResetState",
    "ServiceResetOrchestrator",
    # provider
    "AbstractResetProvider", "MockResetProvider", "ResetTransportError",
    "RoutineOutcome", "RoutineRejected", "SecurityAccessDenied",
]
