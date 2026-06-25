"""Strategy contract.

Every execution mode implements `ExecutionStrategy`, takes a
`StrategyContext`, and returns a `StrategyResult`. The Manager + caller
only program against these three types.
"""
from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from typing import Any, Optional

from ..connection.base import AbstractTransport
from ..safety.preflight import PreflightGate
from ..uds.security_access import SecurityAccess
from .capabilities import WorkshopCapabilities
from .ecu_profiles import EcuProfile


class StrategyOutcome(str, enum.Enum):
    SUCCESS = "success"
    PARTIAL = "partial"                  # E.g. extracted but injection pending
    SUSPENDED = "suspended"              # Wizard waiting for technician input
    FAILED_ROLLED_BACK = "failed_rolled_back"
    FAILED_UNRECOVERABLE = "failed_unrecoverable"


@dataclass
class StrategyContext:
    """Everything a strategy needs to operate. Built by the Manager."""
    vin: str
    profile: EcuProfile
    capabilities: WorkshopCapabilities

    transport: AbstractTransport
    security: SecurityAccess
    preflight: PreflightGate

    target_isn: Optional[bytes] = None   # set during extraction → reused in injection
    wizard_session_id: Optional[int] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyResult:
    outcome: StrategyOutcome
    strategy_name: str
    isn: Optional[bytes] = None
    backup_sha256: str = ""
    wizard_next_step: Optional[dict[str, Any]] = None  # JSON payload for the frontend
    error_code: str = ""
    error_message: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.outcome == StrategyOutcome.SUCCESS


class ExecutionStrategy(abc.ABC):
    """Base class for all three execution modes."""

    name: str

    # Manager uses these to filter eligibility. Override in subclasses.
    requires_software_capable: bool = False
    requires_hardware_box: bool = False
    requires_technician: bool = False

    def is_eligible(self, ctx: StrategyContext) -> tuple[bool, str]:
        """Return (eligible, reason_if_not). Reason is shown to the operator."""
        caps = ctx.capabilities
        if self.requires_software_capable and not caps.can_run_software_only():
            return False, "no software-capable transport (need ENET or K+DCAN)"
        if self.requires_hardware_box and not caps.can_run_hardware_automation():
            return False, "no Mousstec Smart Breakout Box detected"
        if self.requires_technician and not caps.can_run_interactive_guided():
            return False, "technician skill / transport requirement not met"
        return True, ""

    @abc.abstractmethod
    async def extract_isn(self, ctx: StrategyContext) -> StrategyResult: ...

    @abc.abstractmethod
    async def inject_isn(self, ctx: StrategyContext) -> StrategyResult: ...

    @abc.abstractmethod
    async def rollback(self, ctx: StrategyContext, *, reason: str) -> StrategyResult:
        """Strategy-specific cleanup. RollbackCoordinator calls this on failure."""
