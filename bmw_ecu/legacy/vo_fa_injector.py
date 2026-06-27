"""SALAPA / FA writeback after a D-Flash restore.

After flashing a clean blob back the FRM3 needs its option codes
re-asserted so the lights re-enable per the customer's spec (e.g. a
car with optional adaptive headlamps would have 5DA + 524 codes in the
FA, an M-Sport gets 6FL, etc.).

The SALAPA encoding
-------------------
SALAPA = "SAList AusstattungspAket" — a 3-char alphanumeric code
(digit + 2 alphanumerics, OR 3 digits). BMW's FA is a tuple of:
  • plant code (1 byte)
  • week/year (2 bytes)
  • model code (4 bytes)
  • SALAPA list (variable length, NUL-terminated)
  • E-Werk (development serial)

We only model the SALAPA list — the rest comes from the template
during rebuild — because everything else is chassis-static while
SALAPA is per-vehicle.

A SALAPA payload is a concatenated 3-byte ASCII triplet per code,
sorted lex, terminated by 0x00. Example:
  ["5DA", "6FL", "524"] → b"5246FL5DA\\x00"   (sorted!)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


class SalapaInjectionError(ValueError):
    """Raised on malformed SALAPA codes / oversized payload."""


_SALAPA_RE = re.compile(r"^[0-9A-Z]{3}$")


@dataclass(frozen=True)
class SalapaCode:
    code: str          # 3-char uppercase alphanumeric

    def __post_init__(self) -> None:
        if not _SALAPA_RE.match(self.code):
            raise SalapaInjectionError(
                f"SALAPA code {self.code!r} must be 3 chars [0-9A-Z]",
            )


@dataclass(frozen=True)
class FaPayload:
    """Decoded FA writeback shape — what UDS service 0x2E will burn."""
    codes: tuple[SalapaCode, ...]
    raw_bytes: bytes        # length-prefixed binary as the FRM3 expects it
    sorted_codes: tuple[str, ...]   # human-friendly preview of payload


def parse_salapa(raw: Iterable[str]) -> tuple[SalapaCode, ...]:
    """Validate + dedupe + return canonical-cased SALAPA codes."""
    seen: set[str] = set()
    out: list[SalapaCode] = []
    for r in raw:
        if r is None:
            continue
        code = str(r).strip().upper()
        if not code:
            continue
        if code in seen:
            continue
        seen.add(code)
        out.append(SalapaCode(code=code))   # __post_init__ validates
    return tuple(out)


def build_fa_payload(codes: Iterable[str], *, max_codes: int = 80,
                     max_bytes: int = 256) -> FaPayload:
    """Build the binary writeback the orchestrator hands to UDS-2E.

    Args:
      codes      : iterable of raw SALAPA strings (case-insensitive).
      max_codes  : sanity ceiling — BMW FRM3 FA blocks never go past 80
                   codes in practice; anything more is a typo, not a
                   real spec.
      max_bytes  : hard limit on the resulting binary blob (matches the
                   FA-block size in frm_profiles.py).
    """
    parsed = parse_salapa(codes)
    if not parsed:
        raise SalapaInjectionError("FA payload must contain ≥ 1 SALAPA code")
    if len(parsed) > max_codes:
        raise SalapaInjectionError(
            f"{len(parsed)} codes exceeds max_codes={max_codes}",
        )

    sorted_codes = tuple(sorted(c.code for c in parsed))
    payload = ("".join(sorted_codes) + "\x00").encode("ascii")
    if len(payload) > max_bytes:
        raise SalapaInjectionError(
            f"FA payload {len(payload)} bytes > max_bytes={max_bytes}",
        )

    return FaPayload(
        codes=parsed,
        raw_bytes=payload,
        sorted_codes=sorted_codes,
    )
