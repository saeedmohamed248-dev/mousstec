"""Per-family ISN access specification (#2).

The ISN is read differently per ECU family: a different DID, a different
Security Access level, and sometimes not over UDS at all (the N20 DME's ISN
normally comes off the bench via BDM, not a clean ReadDataByIdentifier).

This module makes that mapping EXPLICIT and overridable instead of hiding a
single hard-coded 0xF1A0 in the extractor. Each spec also carries a
`verified` flag: it is False until the DID/level has been confirmed against
real hardware for that FA/I-step. The extractor refuses to run an unverified
spec against a real car unless the caller explicitly opts in — so a guessed
DID can never silently read garbage and be treated as a real ISN.

No proprietary values ship here. The defaults are conservative placeholders;
a private backend (or per-FA config) should register the confirmed specs via
`register_isn_spec(...)`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class IsnAccessSpec:
    family: str
    did: int
    security_level: int
    length: int = 32
    over_uds: bool = True       # False → must be read on the bench (BDM/EEPROM)
    verified: bool = False      # True only when confirmed on real hardware
    notes: str = ""


# Conservative defaults — DO NOT trust the DID/level until verified per FA.
_ISN_MAP: dict[str, IsnAccessSpec] = {
    "FEM": IsnAccessSpec(
        family="FEM", did=0xF1A0, security_level=0x05, length=32,
        over_uds=True, verified=False,
        notes="FEM/BDC ISN — confirm DID + level per FA/I-step before real use.",
    ),
    "CAS": IsnAccessSpec(
        family="CAS", did=0xF1A0, security_level=0x03, length=32,
        over_uds=True, verified=False,
        notes="CAS3/CAS4 ISN — confirm per platform.",
    ),
    "MEVD17": IsnAccessSpec(
        family="MEVD17", did=0xF1A0, security_level=0x01, length=32,
        over_uds=False, verified=False,
        notes="N20 DME ISN is normally read on the bench (BDM), not via RDBI.",
    ),
    # Pairs with MockEcu (RDBI 0xF1A0, 32 bytes) for tests/simulator.
    "MOCK": IsnAccessSpec(
        family="MOCK", did=0xF1A0, security_level=0x01, length=32,
        over_uds=True, verified=True,
        notes="Simulator only.",
    ),
}


def register_isn_spec(spec: IsnAccessSpec) -> None:
    _ISN_MAP[spec.family.upper()] = spec


def get_isn_spec(family: str) -> Optional[IsnAccessSpec]:
    return _ISN_MAP.get((family or "").upper())


def isn_spec_for_profile(profile) -> IsnAccessSpec:
    """Best ISN spec for a profile: a registered family spec, else one derived
    from the profile's own fields (still marked unverified)."""
    spec = get_isn_spec(getattr(profile, "seed_key_family", ""))
    if spec is not None:
        return spec
    return IsnAccessSpec(
        family=getattr(profile, "seed_key_family", "") or "UNKNOWN",
        did=getattr(profile, "uds_isn_did", 0xF1A0),
        security_level=getattr(profile, "isn_security_level", 0x01),
        verified=False,
        notes="Derived from profile; not confirmed against hardware.",
    )
