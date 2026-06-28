"""Dynamic, data-driven hardware auto-detection for bench coding/flashing."""
from __future__ import annotations

from .ecu_hardware_catalog import (  # noqa: F401
    BenchPinout,
    HardwareProfile,
    all_hardware_ids,
    get_hardware_profile,
    register_hardware_profile,
)
from .n20_auto_orchestrator import (  # noqa: F401
    BenchStep,
    HardwareProbe,
    N20AutoOrchestrator,
    TprotStatus,
    TPROT_STATUS_DID,
)
