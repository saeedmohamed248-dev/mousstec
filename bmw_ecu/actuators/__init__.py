"""Bidirectional (actuator) tests + TPMS service.

A declarative actuator-test catalog (actuator_catalog.py) interpreted by ONE
generic orchestrator (actuator_orchestrator.py) over an abstract IO-control
transport (actuator_provider.py). The TPMS read + relearn flow lives in
tpms.py. Both are hardware-free and entitlement-gated behind the saleable
features 'bidirectional_tests' and 'tpms_service'.
"""
from __future__ import annotations

from .actuator_catalog import (
    ACTUATOR_CATALOG,
    BIDIRECTIONAL_FEATURE,
    ActuatorTest,
    ControlKind,
    all_actuators,
    get_actuator,
)
from .actuator_orchestrator import (
    ActuatorData,
    ActuatorEvent,
    ActuatorPrompt,
    ActuatorState,
    ActuatorTestOrchestrator,
    IllegalActuatorTransition,
)
from .actuator_provider import (
    AbstractActuatorProvider,
    ActuatorControlRejected,
    ActuatorFeedback,
    ActuatorSecurityDenied,
    ActuatorTransportError,
    MockActuatorProvider,
)
from .tpms import (
    TPMS_FEATURE,
    AbstractTpmsProvider,
    IllegalTpmsTransition,
    MockTpmsProvider,
    TpmsData,
    TpmsEvent,
    TpmsPrompt,
    TpmsReadResult,
    TpmsRelearnOrchestrator,
    TpmsSensor,
    TpmsState,
    TpmsTransportError,
)

__all__ = [
    # catalog
    "ACTUATOR_CATALOG", "BIDIRECTIONAL_FEATURE", "ActuatorTest",
    "ControlKind", "all_actuators", "get_actuator",
    # orchestrator
    "ActuatorData", "ActuatorEvent", "ActuatorPrompt", "ActuatorState",
    "ActuatorTestOrchestrator", "IllegalActuatorTransition",
    # provider
    "AbstractActuatorProvider", "ActuatorControlRejected", "ActuatorFeedback",
    "ActuatorSecurityDenied", "ActuatorTransportError", "MockActuatorProvider",
    # tpms
    "TPMS_FEATURE", "AbstractTpmsProvider", "IllegalTpmsTransition",
    "MockTpmsProvider", "TpmsData", "TpmsEvent", "TpmsPrompt",
    "TpmsReadResult", "TpmsRelearnOrchestrator", "TpmsSensor", "TpmsState",
    "TpmsTransportError",
]
