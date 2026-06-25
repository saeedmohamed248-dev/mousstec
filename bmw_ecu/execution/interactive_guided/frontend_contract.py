"""Frontend ↔ backend JSON contract for the wizard.

Backend yields `WizardStep` objects (serialised to JSON). The frontend
renders, captures technician input, and POSTs back a `WizardResponse`.
"""
from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


class WizardStepKind(str, enum.Enum):
    SHOW_PINOUT = "show_pinout"
    CONFIRM_POWER = "confirm_power"
    CONFIRM_GLITCH = "confirm_glitch"
    CAPTURE_ISN = "capture_isn"
    CONFIRM_INJECTION = "confirm_injection"
    SHOW_RESULT = "show_result"
    ERROR = "error"


@dataclass
class WizardStep:
    kind: WizardStepKind
    title: str
    instructions: str                                 # Arabic / English mix supported
    pinout_diagram_url: Optional[str] = None
    pinout_callouts: list[dict[str, Any]] = field(default_factory=list)
    input_schema: Optional[dict[str, Any]] = None     # JSON-schema for the technician input
    timeout_s: int = 600
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


@dataclass
class WizardResponse:
    """What the frontend posts back."""
    step_kind: WizardStepKind
    confirmed: bool = False
    isn_hex: Optional[str] = None                    # 64-char hex for 32-byte ISN
    notes: str = ""
    technician_id: str = ""

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "WizardResponse":
        return cls(
            step_kind=WizardStepKind(payload["step_kind"]),
            confirmed=bool(payload.get("confirmed", False)),
            isn_hex=payload.get("isn_hex"),
            notes=payload.get("notes", ""),
            technician_id=payload.get("technician_id", ""),
        )
