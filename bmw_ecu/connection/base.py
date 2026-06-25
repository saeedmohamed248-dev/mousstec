"""Abstract transport interface.

Every concrete transport (DoIP, K+DCAN, SocketCAN) implements this.
The UDS client only ever sees `AbstractTransport`, never a vendor-specific API.
"""
from __future__ import annotations

import abc
import enum
from dataclasses import dataclass
from typing import Optional


class TransportKind(str, enum.Enum):
    DOIP = "doip"          # ENET cable (TCP/IP to OBD gateway)
    KDCAN = "kdcan"        # K+DCAN USB serial (E-series legacy)
    SOCKETCAN = "socketcan"  # Linux native CAN socket


@dataclass(frozen=True)
class TransportConfig:
    kind: TransportKind
    # DoIP
    host: Optional[str] = None
    port: int = 13400
    # K+DCAN / SocketCAN
    serial_port: Optional[str] = None
    channel: Optional[str] = None
    bitrate: int = 500_000
    # Common
    source_addr: int = 0x0EF1   # tester (BMW convention)
    target_addr: int = 0x10     # broadcast functional addressing
    timeout: float = 5.0


class AbstractTransport(abc.ABC):
    """One open connection to one OBD interface.

    Implementations MUST be safe to use under asyncio — blocking I/O should
    be pushed to a thread via `asyncio.to_thread` or use an async-native
    library (e.g. asyncio sockets for DoIP).
    """

    kind: TransportKind

    def __init__(self, config: TransportConfig) -> None:
        self.config = config
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @abc.abstractmethod
    async def open(self) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...

    @abc.abstractmethod
    async def send(self, target_addr: int, payload: bytes) -> None: ...

    @abc.abstractmethod
    async def recv(self, timeout: Optional[float] = None) -> bytes: ...

    async def request(self, target_addr: int, payload: bytes,
                      timeout: Optional[float] = None) -> bytes:
        """Send a request and await one response. Convenience wrapper."""
        await self.send(target_addr, payload)
        return await self.recv(timeout=timeout)

    async def __aenter__(self) -> "AbstractTransport":
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
