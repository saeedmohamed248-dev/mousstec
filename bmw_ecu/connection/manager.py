"""ConnectionManager — auto-detect and own the active transport.

Discovery order (matches what BMW techs actually do in the shop):
    1. ENET / DoIP at 169.254.x.x or user-specified host (fastest, F/G series)
    2. SocketCAN if `can0` is up (workshop bench setup)
    3. K+DCAN serial fallback (E-series, USB)
"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from ..exceptions import NoInterfaceDetected
from ..logging_setup import get_logger
from .base import AbstractTransport, TransportConfig, TransportKind
from .doip import DoIPTransport
from .kdcan import KDCANTransport
from .kline import KLineTransport
from .socketcan import SocketCANTransport

log = get_logger(__name__)


class ConnectionManager:
    """Owns the active transport for the lifetime of one ECU session."""

    def __init__(self) -> None:
        self._active: Optional[AbstractTransport] = None

    @property
    def transport(self) -> AbstractTransport:
        if self._active is None or not self._active.is_connected:
            raise NoInterfaceDetected("No active transport — call connect() first")
        return self._active

    async def connect(self, prefer: Optional[TransportConfig] = None) -> AbstractTransport:
        """Open the first interface that responds. `prefer` short-circuits detection."""
        if prefer is not None:
            self._active = self._build(prefer)
            await self._active.open()
            return self._active

        for cfg in self._detection_candidates():
            try:
                t = self._build(cfg)
                await asyncio.wait_for(t.open(), timeout=3.0)
                log.info("Auto-detected transport", extra={"kind": t.kind.value})
                self._active = t
                return t
            except Exception as e:
                log.info("Probe failed", extra={"kind": cfg.kind.value, "err": str(e)})
                continue

        raise NoInterfaceDetected("No DoIP / SocketCAN / K+DCAN interface responding")

    async def disconnect(self) -> None:
        if self._active is not None:
            await self._active.close()
            self._active = None

    # --- Internals ---------------------------------------------------------
    @staticmethod
    def _build(cfg: TransportConfig) -> AbstractTransport:
        if cfg.kind is TransportKind.DOIP:
            return DoIPTransport(cfg)
        if cfg.kind is TransportKind.SOCKETCAN:
            return SocketCANTransport(cfg)
        if cfg.kind is TransportKind.KDCAN:
            return KDCANTransport(cfg)
        if cfg.kind is TransportKind.KLINE:
            return KLineTransport(cfg)
        raise ValueError(f"Unknown transport kind: {cfg.kind}")

    @staticmethod
    def _detection_candidates() -> list[TransportConfig]:
        candidates: list[TransportConfig] = []

        # DoIP — try the standard BMW ENET link-local first.
        candidates.append(TransportConfig(
            kind=TransportKind.DOIP,
            host=os.environ.get("BMW_ECU_DOIP_HOST", "169.254.255.0"),
            port=13400,
        ))

        # SocketCAN — only if can0 looks present (Linux only).
        if os.path.exists("/sys/class/net/can0"):
            candidates.append(TransportConfig(
                kind=TransportKind.SOCKETCAN, channel="can0",
            ))

        # K+DCAN / D-CAN over a python-can serial adapter (CANable/slcan).
        # Env-driven, since the serial port name and per-ECU CAN IDs are
        # site-specific. We require explicit CAN IDs — never guessed.
        kdcan_port = os.environ.get("BMW_ECU_KDCAN_PORT")
        tx_env = os.environ.get("BMW_ECU_CAN_TX_ID")
        rx_env = os.environ.get("BMW_ECU_CAN_RX_ID")
        if kdcan_port and tx_env and rx_env:
            candidates.append(TransportConfig(
                kind=TransportKind.KDCAN,
                serial_port=kdcan_port,
                can_interface=os.environ.get("BMW_ECU_CAN_INTERFACE", "slcan"),
                can_tx_id=int(tx_env, 0),
                can_rx_id=int(rx_env, 0),
                bitrate=int(os.environ.get("BMW_ECU_CAN_BITRATE", "500000"), 0),
            ))

        # K-Line / KWP2000 over the FTDI serial line (pre-2007 E-series
        # gateway on pin 7). Env-driven: the serial port and the gateway's
        # KWP target address are site-specific and the target is required —
        # we never guess KWP addresses.
        kline_port = os.environ.get("BMW_ECU_KLINE_PORT")
        kline_target = os.environ.get("BMW_ECU_KLINE_TARGET")
        if kline_port and kline_target:
            candidates.append(TransportConfig(
                kind=TransportKind.KLINE,
                serial_port=kline_port,
                kline_target_addr=int(kline_target, 0),
                kline_source_addr=int(
                    os.environ.get("BMW_ECU_KLINE_SOURCE", "0xF1"), 0),
                kline_baudrate=int(
                    os.environ.get("BMW_ECU_KLINE_BAUD", "10400"), 0),
            ))

        return candidates
