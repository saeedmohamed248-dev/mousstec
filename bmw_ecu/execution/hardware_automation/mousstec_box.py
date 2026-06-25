"""MousstecBreakoutBox — concrete HAL over USB-Serial.

Wire protocol (line-based ASCII, CRLF terminated):

    Host → Box                    Box → Host
    --------                      ----------
    PIN <n> <rail>\r\n            OK\r\n              # set pin
    READV <n>\r\n                 V <millivolts>\r\n  # read voltage
    ALLOFF\r\n                    OK\r\n              # emergency cut
    PING\r\n                      PONG\r\n            # liveness

Errors come back as `ERR <code> <message>`.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from ...exceptions import BmwEcuError
from ...logging_setup import get_logger
from .hal import PinRail, SmartBoxHAL

log = get_logger(__name__)


class SmartBoxError(BmwEcuError):
    code = "SMART_BOX_ERROR"


class MousstecBreakoutBox(SmartBoxHAL):
    def __init__(self, serial_port: str, *, baudrate: int = 921_600,
                 timeout_s: float = 2.0) -> None:
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.timeout_s = timeout_s
        self._serial: Optional[object] = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        try:
            import serial  # type: ignore  # pyserial
        except ImportError as e:
            raise SmartBoxError("pyserial not installed") from e
        loop = asyncio.get_running_loop()
        self._serial = await loop.run_in_executor(
            None,
            lambda: serial.Serial(self.serial_port, baudrate=self.baudrate,
                                  timeout=self.timeout_s),
        )
        # Verify liveness
        if await self._cmd("PING") != "PONG":
            raise SmartBoxError("Smart Box did not respond to PING")
        log.info("Mousstec Box connected", extra={"port": self.serial_port})

    async def close(self) -> None:
        if self._serial is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._serial.close)  # type: ignore[union-attr]
        self._serial = None

    async def set_pin(self, pin: int, rail: PinRail) -> None:
        resp = await self._cmd(f"PIN {pin} {rail.value}")
        if resp != "OK":
            raise SmartBoxError(f"PIN {pin}={rail.value} → {resp}")
        log.debug("set_pin", extra={"pin": pin, "rail": rail.value})

    async def read_voltage(self, pin: int) -> float:
        resp = await self._cmd(f"READV {pin}")
        if not resp.startswith("V "):
            raise SmartBoxError(f"READV {pin} → {resp}")
        mv = int(resp.split()[1])
        return mv / 1000.0

    async def all_off(self) -> None:
        try:
            resp = await self._cmd("ALLOFF")
            if resp != "OK":
                log.error("ALLOFF non-OK", extra={"resp": resp})
        except Exception as e:
            log.critical("ALLOFF FAILED — disconnect power manually", extra={"err": str(e)})
            raise

    async def _cmd(self, line: str) -> str:
        if self._serial is None:
            raise SmartBoxError("Smart Box not open")
        async with self._lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None,
                lambda: self._serial.write((line + "\r\n").encode("ascii")))  # type: ignore[union-attr]
            raw = await loop.run_in_executor(None,
                lambda: self._serial.readline())  # type: ignore[union-attr]
        resp = raw.decode("ascii", errors="replace").strip()
        if resp.startswith("ERR "):
            raise SmartBoxError(resp)
        return resp
