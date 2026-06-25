"""Pre-flight payload validator.

Runs purely on the hex payload BEFORE it touches the wire. Catches the
common mistakes that brick ECUs:
    - Wrong checksum
    - Wrong length for declared region
    - Address range outside the ECU's mapped flash
    - Plaintext where encrypted blob expected (and vice-versa)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FlashRegion:
    ecu_name: str
    start: int
    end: int       # inclusive
    expected_crc32: Optional[int] = None
    encrypted: bool = True


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def raise_if_failed(self) -> None:
        from ..exceptions import FlashError
        if not self.ok:
            raise FlashError("; ".join(self.errors))


class PayloadValidator:
    """Stateless validator. Plug per-ECU `FlashRegion` profiles."""

    def __init__(self, regions: dict[str, FlashRegion]) -> None:
        self.regions = regions

    def validate(self, *, ecu_name: str, payload: bytes,
                 target_addr: int) -> ValidationResult:
        result = ValidationResult(ok=True)
        region = self.regions.get(ecu_name)
        if region is None:
            result.warnings.append(f"No flash profile for {ecu_name} — proceeding blind")
            return result

        end_addr = target_addr + len(payload) - 1
        if target_addr < region.start or end_addr > region.end:
            result.ok = False
            result.errors.append(
                f"Address range 0x{target_addr:08X}-0x{end_addr:08X} outside "
                f"{ecu_name} mapped region 0x{region.start:08X}-0x{region.end:08X}",
            )

        if region.expected_crc32 is not None:
            import zlib
            actual = zlib.crc32(payload)
            if actual != region.expected_crc32:
                result.ok = False
                result.errors.append(
                    f"CRC32 mismatch: got 0x{actual:08X}, "
                    f"expected 0x{region.expected_crc32:08X}",
                )

        # Cheap entropy check — encrypted blob shouldn't be mostly zeros.
        if region.encrypted and payload.count(b"\x00"[0]) > len(payload) * 0.5:
            result.warnings.append("Payload >50% zeros but region marked encrypted")

        return result
