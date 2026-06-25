"""Pin-glitching sequences to land the ECU in bootloader.

The classic recipe: power up with the BOOT pin grounded, settle, then
release. Implemented as a small DSL of (rail, pin, hold_ms) steps so
each ECU's profile is data, not code.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ...logging_setup import get_logger
from .hal import PinRail, SmartBoxHAL
from .pin_maps import PinMap

log = get_logger(__name__)


@dataclass(frozen=True)
class GlitchStep:
    pin: int
    rail: PinRail
    hold_ms: int


def standard_bsl_sequence(pm: PinMap) -> list[GlitchStep]:
    """Standard 'ground BOOT, then power up' sequence used by most BMW ECUs."""
    return [
        GlitchStep(pm.gnd, PinRail.GND, 50),       # ground first
        GlitchStep(pm.boot, PinRail.GND, 50),      # boot pin low
        GlitchStep(pm.v12, PinRail.V12, 300),      # power on, hold 300ms
        GlitchStep(pm.boot, PinRail.OFF, 50),      # release boot
    ]


async def run_sequence(hal: SmartBoxHAL, steps: list[GlitchStep]) -> None:
    log.info("Glitch sequence start", extra={"steps": len(steps)})
    try:
        for step in steps:
            await hal.set_pin(step.pin, step.rail)
            await asyncio.sleep(step.hold_ms / 1000.0)
    except Exception:
        await hal.all_off()
        raise
    log.info("Glitch sequence complete")
