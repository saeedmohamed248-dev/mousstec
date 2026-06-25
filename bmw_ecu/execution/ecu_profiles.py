"""ECU profiles — what we know about each chip family that drives strategy selection."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class ProtectionLevel(enum.IntEnum):
    """How hard the ECU's ISN protection is to defeat.

    Drives which strategies are eligible:
        OPEN/LOW  → SoftwareOnly is usually enough.
        MEDIUM    → SoftwareOnly with known exploit, else Hardware.
        HIGH      → Hardware required.
        CRITICAL  → Interactive (technician + pinout) only safe path.
    """
    OPEN = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


@dataclass(frozen=True)
class EcuProfile:
    """Identification + capability hints for one ECU family."""
    name: str                                # "MEVD17_2_9", "FEM_F30"
    chassis: tuple[str, ...]                 # ("F30", "F10", "F25")
    engine: str = ""                         # "N20", "" for body ECUs
    chip: str = ""                           # "Tricore TC1797", "MAC7242"
    protection: ProtectionLevel = ProtectionLevel.HIGH
    uds_isn_did: int = 0xF1A0
    boot_pin: int | None = None              # Connector pin number to ground for BSL
    known_software_exploit_ids: tuple[str, ...] = field(default_factory=tuple)
    requires_bench: bool = False             # True → SoftwareOnly will not be offered

    def supports_software_only(self) -> bool:
        return (
            not self.requires_bench
            and self.protection <= ProtectionLevel.MEDIUM
            and bool(self.known_software_exploit_ids)
        )


# ---------------------------------------------------------------------------
# Concrete profiles. Extend as more ECUs are reverse-engineered.
# ---------------------------------------------------------------------------
KNOWN_PROFILES: dict[str, EcuProfile] = {
    "MEVD17_2_9": EcuProfile(
        name="MEVD17_2_9",
        chassis=("F30", "F10", "F25", "F20", "F22"),
        engine="N20",
        chip="Tricore TC1797",
        protection=ProtectionLevel.HIGH,
        uds_isn_did=0xF1A0,
        boot_pin=24,
        known_software_exploit_ids=(),       # ISN write needs BDM on production fw
        requires_bench=True,
    ),
    "FEM_F30": EcuProfile(
        name="FEM_F30",
        chassis=("F30", "F32", "F36", "F20", "F22"),
        chip="MAC7242",
        protection=ProtectionLevel.MEDIUM,
        uds_isn_did=0xF1A0,
        boot_pin=18,
        known_software_exploit_ids=("FEM_PRE_2014_PRGSESS_BYPASS",),
        requires_bench=False,
    ),
    "FEM_F30_POST_2014": EcuProfile(
        name="FEM_F30_POST_2014",
        chassis=("F30", "F32", "F36"),
        chip="MAC7242",
        protection=ProtectionLevel.HIGH,
        uds_isn_did=0xF1A0,
        boot_pin=18,
        known_software_exploit_ids=(),
        requires_bench=False,                # bench-optional; tech-guided viable
    ),
}


def lookup(profile_name: str) -> EcuProfile:
    if profile_name not in KNOWN_PROFILES:
        raise KeyError(f"Unknown ECU profile: {profile_name}")
    return KNOWN_PROFILES[profile_name]
