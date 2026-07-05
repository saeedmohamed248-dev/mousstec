"""Best-effort engine-code extraction from a parsed FA (VehicleOrder).

The engine a car was built with is encoded in the FA's *type key*, not as a
plain SALAPA option — and the type-key → engine table is BMW-internal and
must be VERIFIED before we trust it. So this module deliberately does NOT
guess: it returns an engine code only when it is genuinely derivable:

    1. an explicit engine token present in the FA text (e.g. a workshop dump
       that literally carries "N18"), matched conservatively; or
    2. a type_code registered in the verified `TYPE_CODE_ENGINE` catalog.

Otherwise it returns "" (unknown) and the caller shows the raw FA for the
technician to confirm — never a fabricated N14/N18. `register_type_engine()`
lets the verified catalog grow from Django admin / a data migration without
touching call sites.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .fa_vo import VehicleOrder

# A standalone BMW engine-family token: one letter (N/B/M/S) + exactly two
# digits, on word boundaries so SALAPA codes ("S322A", "205A") never match.
_ENGINE_TOKEN = re.compile(r"\b([NBMS]\d{2})\b")

# Verified type_code → engine catalog. Intentionally EMPTY by default — we
# only add entries we have confirmed, and register the rest at runtime. Never
# seed this from memory with unverified mappings.
TYPE_CODE_ENGINE: dict[str, str] = {}


def register_type_engine(type_code: str, engine: str) -> None:
    """Register a VERIFIED type_code → engine mapping (admin / migration)."""
    tc = (type_code or "").strip().upper()
    if tc:
        TYPE_CODE_ENGINE[tc] = (engine or "").strip().upper()


def engine_from_vo(vo: "VehicleOrder") -> str:
    """Return the engine code the FA implies, or "" if not derivable.

    Order: verified type_code catalog first (authoritative), then an explicit
    engine token in the raw FA text. Empty string means "unknown — ask the
    technician", never a guess.
    """
    tc = (getattr(vo, "type_code", "") or "").strip().upper()
    if tc and tc in TYPE_CODE_ENGINE:
        return TYPE_CODE_ENGINE[tc]

    raw = getattr(vo, "raw", "") or ""
    m = _ENGINE_TOKEN.search(raw.upper())
    if m:
        return m.group(1)
    return ""
