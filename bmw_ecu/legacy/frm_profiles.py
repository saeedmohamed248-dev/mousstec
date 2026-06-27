"""FRM3 variant catalog.

E-chassis BMW + R5x MINI all use the same Freescale MC9S12XEP100, but
the D-Flash layout — VIN window, brightness LUT addresses, FA base
offset, checksum byte position — differs subtly per chassis. We model
those differences as profile rows so the orchestrator + analyzer don't
hard-code addresses.

Per-profile data
----------------
  bdm_clock_khz   : BKGD clock speed the BDM POD should use
  reset_low_ms    : how long to hold RESET low when entering BDM
  dflash_size     : usable bytes (the chip has 32 KB, but only a window
                    of it is the FRM data region; the rest is config)
  dflash_base     : start address of the D-Flash region in the linear
                    memory map exposed by BDM
  vin_offset      : where the 17-char ASCII VIN sits in the dump
  fa_offset       : where the encoded FA option block starts
  fa_length       : how many bytes the FA block spans
  checksum_offset : single-byte checksum location
  notes           : free-text guidance the chatbot pipes to the
                    technician as a "you should also..." note
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class FrmVariant(str, enum.Enum):
    """Recognised FRM3 variants. Add a row to FRM_PROFILES to onboard
    a new chassis without touching the orchestrator."""
    E90_FRM3 = "E90_FRM3"     # E90/E91/E92/E93 + E84 X1
    E70_FRM3 = "E70_FRM3"     # E70/E71/E72 X5/X6
    R56_FRM3 = "R56_FRM3"     # R56/R55/R57 MINI


@dataclass(frozen=True)
class FrmProfile:
    variant: FrmVariant
    label: str
    bdm_clock_khz: int = 800            # 0.8 MHz is the BKGD spec default
    reset_low_ms: int = 100             # hold RESET low → enter BDM cleanly
    dflash_size: int = 8192             # bytes — the working data window
    dflash_base: int = 0x10_0000        # linear-map base address
    vin_offset: int = 0x0040
    vin_length: int = 17
    fa_offset: int = 0x0200
    fa_length: int = 256                # 256-byte FA option block
    checksum_offset: int = 0x1FFF       # last byte of the window
    bench_voltage_v: float = 12.0
    voltage_tolerance_v: float = 0.3    # FRM3 is undervoltage-sensitive
    notes: tuple[str, ...] = field(default_factory=tuple)


FRM_PROFILES: dict[FrmVariant, FrmProfile] = {
    FrmVariant.E90_FRM3: FrmProfile(
        variant=FrmVariant.E90_FRM3,
        label="E90 / E91 / E92 / E93 / E84 — FRM3",
        bdm_clock_khz=800,
        reset_low_ms=100,
        dflash_size=8192,
        dflash_base=0x10_0000,
        vin_offset=0x0040,
        fa_offset=0x0200, fa_length=256,
        checksum_offset=0x1FFF,
        notes=(
            "FRM3 is sensitive to a 12 V dip below ~11.6 V mid-write. "
            "Run from a stable bench supply — never the car's battery "
            "after jump-starting.",
            "After the flash-back finishes, ALWAYS run UDS routine "
            "0x0203 to refresh the brightness LUT cache.",
        ),
    ),
    FrmVariant.E70_FRM3: FrmProfile(
        variant=FrmVariant.E70_FRM3,
        label="E70 / E71 — X5 / X6 FRM3",
        bdm_clock_khz=800,
        reset_low_ms=100,
        dflash_size=8192,
        dflash_base=0x10_0000,
        vin_offset=0x0040,
        fa_offset=0x0240, fa_length=256,  # E70 stores FA 64 bytes later
        checksum_offset=0x1FFF,
        notes=(
            "E70 family stores the FA block at offset 0x0240 instead of "
            "0x0200 — do NOT reuse an E90 template directly.",
        ),
    ),
    FrmVariant.R56_FRM3: FrmProfile(
        variant=FrmVariant.R56_FRM3,
        label="MINI R56 / R55 / R57 — FRM3-MINI",
        bdm_clock_khz=800,
        reset_low_ms=120,                # MINI variant is slightly slower to settle
        dflash_size=8192,
        dflash_base=0x10_0000,
        vin_offset=0x0040,
        fa_offset=0x0200, fa_length=192,  # MINI option block is smaller
        checksum_offset=0x1FFF,
        notes=(
            "MINI R56 FRM3 has a smaller 192-byte FA block (no S/C "
            "package codes). Don't pad the rebuild — the checksum "
            "covers exactly 192 bytes here.",
        ),
    ),
}


def get_frm_profile(variant: FrmVariant | str) -> FrmProfile:
    """Lookup a profile by enum value or raw string. Raises KeyError on
    unknown variants — callers should validate before calling."""
    if isinstance(variant, str):
        try:
            variant = FrmVariant(variant)
        except ValueError as e:
            raise KeyError(f"Unknown FRM variant: {variant!r}") from e
    if variant not in FRM_PROFILES:
        raise KeyError(f"No profile registered for {variant.value!r}")
    return FRM_PROFILES[variant]
