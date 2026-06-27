"""Scan transport contract + a hardware-free mock.

The full-system orchestrator never talks to a bus directly. It drives an
`AbstractScanProvider`: ask which modules answered, pull each module's
raw fault list, read the VIN, and (optionally) clear a module's memory.

Production wires a concrete provider over the existing UDS client
(`ReadDTCInformation 0x19` per module across the ZGW). Tests inject
`MockScanProvider`, pre-loaded with a per-module fault table, so every
report shape + every "module didn't answer" branch is deterministic and
runs in microseconds.

Raw fault entries are `(code, status_byte)` tuples — decoding into
severities happens later in `dtc_decoder`, keeping the transport dumb.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional

RawFault = tuple[str, int]   # (dtc_code, iso14229_status_byte)


class ScanTransportError(Exception):
    """Raised when the bus itself is unreachable (no gateway, no power)."""


class AbstractScanProvider(abc.ABC):
    @abc.abstractmethod
    async def read_vin(self) -> str: ...

    @abc.abstractmethod
    async def list_reachable_modules(self) -> list[str]:
        """Module codes that answered a tester-present ping. May be a
        subset of the chassis' expected set — a non-answering module is
        itself a finding the orchestrator surfaces."""

    @abc.abstractmethod
    async def read_module_dtcs(self, module_code: str) -> list[RawFault]:
        """Raw fault memory for ONE module. Empty list = clean module."""

    @abc.abstractmethod
    async def clear_module_dtcs(self, module_code: str) -> bool:
        """ClearDiagnosticInformation 0x14 for one module. Returns True
        on positive response."""


# ─────────────────────────────────────────────────────────────────────
# Mock — the only provider the test-suite ever uses.
# ─────────────────────────────────────────────────────────────────────
@dataclass
class MockScanProvider(AbstractScanProvider):
    """Deterministic test double.

    Configure:
      • vin                — returned by read_vin()
      • reachable          — module codes that answer (None ⇒ all keys of
                             `faults`, so you only specify faults).
      • faults             — {module_code: [(code, status_byte), ...]}.
      • gateway_down       — read_vin / list raise ScanTransportError.
      • unclearable        — module codes whose clear() returns False
                             (e.g. ACSM with stored crash data).

    Records:
      • dtc_read_calls     — modules whose DTCs were read, in order.
      • clear_calls        — modules whose memory was cleared, in order.
    """
    vin: str = "WBADEMOSCAN000001"
    faults: dict[str, list[RawFault]] = field(default_factory=dict)
    reachable: Optional[list[str]] = None
    gateway_down: bool = False
    unclearable: tuple[str, ...] = ()

    dtc_read_calls: list[str] = field(default_factory=list)
    clear_calls: list[str] = field(default_factory=list)

    async def read_vin(self) -> str:
        if self.gateway_down:
            raise ScanTransportError(
                "البوابة المركزية (ZGW) مش بترد — اتأكد من الكابل والكونتاكت."
            )
        return self.vin

    async def list_reachable_modules(self) -> list[str]:
        if self.gateway_down:
            raise ScanTransportError(
                "مفيش رد من شبكة الـ CAN — البوابة المركزية مقطوعة."
            )
        if self.reachable is not None:
            return list(self.reachable)
        return list(self.faults.keys())

    async def read_module_dtcs(self, module_code: str) -> list[RawFault]:
        self.dtc_read_calls.append(module_code)
        return list(self.faults.get(module_code, []))

    async def clear_module_dtcs(self, module_code: str) -> bool:
        self.clear_calls.append(module_code)
        return module_code not in self.unclearable
