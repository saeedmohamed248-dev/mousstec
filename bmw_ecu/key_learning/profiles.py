"""Per-module key-learning profiles.

Each profile bundles the constants the orchestrator needs to drive ONE
module family through the bench flow:

  • pinout      — which Smart-Harness pins map to power, ground, CAN-H,
                  CAN-L, BOOT (for bench-mode entry), and the EEPROM read
                  pads (CAS3 family) or the K-line tap (legacy CAS).
  • can_speed   — bus speed when running over CAN once bench mode is
                  established.
  • eeprom      — chip family + size + ISN window inside the dump.
  • key_count   — how many key slots the module physically supports.
  • uds_addrs   — ECU + tester logical addresses.
  • flow        — which read path the orchestrator should drive:
                  "eeprom"  → power → BOOT → dump → parse → ISN
                  "uds"     → power → bus init → SecurityAccess → UDS read

The catalog deliberately covers the modules the Mousstec ECU suite was
specced to support; new modules drop in by appending another entry.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


class ModuleFamily(str, enum.Enum):
    """Recognised BMW immobiliser / key-master modules."""
    CAS3 = "CAS3"      # E-series 2007-2010, 8-pin M35080 EEPROM
    CAS3_PLUS = "CAS3+"  # E-series 2010+, hardened bootloader
    FEM = "FEM"        # F-series footwell module (UDS bootloader)
    BDC = "BDC"        # G-series body domain controller (UDS bootloader)


class ReadFlow(str, enum.Enum):
    """Which path the orchestrator drives to retrieve the ISN."""
    EEPROM = "eeprom"   # solder + dump path (legacy CAS3 / CAS3+)
    UDS = "uds"         # bench-mode UDS bootloader (FEM / BDC)


# ─────────────────────────────────────────────────────────────────────
# Pinout — a labelled subset of the Smart-Harness 24-pin connector.
# Keys are MNEMONIC; the integers are the physical wire numbers the
# Mousstec Breakout Box exposes to the technician. The chatbot reads
# these straight from the dict so adding a new module never touches UI
# code.
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class HarnessPinout:
    power_12v: int
    ground:    int
    can_high:  Optional[int] = None
    can_low:   Optional[int] = None
    boot:      Optional[int] = None   # pulled to specific level to enter BSL
    eeprom_sda: Optional[int] = None  # I²C data (CAS3 M35080)
    eeprom_scl: Optional[int] = None  # I²C clock
    eeprom_wp:  Optional[int] = None  # write-protect (held high while reading)


@dataclass(frozen=True)
class KeyLearningProfile:
    family: ModuleFamily
    label: str                       # human label for the chatbot
    read_flow: ReadFlow
    pinout: HarnessPinout
    can_speed_kbps: int = 500        # 500 for F/G-series, 100 for K-line bridge
    eeprom_chip: Optional[str] = None  # e.g. "M35080" — used by parse_dump
    eeprom_size: int = 0             # bytes; 0 for non-EEPROM flows
    isn_offset: int = 0              # byte offset of the 32-byte ISN window
    isn_length: int = 32
    key_count: int = 4               # CAS3=4, CAS3+=8, FEM=10, BDC=10
    uds_ecu_addr: Optional[int] = None
    uds_tester_addr: Optional[int] = 0xF1
    security_level: int = 0x03       # CAS=03, FEM=05, BDC=05
    bench_voltage_v: float = 12.0
    voltage_tolerance_v: float = 0.5
    notes: tuple[str, ...] = field(default_factory=tuple)


# ─────────────────────────────────────────────────────────────────────
# Catalog — one entry per supported family. Source: Mousstec workshop
# manuals + commonly-cited BMW bench guides. Pins are normalised to the
# Mousstec Breakout Box numbering, NOT the OEM connector.
# ─────────────────────────────────────────────────────────────────────
KEY_LEARNING_PROFILES: dict[ModuleFamily, KeyLearningProfile] = {
    ModuleFamily.CAS3: KeyLearningProfile(
        family=ModuleFamily.CAS3,
        label="CAS3 (E-series 2007-2010)",
        read_flow=ReadFlow.EEPROM,
        pinout=HarnessPinout(
            power_12v=1, ground=2,
            eeprom_sda=14, eeprom_scl=15, eeprom_wp=16,
        ),
        eeprom_chip="M35080",
        eeprom_size=512,
        isn_offset=0x20,
        isn_length=32,
        key_count=4,
        bench_voltage_v=12.0,
        notes=(
            "Tape the WP pin HIGH while reading — releasing it can wear the EEPROM cell.",
            "Reading is non-destructive but ALWAYS save a backup before touching key slots.",
        ),
    ),
    ModuleFamily.CAS3_PLUS: KeyLearningProfile(
        family=ModuleFamily.CAS3_PLUS,
        label="CAS3+ (E-series 2010+)",
        read_flow=ReadFlow.EEPROM,
        pinout=HarnessPinout(
            power_12v=1, ground=2,
            eeprom_sda=14, eeprom_scl=15, eeprom_wp=16,
        ),
        eeprom_chip="M35128",
        eeprom_size=1024,
        isn_offset=0x40,
        isn_length=32,
        key_count=8,
        bench_voltage_v=12.0,
        notes=(
            "CAS3+ has a hardened bootloader — never attempt UDS write without bench power.",
            "Key slot 0 is dealer-reserved; only slots 1-7 are usable for aftermarket keys.",
        ),
    ),
    ModuleFamily.FEM: KeyLearningProfile(
        family=ModuleFamily.FEM,
        label="FEM (F-series 2011-2018)",
        read_flow=ReadFlow.UDS,
        pinout=HarnessPinout(
            power_12v=1, ground=2,
            can_high=8, can_low=9, boot=12,
        ),
        can_speed_kbps=500,
        eeprom_chip=None,
        eeprom_size=0,
        isn_length=32,
        key_count=10,
        uds_ecu_addr=0x40,
        security_level=0x05,
        bench_voltage_v=12.0,
        notes=(
            "Hold BOOT high during the first 200 ms of power-on to enter UDS bootloader.",
            "FEM rejects security access if the 12V rail dips below 11.0V mid-handshake.",
        ),
    ),
    ModuleFamily.BDC: KeyLearningProfile(
        family=ModuleFamily.BDC,
        label="BDC (G-series 2017+)",
        read_flow=ReadFlow.UDS,
        pinout=HarnessPinout(
            power_12v=1, ground=2,
            can_high=8, can_low=9, boot=12,
        ),
        can_speed_kbps=500,
        eeprom_chip=None,
        eeprom_size=0,
        isn_length=32,
        key_count=10,
        uds_ecu_addr=0x40,
        security_level=0x05,
        bench_voltage_v=12.0,
        notes=(
            "BDC adds rolling-code anti-replay on SecurityAccess — seed/key window is 5 s.",
            "After a successful key burn, ALWAYS run ATSP6 + RC routine 0xFF00 to commit.",
        ),
    ),
}


def get_profile(family: ModuleFamily | str) -> KeyLearningProfile:
    """Lookup a profile by enum or raw string. Raises KeyError on unknown
    families — callers should validate before calling."""
    if isinstance(family, str):
        try:
            family = ModuleFamily(family)
        except ValueError as e:
            raise KeyError(f"Unknown module family: {family!r}") from e
    if family not in KEY_LEARNING_PROFILES:
        raise KeyError(f"No profile registered for {family.value!r}")
    return KEY_LEARNING_PROFILES[family]
