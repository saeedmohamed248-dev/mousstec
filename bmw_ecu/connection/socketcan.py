"""SocketCAN transport for Linux. Thin wrapper around python-can + isotp."""
from __future__ import annotations

import asyncio
from typing import Optional

from ..exceptions import ConnectionError_, TransportTimeout
from ..logging_setup import get_logger
from .base import AbstractTransport, TransportConfig, TransportKind

log = get_logger(__name__)


class SocketCANTransport(AbstractTransport):
    kind = TransportKind.SOCKETCAN

    def __init__(self, config: TransportConfig) -> None:
        super().__init__(config)
        if config.channel is None:
            raise ValueError("SocketCANTransport requires config.channel (e.g. 'can0')")
        self._bus: Optional[object] = None
        self._stack: Optional[object] = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        try:
            import can  # type: ignore
            import isotp  # type: ignore
        except ImportError as e:
            raise ConnectionError_("python-can + isotp not installed") from e
        loop = asyncio.get_running_loop()

        def _connect() -> tuple[object, object]:
            bus = can.interface.Bus(channel=self.config.channel,
                                    bustype="socketcan", bitrate=self.config.bitrate)
            addr = isotp.Address(
                isotp.AddressingMode.Normal_11bits,
                txid=self.config.source_addr, rxid=self.config.target_addr,
            )
            stack = isotp.CanStack(bus=bus, address=addr)
            return bus, stack

        try:
            self._bus, self._stack = await loop.run_in_executor(None, _connect)
            self._connected = True
            log.info("SocketCAN connected", extra={"channel": self.config.channel})
        except Exception as e:
            raise ConnectionError_(f"SocketCAN open failed: {e}") from e

    async def close(self) -> None:
        if self._bus is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, getattr(self._bus, "shutdown", lambda: None))
        self._bus = None
        self._stack = None
        self._connected = False

    async def send(self, target_addr: int, payload: bytes) -> None:
        if self._stack is None:
            raise ConnectionError_("SocketCAN not open")
        async with self._lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: (self._stack.send(payload), self._stack.process()),  # type: ignore[union-attr]
            )

    async def recv(self, timeout: Optional[float] = None) -> bytes:
        if self._stack is None:
            raise ConnectionError_("SocketCAN not open")
        to = timeout if timeout is not None else self.config.timeout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + to
        while loop.time() < deadline:
            await loop.run_in_executor(None, self._stack.process)  # type: ignore[union-attr]
            data = await loop.run_in_executor(None, self._stack.recv)  # type: ignore[union-attr]
            if data is not None:
                return bytes(data)
            await asyncio.sleep(0.01)
        raise TransportTimeout(f"SocketCAN recv timeout {to}s")
