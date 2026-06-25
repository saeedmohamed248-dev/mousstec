"""DoIP (ISO 13400) transport over ENET cable.

Real implementations should use `doipclient` (pip). We keep the interface
async-first and isolate the third-party call behind a thin shim so the
unit tests can substitute a `MockDoIPTransport` (see `bmw_ecu.mocks`).
"""
from __future__ import annotations

import asyncio
from typing import Optional

from ..exceptions import ConnectionError_, TransportTimeout
from ..logging_setup import get_logger
from .base import AbstractTransport, TransportConfig, TransportKind

log = get_logger(__name__)


class DoIPTransport(AbstractTransport):
    """ENET / DoIP transport.

    Lazy-imports `doipclient` so the rest of the subsystem works in CI
    without the optional dependency installed.
    """

    kind = TransportKind.DOIP

    def __init__(self, config: TransportConfig) -> None:
        super().__init__(config)
        if config.host is None:
            raise ValueError("DoIPTransport requires config.host")
        self._client: Optional[object] = None  # doipclient.DoIPClient
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        try:
            from doipclient import DoIPClient  # type: ignore
        except ImportError as e:
            raise ConnectionError_(
                "doipclient not installed. `pip install doipclient`",
                cause=str(e),
            ) from e

        loop = asyncio.get_running_loop()

        def _connect() -> object:
            return DoIPClient(
                ecu_ip_address=self.config.host,
                ecu_logical_address=self.config.target_addr,
                tcp_port=self.config.port,
                client_logical_address=self.config.source_addr,
            )

        try:
            self._client = await loop.run_in_executor(None, _connect)
            self._connected = True
            log.info("DoIP connected", extra={"host": self.config.host})
        except Exception as e:
            raise ConnectionError_(f"DoIP open failed: {e}") from e

    async def close(self) -> None:
        if self._client is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, getattr(self._client, "close", lambda: None))
        self._client = None
        self._connected = False

    async def send(self, target_addr: int, payload: bytes) -> None:
        if self._client is None:
            raise ConnectionError_("DoIP not open")
        async with self._lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: self._client.send_diagnostic(payload),  # type: ignore[union-attr]
            )

    async def recv(self, timeout: Optional[float] = None) -> bytes:
        if self._client is None:
            raise ConnectionError_("DoIP not open")
        loop = asyncio.get_running_loop()
        to = timeout if timeout is not None else self.config.timeout
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: self._client.receive_diagnostic(timeout=to),  # type: ignore[union-attr]
                ),
                timeout=to + 1.0,
            )
        except asyncio.TimeoutError as e:
            raise TransportTimeout(f"DoIP recv timeout {to}s") from e
