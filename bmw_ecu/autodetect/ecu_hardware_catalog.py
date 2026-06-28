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


# ── Seed catalog — at least two N20 MEVD17.2.9 revisions ────────────────────
# Values marked verified=False are conservative placeholders to be confirmed
# per board on real hardware before production use.
_CATALOG: dict[str, HardwareProfile] = {}


def register_hardware_profile(profile: HardwareProfile) -> None:
    _CATALOG[profile.hardware_id] = profile


def _seed() -> None:
    register_hardware_profile(HardwareProfile(
        hardware_id="8606229",
        ecu_name="MEVD17.2.9",
        board_revision="Rev B (N20 pre-LCI)",
        family="MEVD17",
        protocol="BootMode",
        pinout=BenchPinout(
            power_pin=87, ground_pin=88, boot_pin=24,
            can_h_pin=None, can_l_pin=None, k_line_pin=63,
            pcb_image_url="/static/bmw_ecu/hw/mevd17_29_8606229_pcb.jpg",
            boot_image_url="/static/bmw_ecu/hw/mevd17_29_8606229_boot.jpg",
            callouts=[
                {"pin": 87, "label": "12V (KL30)", "color": "red"},
                {"pin": 88, "label": "GND (KL31)", "color": "black"},
                {"pin": 24, "label": "BOOT (ground on power-up)", "color": "yellow"},
                {"pin": 63, "label": "K-Line", "color": "green"},
            ],
        ),
        physical_steps_ar=[
            "البورده دي البوت بتاعها قُرب طرف الموصّل الكبير — بِن 24.",
        ],
        physical_steps_en=[
            "On this board the boot pad sits near the large connector edge — pin 24.",
        ],
        notes="N20 pre-LCI. Confirm boot pad before grounding.",
        verified=False,
    ))
    register_hardware_profile(HardwareProfile(
        hardware_id="8623136",
        ecu_name="MEVD17.2.9",
        board_revision="Rev D (N20 LCI)",
        family="MEVD17",
        protocol="BootMode",
        pinout=BenchPinout(
            power_pin=87, ground_pin=88, boot_pin=31,
            can_h_pin=None, can_l_pin=None, k_line_pin=63,
            pcb_image_url="/static/bmw_ecu/hw/mevd17_29_8623136_pcb.jpg",
            boot_image_url="/static/bmw_ecu/hw/mevd17_29_8623136_boot.jpg",
            callouts=[
                {"pin": 87, "label": "12V (KL30)", "color": "red"},
                {"pin": 88, "label": "GND (KL31)", "color": "black"},
                {"pin": 31, "label": "BOOT (ground on power-up)", "color": "yellow"},
                {"pin": 63, "label": "K-Line", "color": "green"},
            ],
        ),
        physical_steps_ar=[
            "نسخة الـ LCI: مكان البوت اتغيّر لـ بِن 31 — متبصّش على مخطط الـ Rev B.",
        ],
        physical_steps_en=[
            "LCI board: the boot point moved to pin 31 — do NOT use the Rev B map.",
        ],
        notes="N20 LCI. Boot pin differs from Rev B.",
        verified=False,
    ))


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
