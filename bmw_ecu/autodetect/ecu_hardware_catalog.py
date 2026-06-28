"""Dynamic Hardware Pinout Catalog (Phase 1).

Mouss Tec does NOT hardcode "the N20 image". Two cars both reporting
"MEVD17.2.9" can carry different PCB revisions with different boot-pin
positions and bench wiring. So the bench procedure is keyed on the ECU's
*Hardware ID* (the HWEL / part number we read live over OBD via UDS), not on
the marketing ECU name.

This module is the registry that maps a concrete Hardware ID → the exact,
board-revision-specific bench profile: power/ground/CAN pins, boot-pin
location, the PCB photo + boot-pin close-up image URLs, and any
variant-specific physical steps.

It's an in-memory registry by default (seeded below) and is overridable: a
private/cloud backend can register confirmed profiles for more revisions via
`register_hardware_profile(...)` without touching call sites. Lookup matches
an exact Hardware ID first, then a longest-prefix match (BMW part numbers
share family prefixes across minor revisions).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass(frozen=True)
class BenchPinout:
    """The physical bench connection map for one specific board revision."""
    power_pin: int                     # KL30 / +12V
    ground_pin: int                    # KL31 / GND
    boot_pin: Optional[int]            # ground (or probe) to enter BSL/BDM
    can_h_pin: Optional[int] = None
    can_l_pin: Optional[int] = None
    k_line_pin: Optional[int] = None
    pcb_image_url: str = ""            # full PCB photo for orientation
    boot_image_url: str = ""           # close-up of the boot pad/pin
    callouts: list[dict] = field(default_factory=list)  # diagram overlay

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HardwareProfile:
    """Everything needed to bench a specific ECU board revision."""
    hardware_id: str                   # the live HWEL / part number, e.g. "8606229"
    ecu_name: str                      # marketing name, e.g. "MEVD17.2.9"
    board_revision: str                # e.g. "Rev B (pre-2014)"
    family: str                        # "MEVD17", "FEM", ...
    protocol: str                      # "BDM" | "BootMode" | "JTAG"
    pinout: BenchPinout
    physical_steps_ar: list[str] = field(default_factory=list)
    physical_steps_en: list[str] = field(default_factory=list)
    notes: str = ""
    verified: bool = False             # True once confirmed on real hardware

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["pinout"] = self.pinout.to_json()
        return d


# ── Catalog — EMPTY BY DESIGN (safety rule: NO guessed pins) ────────────────
# We deliberately ship NO bundled pin/boot data. Bench pinouts are physical
# wiring maps where a wrong pin can destroy an ECU, so the catalog stays empty
# until a workshop enters *verified, real-world* values via the Django admin
# (EcuHardwareProfile → resolved here through `get_hardware_profile_db_first`).
#
# The previous seed carried `verified=False` placeholder pins (87/88/24/31/63
# for MEVD17.2.9 N20). Those were unconfirmed guesses and were removed so they
# can never be shown to a technician. Re-add data ONLY when confirmed on a real
# board — and only via the admin/DB, never hardcoded here.
_CATALOG: dict[str, HardwareProfile] = {}


def register_hardware_profile(profile: HardwareProfile) -> None:
    """Register a CONFIRMED board profile at runtime.

    Intended for a private/cloud backend that holds verified pinouts. The
    bundled catalog itself ships empty — never seed unverified pins here.
    """
    _CATALOG[profile.hardware_id] = profile


def _seed() -> None:
    """No-op: the bundled catalog ships empty (no guessed pins). See above."""
    return None


_seed()


def get_hardware_profile(hardware_id: str) -> Optional[HardwareProfile]:
    """Resolve the bench profile for a live Hardware ID.

    Exact match first, then longest-prefix match so minor sub-revisions that
    share a part-number prefix still resolve to the closest known board.
    """
    if not hardware_id:
        return None
    hid = hardware_id.strip()
    exact = _CATALOG.get(hid)
    if exact is not None:
        return exact
    best: Optional[HardwareProfile] = None
    for known_id, profile in _CATALOG.items():
        if hid.startswith(known_id) or known_id.startswith(hid):
            if best is None or len(known_id) > len(best.hardware_id):
                best = profile
    return best


def get_hardware_profile_db_first(hardware_id: str) -> Optional[HardwareProfile]:
    """DB-first resolve: a workshop-registered row (Django admin) wins over
    the bundled seed; otherwise fall back to the in-memory catalog.

    The DB hop is best-effort — if Django isn't configured or the table is
    absent it silently degrades to the bundled `get_hardware_profile`, so
    pure-unit callers and tenant-less contexts still work.
    """
    if not hardware_id:
        return None
    try:
        from .._db_hardware import fetch_hardware_profile
        row = fetch_hardware_profile(hardware_id.strip())
    except Exception:
        row = None
    if row is not None:
        return row
    return get_hardware_profile(hardware_id)


def all_hardware_ids() -> list[str]:
    return sorted(_CATALOG)
