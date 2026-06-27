"""Pull the 32-byte ISN out of a parsed EEPROM dump.

Trivial today (just `dump.isn`) but kept as a separate module so we have
a single place to wire in:
  • per-VIN sanity checks (e.g. compare against last seen ISN for this
    VIN in the cloud-sync table);
  • XOR-mask de-obfuscation if Mousstec adds a new CAS variant whose
    ISN is stored XOR'd with a fixed key;
  • length re-normalisation if a future chip variant stores the ISN as
    16 bytes packed.

Keeping it isolated also means the bench orchestrator can call
extract_isn_from_dump() without depending on the EEPROM layout
constants directly.
"""
from __future__ import annotations

from ..exceptions import IsnMismatch
from .eeprom_dump import EepromDump


def extract_isn_from_dump(dump: EepromDump, *, expected_length: int = 32
                          ) -> bytes:
    """Return the ISN bytes, validating shape + plausibility.

    parse_dump() has already enforced "non-virgin", but a malformed
    layout could still hand us a truncated ISN — re-check here so the
    orchestrator gets a single, well-defined error type
    (IsnMismatch) regardless of where the data came from.
    """
    isn = dump.isn
    if len(isn) != expected_length:
        raise IsnMismatch(
            f"ISN window length mismatch: expected {expected_length}, "
            f"got {len(isn)} for chip {dump.chip!r}",
            got_len=len(isn),
        )
    if all(b == 0x00 for b in isn) or all(b == 0xFF for b in isn):
        # parse_dump() should have caught this, but check again so a
        # caller that built a dump manually (test code) can't bypass.
        raise IsnMismatch(
            "ISN is virgin (all 0x00 or 0xFF) — refuse to use",
        )
    return bytes(isn)
