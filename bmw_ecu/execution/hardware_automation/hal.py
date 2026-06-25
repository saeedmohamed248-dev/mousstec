"""Hardware Abstraction Layer for any "Smart Breakout Box".

Pure interface. Two implementations ship:
    - MousstecBreakoutBox (real USB-Serial protocol)
    - FakeBreakoutBox (in-memory; used by tests + dry-runs)

A breakout box is any device that lets the software programmatically
switch 12V / GND / CAN_H / CAN_L to a specific pin on the ECU connector.
"""
from __future__ import annotations

import abc
import enum
from dataclasses import dataclass


class PinRail(str, enum.Enum):
    OFF = "off"
    V12 = "12v"
    GND = "gnd"
    CAN_H = "can_h"
    CAN_L = "can_l"
    PULL_UP_5V = "pull_up_5v"


@dataclass(frozen=True)
class PinAssignment:
    pin: int
    rail: PinRail


class SmartBoxHAL(abc.ABC):
    """One open connection to one breakout box."""

    @abc.abstractmethod
    async def open(self) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...

    @abc.abstractmethod
    async def set_pin(self, pin: int, rail: PinRail) -> None: ...

    @abc.abstractmethod
    async def read_voltage(self, pin: int) -> float: ...

    @abc.abstractmethod
    async def all_off(self) -> None:
        """Emergency: drop every rail. Called by rollback paths."""

    async def apply(self, assignments: list[PinAssignment]) -> None:
        """Apply a list of (pin, rail) tuples in order, no overlap atomically."""
        for a in assignments:
            await self.set_pin(a.pin, a.rail)

    async def __aenter__(self) -> "SmartBoxHAL":
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.all_off()
        await self.close()
