"""K+DCAN serial transport (E-series legacy interface).

Wraps `pyserial` + ISO-TP framing. We don't ship a full ISO-TP stack here;
production deployments should use `python-can` + `isotp` package. This module
is the integration seam — keep `_frame_isotp` / `_unframe_isotp` minimal and
testable.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from ..exceptions import ConnectionError_, TransportTimeout
from ..logging_setup import get_logger
from .base import AbstractTransport, TransportConfig, TransportKind

log = get_logger(__name__)


class KDCANTransport(AbstractTransport):
    kind = TransportKind.KDCAN

    def __init__(self, config: TransportConfig) -> None:
        super().__init__(config)
        if config.serial_port is None:
            raise ValueError("KDCANTransport requires config.serial_port")
        self._serial: Optional[object] = None  # serial.Serial
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        try:
            import serial  # type: ignore  # pyserial
        except ImportError as e:
            raise ConnectionError_("pyserial not installed") from e
        loop = asyncio.get_running_loop()
        try:
            self._serial = await loop.run_in_executor(
                None,
                lambda: serial.Serial(self.config.serial_port, baudrate=115200,
                                      timeout=self.config.timeout),
            )
            self._connected = True
            log.info("K+DCAN connected", extra={"port": self.config.serial_port})
        except Exception as e:
            raise ConnectionError_(f"K+DCAN open failed: {e}") from e

    async def close(self) -> None:
        if self._serial is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._serial.close)  # type: ignore[union-attr]
        self._serial = None
        self._connected = False

    async def send(self, target_addr: int, payload: bytes) -> None:
        if self._serial is None:
            raise ConnectionError_("K+DCAN not open")
        frame = self._frame_isotp(target_addr, payload)
        async with self._lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self._serial.write(frame))  # type: ignore[union-attr]

    async def recv(self, timeout: Optional[float] = None) -> bytes:
        if self._serial is None:
            raise ConnectionError_("K+DCAN not open")
        loop = asyncio.get_running_loop()
        to = timeout if timeout is not None else self.config.timeout
        try:
            raw = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: self._serial.read(4095)),  # type: ignore[union-attr]
                timeout=to,
            )
        except asyncio.TimeoutError as e:
            raise TransportTimeout(f"K+DCAN recv timeout {to}s") from e
        if not raw:
            raise TransportTimeout("K+DCAN empty read")
        return self._unframe_isotp(raw)

    # --- ISO-TP framing (minimal — single-frame only).
    # Production code MUST swap this for the `isotp` package which handles
    # multi-frame, flow-control, and STmin properly.
    @staticmethod
    def _frame_isotp(target_addr: int, payload: bytes) -> bytes:
        if len(payload) > 7:
            raise NotImplementedError(
                "Multi-frame ISO-TP not implemented — use python-can + isotp package."
            )
        return bytes([target_addr >> 8, target_addr & 0xFF, len(payload)]) + payload

    @staticmethod
    def _unframe_isotp(raw: bytes) -> bytes:
        # Skip 2-byte address header, 1-byte length, return payload.
        if len(raw) < 4:
            raise ConnectionError_("K+DCAN frame too short")
        length = raw[2]
        return raw[3:3 + length]
