"""Cloud-driven D-Flash rebuild.

The "cloud" here is conceptual: the Mousstec server keeps a small set
of *template* dumps per chassis (E90 / E70 / R56) — one known-good
D-Flash blob recorded from a healthy module of the same variant. The
rebuilder fuses the template with the per-vehicle data we can still
recover from the corrupted dump (VIN, optionally the FA bytes that
survived), then re-computes the XOR checksum.

In production the actual templates ship as pickled bytes in a
versioned dump-bank table; the local stub in this file uses a synthetic
template generator so the rebuild logic + the tests share one source
of truth.

Why bother
----------
A naïve "flash the template back" is wrong: the FRM3's VIN is bound to
the BMW asset register, so flashing a foreign VIN into a customer's
module would brand the workshop fraudulent. The rebuilder enforces:
  • VIN comes from the technician (job sheet) — never the template.
  • FA bytes come from the dump first, fall back to template defaults.
  • Final checksum byte is RECOMPUTED so a future analyzer agrees.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .dflash_corruption import _xor_checksum
from .frm_profiles import FrmProfile


class CloudRebuildError(Exception):
    """Raised when the rebuilder refuses to emit a blob (bad VIN, etc.)."""


@dataclass(frozen=True)
class CloudRebuildResult:
    """What the rebuilder hands back to the orchestrator."""
    rebuilt_bytes: bytes
    template_version: str
    vin_used: str
    fa_carried_over: bool   # True when we kept the dump's FA over the template's
    notes: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────
# Stub template generator — production replaces this with a DB lookup.
# We expose it as a function so tests can build templates inline and
# both halves of the codebase (rebuild + tests) read from the same
# definition.
# ─────────────────────────────────────────────────────────────────────
def build_template_blob(profile: FrmProfile) -> bytes:
    """Synthesise a known-good blob for `profile`. Used both by the
    rebuilder (when a real template can't be fetched) and by tests."""
    buf = bytearray(0xCD for _ in range(profile.dflash_size))  # neutral filler
    # Placeholder VIN — caller overwrites.
    buf[profile.vin_offset:profile.vin_offset + profile.vin_length] = b"X" * profile.vin_length
    # Plausible FA pattern — alternating bytes so analyze_dflash() doesn't
    # think the template itself is corrupted.
    fa_slice = bytearray(
        ((i * 73) ^ 0x5A) & 0xFF for i in range(profile.fa_length))
    buf[profile.fa_offset:profile.fa_offset + profile.fa_length] = fa_slice
    # Compute + write the checksum so the template ships internally valid.
    body = bytes(buf[:profile.checksum_offset])
    buf[profile.checksum_offset] = _xor_checksum(body)
    return bytes(buf)


def _validate_vin(vin: str) -> None:
    if not vin:
        raise CloudRebuildError("vin is required for rebuild")
    if len(vin) != 17:
        raise CloudRebuildError(f"vin must be 17 chars, got {len(vin)}")
    for ch in vin:
        if not (ch.isalnum() and ch.isascii() and ch.upper() == ch):
            raise CloudRebuildError(
                f"vin char {ch!r} is not uppercase alphanumeric ASCII",
            )


def rebuild_dflash(
    *,
    profile: FrmProfile,
    corrupted_dump: bytes,
    vin: str,
    template: Optional[bytes] = None,
    template_version: str = "stub_v1",
    fa_recoverable: bool = False,
) -> CloudRebuildResult:
    """Fuse a template + the recoverable parts of the corrupted dump
    into a clean blob.

    Args:
      profile          : the FRM variant — drives offsets.
      corrupted_dump   : bytes from the BDM read; may be partially valid.
      vin              : the VIN from the technician's job sheet.
      template         : pre-fetched template bytes; defaults to the
                         synthesised stub if None.
      template_version : tag stored in the result for audit.
      fa_recoverable   : True when the analyzer said the dump's FA window
                         is plausibly readable — carry it over instead of
                         the template defaults.
    """
    _validate_vin(vin)

    if len(corrupted_dump) != profile.dflash_size:
        raise CloudRebuildError(
            f"corrupted_dump length {len(corrupted_dump)} ≠ expected "
            f"{profile.dflash_size}",
        )

    if template is None:
        template = build_template_blob(profile)
    if len(template) != profile.dflash_size:
        raise CloudRebuildError(
            f"template length {len(template)} ≠ expected {profile.dflash_size}",
        )

    # Start from the template; overlay VIN + (optionally) FA bytes.
    buf = bytearray(template)
    notes: list[str] = []

    # 1️⃣ VIN — always from the technician.
    buf[profile.vin_offset:profile.vin_offset + profile.vin_length] = vin.encode("ascii")
    notes.append(f"VIN {vin} injected at offset 0x{profile.vin_offset:04X}.")

    # 2️⃣ FA — carry over from the dump only when the analyzer judged it
    #     readable. Otherwise the template's defaults stay.
    fa_carried_over = False
    if fa_recoverable:
        buf[profile.fa_offset:profile.fa_offset + profile.fa_length] = (
            corrupted_dump[profile.fa_offset:profile.fa_offset + profile.fa_length]
        )
        fa_carried_over = True
        notes.append("FA block carried over from corrupted dump.")
    else:
        notes.append(
            "FA block sourced from template defaults — technician must "
            "re-inject SALAPA codes after the flash-back.",
        )

    # 3️⃣ Recompute the XOR checksum so a fresh analyze_dflash() returns
    #     checksum_ok=True on the rebuilt blob.
    body = bytes(buf[:profile.checksum_offset])
    buf[profile.checksum_offset] = _xor_checksum(body)
    notes.append(f"XOR checksum recomputed → 0x{buf[profile.checksum_offset]:02X}.")

    return CloudRebuildResult(
        rebuilt_bytes=bytes(buf),
        template_version=template_version,
        vin_used=vin,
        fa_carried_over=fa_carried_over,
        notes=notes,
    )
