"""K+DCAN / D-CAN transport — REAL multi-frame ISO-TP over python-can.

This is a *production* D-CAN stack: it drives the OBD D-CAN bus through a
``python-can`` adapter and speaks full ISO-TP (15765-2) via the ``isotp``
(``can-isotp``) package, so the UDS client can exchange payloads of ANY size —
First-Frame + Consecutive-Frames + Flow-Control are handled by the isotp stack,
not by us. That is what makes ``WriteDataByIdentifier`` and flashing (which are
always > 7 bytes) actually work.

HARDWARE NOTE — read this before wiring a cable:
    ``python-can`` talks to *CAN-native* adapters. Use:
      • "slcan"     — USB serial CAN adapters (CANable / Lawicel), appear on
                      macOS as ``/dev/cu.usbmodem*``. This is the recommended,
                      no-Windows, no-EDIABAS path that works on a Mac.
      • "socketcan" — Linux native ``canX`` (handled by SocketCANTransport).
      • "pcan", "kvaser", ... — pro interfaces, also supported by python-can.

    The cheap blue/white **FTDI "K+DCAN"** cable is an EDIABAS/INPA interface,
    NOT a python-can adapter — there is no python-can backend for it, so it
    cannot be driven from here. Use a CANable/slcan adapter for the Python path.

The diagnostic CAN IDs (``can_tx_id`` / ``can_rx_id``) are per-ECU and must be
supplied by the caller (workshop / EcuHardwareProfile). We never guess CAN
arbitration IDs.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from ..exceptions import ConnectionError_, TransportTimeout
from ..logging_setup import get_logger
from .base import AbstractTransport, TransportConfig, TransportKind

log = get_logger(__name__)


class KDCANTransport(AbstractTransport):
    """D-CAN UDS transport: python-can bus + full ISO-TP (multi-frame)."""

    kind = TransportKind.KDCAN

    def __init__(self, config: TransportConfig) -> None:
        super().__init__(config)
        if config.serial_port is None and config.channel is None:
            raise ValueError(
                "KDCANTransport requires config.serial_port (e.g. "
                "'/dev/cu.usbmodem1411' for a CANable/slcan adapter) or "
                "config.channel."
            )
        if config.can_tx_id is None or config.can_rx_id is None:
            raise ValueError(
                "KDCANTransport requires explicit can_tx_id and can_rx_id "
                "(per-ECU ISO-TP diagnostic CAN IDs) — we never guess CAN "
                "arbitration IDs. Set them on the TransportConfig / profile."
            )
        self._bus: Optional[object] = None      # can.BusABC
        self._stack: Optional[object] = None     # isotp.CanStack
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        try:
            import can  # type: ignore  # python-can
            import isotp  # type: ignore  # can-isotp
        except ImportError as e:
            raise ConnectionError_(
                "python-can + can-isotp not installed. Run: "
                "pip install python-can can-isotp pyserial"
            ) from e

        loop = asyncio.get_running_loop()

        def _connect() -> tuple[object, object]:
            # `channel` for slcan is the serial device; for socketcan it's canX.
            channel = self.config.channel or self.config.serial_port
            bus = can.interface.Bus(
                channel=channel,
                bustype=self.config.can_interface,
                bitrate=self.config.bitrate,
            )
            mode = (isotp.AddressingMode.Normal_29bits
                    if self.config.can_extended_id
                    else isotp.AddressingMode.Normal_11bits)
            addr = isotp.Address(
                mode,
                txid=self.config.can_tx_id,
                rxid=self.config.can_rx_id,
            )
            # STmin/blocksize left to ECU's flow-control; padding on (BMW pads).
            params = {"tx_padding": 0x00, "blocking_send": False}
            stack = isotp.CanStack(bus=bus, address=addr, params=params)
            return bus, stack

        try:
            self._bus, self._stack = await loop.run_in_executor(None, _connect)
            self._connected = True
            log.info("D-CAN (K+DCAN) connected", extra={
                "interface": self.config.can_interface,
                "channel": self.config.channel or self.config.serial_port,
                "tx_id": hex(self.config.can_tx_id),
                "rx_id": hex(self.config.can_rx_id),
            })
        except Exception as e:
            raise ConnectionError_(f"K+DCAN open failed: {e}") from e

    async def close(self) -> None:
        if self._bus is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, getattr(self._bus, "shutdown", lambda: None))
        self._bus = None
        self._stack = None
        self._connected = False

    async def send(self, target_addr: int, payload: bytes) -> None:
        """Queue a full UDS payload; isotp segments it into CAN frames.

        `target_addr` is accepted for interface parity with DoIP but the actual
        addressing is the configured can_tx_id/can_rx_id pair (ISO-TP).
        """
        if self._stack is None:
            raise ConnectionError_("K+DCAN not open")
        async with self._lock:
            loop = asyncio.get_running_loop()

            def _send_and_pump() -> None:
                self._stack.send(payload)  # type: ignore[union-attr]
                # Pump the state machine until the send queue is drained so all
                # First/Consecutive frames go out (honours Flow-Control/STmin).
                while self._stack.transmitting():  # type: ignore[union-attr]
                    self._stack.process()           # type: ignore[union-attr]

            await loop.run_in_executor(None, _send_and_pump)

    async def recv(self, timeout: Optional[float] = None) -> bytes:
        """Reassemble one complete UDS payload from the ISO-TP stack."""
        if self._stack is None:
            raise ConnectionError_("K+DCAN not open")
        to = timeout if timeout is not None else self.config.timeout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + to
        while loop.time() < deadline:
            await loop.run_in_executor(None, self._stack.process)  # type: ignore[union-attr]
            data = await loop.run_in_executor(None, self._stack.recv)  # type: ignore[union-attr]
            if data is not None:
                return bytes(data)
            await asyncio.sleep(0.005)
        raise TransportTimeout(f"K+DCAN recv timeout {to}s")
