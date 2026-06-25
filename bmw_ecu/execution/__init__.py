"""Multi-modal ECU execution framework.

Three strategies plug into one Manager:
    1. SoftwareOnlyStrategy        — UDS exploits / forced boot mode
    2. HardwareAutomationStrategy  — Mousstec Smart Breakout Box
    3. InteractiveGuidedStrategy   — Technician wizard (suspend / resume)

Use `ExecutionStrategyManager` — never instantiate a strategy directly.
"""
from .base import (  # noqa: F401
    ExecutionStrategy,
    StrategyContext,
    StrategyResult,
    StrategyOutcome,
)
from .capabilities import WorkshopCapabilities  # noqa: F401
from .ecu_profiles import EcuProfile, ProtectionLevel, KNOWN_PROFILES  # noqa: F401
from .manager import ExecutionStrategyManager  # noqa: F401
