"""Wizard state machine.

States move forward only:

    INIT → SHOWING_PINOUT → AWAITING_POWER → AWAITING_GLITCH →
    AWAITING_ISN → INJECTING → DONE
                                              ↘ FAILED

Each transition is persisted (WizardSession model) so the session
survives a backend restart or a technician taking lunch.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


class WizardState(str, enum.Enum):
    INIT = "init"
    SHOWING_PINOUT = "showing_pinout"
    AWAITING_POWER = "awaiting_power"
    AWAITING_GLITCH = "awaiting_glitch"
    AWAITING_ISN = "awaiting_isn"
    INJECTING = "injecting"
    DONE = "done"
    FAILED = "failed"


@dataclass
class WizardData:
    vin: str = ""
    ecu_name: str = ""
    captured_isn: Optional[bytes] = None
    notes: list[str] = field(default_factory=list)
    error_code: str = ""


_ALLOWED: dict[WizardState, set[WizardState]] = {
    WizardState.INIT: {WizardState.SHOWING_PINOUT, WizardState.FAILED},
    WizardState.SHOWING_PINOUT: {WizardState.AWAITING_POWER, WizardState.FAILED},
    WizardState.AWAITING_POWER: {WizardState.AWAITING_GLITCH, WizardState.FAILED},
    WizardState.AWAITING_GLITCH: {WizardState.AWAITING_ISN, WizardState.FAILED},
    WizardState.AWAITING_ISN: {WizardState.INJECTING, WizardState.FAILED},
    WizardState.INJECTING: {WizardState.DONE, WizardState.FAILED},
    WizardState.DONE: set(),
    WizardState.FAILED: set(),
}


class IllegalTransition(Exception):
    pass


@dataclass
class WizardStateMachine:
    state: WizardState = WizardState.INIT
    data: WizardData = field(default_factory=WizardData)

    def advance(self, to: WizardState) -> None:
        if to not in _ALLOWED[self.state]:
            raise IllegalTransition(f"{self.state.value} → {to.value} not allowed")
        self.state = to

    def fail(self, reason: str) -> None:
        self.data.error_code = reason
        self.state = WizardState.FAILED

    @property
    def is_terminal(self) -> bool:
        return self.state in (WizardState.DONE, WizardState.FAILED)
