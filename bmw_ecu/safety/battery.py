"""Battery voltage monitor.

Reads voltage via UDS DID 0xF40C (BMW convention for battery voltage on
most F/G platform PT-CAN ECUs). Falls back to whatever the integrator
plugs into `voltage_source` for bench setups (lab power supply).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from ..exceptions import LowVoltage
from ..logging_setup import get_logger

log = get_logger(__name__)

# BMW spec: flashing requires >= 13.0 V (with charger). Below this the bus
# brownout risk is real and EEPROM writes can corrupt mid-cell.
MIN_FLASH_VOLTAGE = 13.0
MIN_DIAG_VOLTAGE = 11.5  # Read-only diag is safe down to ~11.5V

VoltageReader = Callable[[], Awaitable[float]]


@dataclass
class BatteryReading:
    volts: float
    source: str


class BatteryMonitor:
    """Pluggable voltage source. Default reads a UDS DID; bench setups
    can inject a lambda that reads a USB DMM or a constant value for tests.
    """

    def __init__(self, reader: VoltageReader, *, min_flash: float = MIN_FLASH_VOLTAGE,
                 min_diag: float = MIN_DIAG_VOLTAGE) -> None:
        self._reader = reader
        self.min_flash = min_flash
        self.min_diag = min_diag

    async def read(self) -> BatteryReading:
        volts = await self._reader()
        return BatteryReading(volts=volts, source="uds_did_F40C")

    async def assert_flash_safe(self) -> BatteryReading:
        r = await self.read()
        log.info("Battery check (flash)", extra={"volts": r.volts, "min": self.min_flash})
        if r.volts < self.min_flash:
            raise LowVoltage(
                f"{r.volts:.2f}V below flash minimum {self.min_flash}V",
                volts=r.volts, min_required=self.min_flash,
            )
        return r

    async def assert_diag_safe(self) -> BatteryReading:
        r = await self.read()
        if r.volts < self.min_diag:
            raise LowVoltage(f"{r.volts:.2f}V below diag minimum {self.min_diag}V")
        return r
