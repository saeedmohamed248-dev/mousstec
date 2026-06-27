"""EEPROM dump parser + validator for CAS3 / CAS3+ M35080 / M35128.

The dump is a flat byte blob; the layout is *chip-specific*. This
module gives the orchestrator three guarantees:

  1. parse_dump() refuses obviously-invalid blobs (wrong size, all
     0x00 / all 0xFF, no plausible ISN window) BEFORE any irreversible
     write operation can be queued.
  2. EepromDump exposes a typed accessor for the ISN window so callers
     don't have to remember the per-chip offsets.
  3. A handful of "key slot state" decoder helpers so key_generation.py
     can pick a free slot without re-implementing the byte layout.

Real BMW dumps obviously carry a lot more (VIN, mileage, FA, KM-stand
checksum). We model only what the bench-mode workflow needs; everything
else stays opaque bytes.
"""
from __future__ import annotations

from dataclasses import dataclass

# Per-chip layout — extend by adding another row.
#   (chip, expected_size, isn_offset, isn_length, key_slots_offset, key_slot_stride)
_CHIP_LAYOUTS: dict[str, dict] = {
    "M35080": {
        "size": 512, "isn_offset": 0x20, "isn_length": 32,
        "key_slots_offset": 0x80, "key_slot_stride": 32,
        "key_slot_count": 4,
    },
    "M35128": {
        "size": 1024, "isn_offset": 0x40, "isn_length": 32,
        "key_slots_offset": 0x100, "key_slot_stride": 32,
        "key_slot_count": 8,
    },
}


class EepromParseError(ValueError):
    """Raised when a raw dump fails structural validation."""


@dataclass(frozen=True)
class EepromDump:
    """Decoded view over a raw EEPROM blob. Immutable on purpose — any
    edit goes through a fresh `bytearray` + a re-parse so test code
    can't accidentally mutate a backup."""
    raw: bytes
    chip: str
    isn_offset: int
    isn_length: int
    key_slots_offset: int
    key_slot_stride: int
    key_slot_count: int

    @property
    def isn(self) -> bytes:
        return self.raw[self.isn_offset:self.isn_offset + self.isn_length]

    def key_slot(self, idx: int) -> bytes:
        if idx < 0 or idx >= self.key_slot_count:
            raise IndexError(
                f"key slot {idx} out of range [0, {self.key_slot_count})",
            )
        start = self.key_slots_offset + idx * self.key_slot_stride
        return self.raw[start:start + self.key_slot_stride]

    def is_key_slot_free(self, idx: int) -> bool:
        """A slot is free when the bytes are uniform 0x00 or 0xFF.
        Production CAS firmware writes 0xFF on a virgin / cleared slot."""
        slot = self.key_slot(idx)
        return all(b == 0xFF for b in slot) or all(b == 0x00 for b in slot)


def parse_dump(raw: bytes, *, chip: str) -> EepromDump:
    """Validate and decode a raw EEPROM blob.

    Failure modes (all raise EepromParseError):
      • unknown chip
      • wrong byte length (mismatched chip)
      • dump is entirely 0x00 or 0xFF (uninitialised reader / dead chip)
      • the ISN window is uniform 0x00 / 0xFF (virgin CAS or read miss)
    """
    layout = _CHIP_LAYOUTS.get(chip)
    if layout is None:
        raise EepromParseError(f"Unknown chip family: {chip!r}")
    if len(raw) != layout["size"]:
        raise EepromParseError(
            f"Dump size mismatch for {chip}: expected {layout['size']} bytes, "
            f"got {len(raw)}",
        )
    if all(b == 0x00 for b in raw) or all(b == 0xFF for b in raw):
        raise EepromParseError(
            "Dump is uniformly 0x00 / 0xFF — the harness probably failed to "
            "read the chip (try re-seating the EEPROM clip and re-running).",
        )

    isn_offset = layout["isn_offset"]
    isn_length = layout["isn_length"]
    isn_bytes = raw[isn_offset:isn_offset + isn_length]
    if (all(b == 0xFF for b in isn_bytes)
            or all(b == 0x00 for b in isn_bytes)):
        raise EepromParseError(
            "ISN window is virgin — refuse to use (this CAS has never been "
            "paired). Did you read the right chip?",
        )

    return EepromDump(
        raw=bytes(raw),
        chip=chip,
        isn_offset=isn_offset,
        isn_length=isn_length,
        key_slots_offset=layout["key_slots_offset"],
        key_slot_stride=layout["key_slot_stride"],
        key_slot_count=layout["key_slot_count"],
    )


def build_test_dump(*, chip: str, isn: bytes,
                    occupied_slots: tuple[int, ...] = ()
                    ) -> bytes:
    """Helper for tests: build a syntactically-valid dump with the
    requested ISN window + a configurable set of occupied key slots.

    Production code never calls this — it's exported so the
    bench_orchestrator + tests share one source of truth for what a
    "well-formed" dump looks like.
    """
    layout = _CHIP_LAYOUTS[chip]
    size = layout["size"]
    buf = bytearray(b"\xAA" * size)  # neutral filler that's not 0x00/0xFF
    if len(isn) != layout["isn_length"]:
        raise EepromParseError(
            f"isn must be {layout['isn_length']} bytes for {chip}",
        )
    isn_off = layout["isn_offset"]
    buf[isn_off:isn_off + len(isn)] = isn

    # Mark every slot as free (0xFF) by default — tests opt into
    # occupancy via the `occupied_slots` argument.
    for i in range(layout["key_slot_count"]):
        start = layout["key_slots_offset"] + i * layout["key_slot_stride"]
        buf[start:start + layout["key_slot_stride"]] = b"\xFF" * layout["key_slot_stride"]
    for occ in occupied_slots:
        start = layout["key_slots_offset"] + occ * layout["key_slot_stride"]
        # Non-uniform pattern so is_key_slot_free() returns False.
        buf[start:start + layout["key_slot_stride"]] = bytes(
            (i + 1) & 0xFE for i in range(layout["key_slot_stride"]))
    return bytes(buf)
