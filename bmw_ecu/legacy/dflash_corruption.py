"""Classify a raw FRM3 D-Flash dump as healthy, partially corrupted,
or beyond bench recovery.

Why this lives in its own module
--------------------------------
The corruption pattern is the *signature* the orchestrator uses to
decide:
  • whether to even attempt a cloud rebuild (some dumps are too far
    gone — the customer needs a new module);
  • how much of the dump to preserve (VIN/odometer survive even when
    the FA block is wrecked);
  • what to surface to the technician as a confidence score.

We expose ONE pure function — `analyze_dflash(raw, profile)` —
returning a structured CorruptionReport. The orchestrator and tests
both consume the same shape so a future refactor of the heuristics
can't desync the two.

The heuristic is intentionally conservative: a borderline dump gets
flagged PARTIAL (rebuild + verify), never SEVERE.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Iterable

from .frm_profiles import FrmProfile


class CorruptionLevel(str, enum.Enum):
    """Coarse-grained verdict the orchestrator branches on."""
    HEALTHY  = "healthy"   # nothing to do
    PARTIAL  = "partial"   # cloud rebuild recommended
    SEVERE   = "severe"    # cloud rebuild required (most fields gone)
    UNREADABLE = "unreadable"  # dump is uniform / wrong size — re-read


@dataclass(frozen=True)
class CorruptionReport:
    level: CorruptionLevel
    vin_recoverable: bool
    fa_recoverable: bool
    uniform_runs: int           # how many ≥256-byte 0xFF/0x00 runs
    checksum_ok: bool
    confidence: float           # 0.0 (re-read) → 1.0 (untouched bytes)
    notes: list[str] = field(default_factory=list)

    @property
    def needs_rebuild(self) -> bool:
        """True for anything except a clean HEALTHY dump."""
        return self.level != CorruptionLevel.HEALTHY


# ─────────────────────────────────────────────────────────────────────
def _count_uniform_runs(raw: bytes, *, minimum: int = 256,
                        values: Iterable[int] = (0x00, 0xFF)) -> int:
    """Count runs of length ≥ `minimum` filled with one of `values`."""
    runs = 0
    target_set = set(values)
    n = len(raw)
    i = 0
    while i < n:
        b = raw[i]
        if b not in target_set:
            i += 1
            continue
        j = i
        while j < n and raw[j] == b:
            j += 1
        if j - i >= minimum:
            runs += 1
        i = j
    return runs


def _looks_like_vin(window: bytes) -> bool:
    """Return True if the 17-byte window looks like a printable VIN.

    Real BMW VINs are 17 chars: digits + uppercase letters (no I, O, Q
    but we don't strictly enforce that here — only ASCII printable).
    """
    if len(window) != 17:
        return False
    return all(0x30 <= b <= 0x5A or b in (0x2D,) for b in window)


def _looks_like_fa(window: bytes) -> bool:
    """A healthy FA block has < 70% bytes uniform-FF.

    Production FRM3 dumps show the FA region as a dense mix of SALAPA
    triplets and binary fields. A near-virgin FA window is the
    archetypal corruption signature.
    """
    if not window:
        return False
    ff = sum(1 for b in window if b == 0xFF)
    return (ff / len(window)) < 0.70


def _xor_checksum(window: bytes) -> int:
    """The FRM3 checksum is a simple XOR over the whole D-Flash window
    EXCEPT the last byte, which holds the expected XOR value. We model
    this so the analyzer + rebuilder agree on what 'OK' means."""
    acc = 0
    for b in window:
        acc ^= b
    return acc


def analyze_dflash(raw: bytes, *, profile: FrmProfile) -> CorruptionReport:
    """Classify a raw dump. Pure function — no I/O, no state."""
    notes: list[str] = []

    # 1️⃣ Shape check first — wrong size means the BDM read was incomplete.
    if len(raw) != profile.dflash_size:
        return CorruptionReport(
            level=CorruptionLevel.UNREADABLE,
            vin_recoverable=False, fa_recoverable=False,
            uniform_runs=0, checksum_ok=False, confidence=0.0,
            notes=[
                f"dump length {len(raw)} ≠ expected {profile.dflash_size} — "
                f"BDM read was truncated. Re-seat the BDM pod and retry.",
            ],
        )

    # 2️⃣ Whole-window uniformity = unreadable.
    if all(b == 0xFF for b in raw) or all(b == 0x00 for b in raw):
        return CorruptionReport(
            level=CorruptionLevel.UNREADABLE,
            vin_recoverable=False, fa_recoverable=False,
            uniform_runs=1, checksum_ok=False, confidence=0.0,
            notes=[
                "Whole dump is uniform — the BDM read got NACK'd on every "
                "byte. The chip may be dead, or BKGD is shorted.",
            ],
        )

    # 3️⃣ VIN window.
    vin_window = raw[profile.vin_offset:profile.vin_offset + profile.vin_length]
    vin_ok = _looks_like_vin(vin_window)
    if vin_ok:
        notes.append(f"VIN visible at offset 0x{profile.vin_offset:04X}: "
                     f"{vin_window.decode('ascii', errors='replace')}")
    else:
        notes.append("VIN window is corrupted — cloud rebuild must supply "
                     "the VIN from the workshop's job sheet.")

    # 4️⃣ FA window.
    fa_window = raw[profile.fa_offset:profile.fa_offset + profile.fa_length]
    fa_ok = _looks_like_fa(fa_window)
    if not fa_ok:
        notes.append("FA option block is near-virgin — likely truncated by "
                     "undervoltage during a write cycle.")

    # 5️⃣ Uniform run count — surfaces the brown-out signature.
    uniform_runs = _count_uniform_runs(raw, minimum=256)
    if uniform_runs >= 4:
        notes.append(f"{uniform_runs} large uniform runs detected — classic "
                     f"undervoltage corruption.")

    # 6️⃣ Checksum check.
    expected = raw[profile.checksum_offset]
    body = raw[:profile.checksum_offset]
    computed = _xor_checksum(body)
    checksum_ok = (computed == expected)
    if checksum_ok:
        notes.append("XOR checksum matches — body bytes are internally consistent.")
    else:
        notes.append(
            f"XOR checksum mismatch (expected 0x{expected:02X}, computed "
            f"0x{computed:02X}) — at least one byte was flipped after the "
            f"last good write.",
        )

    # 7️⃣ Pick the verdict.
    if vin_ok and fa_ok and checksum_ok and uniform_runs == 0:
        level = CorruptionLevel.HEALTHY
        confidence = 1.0
    elif vin_ok and (fa_ok or uniform_runs <= 2) and uniform_runs <= 4:
        level = CorruptionLevel.PARTIAL
        confidence = 0.7 if uniform_runs <= 2 else 0.5
    elif vin_ok or uniform_runs <= 6:
        level = CorruptionLevel.SEVERE
        confidence = 0.3
    else:
        level = CorruptionLevel.UNREADABLE
        confidence = 0.0

    return CorruptionReport(
        level=level,
        vin_recoverable=vin_ok,
        fa_recoverable=fa_ok,
        uniform_runs=uniform_runs,
        checksum_ok=checksum_ok,
        confidence=confidence,
        notes=notes,
    )
