"""D-CAN through the FTDI "K+DCAN" cable — BMW-FAST (KWP2000) over pyserial.

WHY THIS EXISTS
    ``kdcan.py`` needs a python-can adapter (CANable/slcan) and ``kline.py``
    only reaches pre-03/2007 gateways on pin 7. Every E-series / R5x MINI built
    after 03/2007 (e.g. a 2010 MINI R56) speaks **D-CAN** on pins 6/14 — and the
    common blue/white FTDI "K+DCAN" cable *with the K-Line/D-CAN switch* drives
    that bus by itself: the micro-controller inside the cable takes BMW-FAST
    (KWP2000-framed) telegrams on its serial side at 115200 baud and converts
    them into ISO-TP frames on D-CAN, then converts the ECU's reply back. This
    is the same path EDIABAS/INPA/NCS use with that cable, so no extra hardware
    is needed — the PC only builds the KWP frame:

        [0x80|len] [ECU addr] [0xF1] [data...] [checksum]

    and the cable does the CAN addressing (tester 0x6F1 + target byte).

HARDWARE NOTE — read before plugging in:
    • Put the cable switch on **D-CAN** (pins 7 and 8 NOT bridged). In K-Line
      position the cable never reaches the D-CAN bus and nothing answers.
    • Ignition ON (or engine running) — most body ECUs sleep otherwise.
    • Unlike K-Line there is no fast-init and no StartCommunication: the first
      request goes straight to the ECU.
    • Some cable firmwares echo the request back on RX, others don't. We don't
      guess: any frame byte-identical to what we just sent is dropped as an
      echo, and only a frame addressed tester ← ECU is accepted as the reply.

The ECU address comes from the UDS client (``ecu_addr``) on every ``send`` —
one open cable talks to every module on the bus (FRM, CAS, DME, ...).
"""
from __future__ import annotations

import asyncio
from typing import Optional

from ..exceptions import ConnectionError_, TransportTimeout
from ..logging_setup import get_logger
from .base import AbstractTransport, TransportConfig, TransportKind
from .kline import KLineTransport, decode_kwp, encode_kwp

log = get_logger(__name__)

# How many stray frames (echo / another ECU / late reply) we skip while waiting
# for the reply to OUR request before giving up.
_MAX_SKIPPED_FRAMES = 8


def kwp_header(frame: bytes) -> tuple[int, int]:
    """Return (target, source) of a raw KWP frame."""
    if len(frame) < 3:
        raise ValueError(f"KWP frame too short: {frame.hex()}")
    return frame[1], frame[2]


class DCanCableTransport(AbstractTransport):
    """BMW-FAST telegrams over the FTDI K+DCAN cable in D-CAN mode."""

    kind = TransportKind.DCAN_CABLE

    def __init__(self, config: TransportConfig) -> None:
        super().__init__(config)
        if config.serial_port is None:
            raise ValueError(
                "DCanCableTransport requires config.serial_port (e.g. "
                "'/dev/cu.usbserial-A50285BI' or 'COM3' for the K+DCAN cable).")
        self._ser: Optional[object] = None  # serial.Serial
        self._lock = asyncio.Lock()
        self._last_frame: bytes = b""
        self._last_target: Optional[int] = None

    @property
    def _source(self) -> int:
        return self.config.kline_source_addr or 0xF1

    async def open(self) -> None:
        try:
            import serial  # type: ignore  # pyserial
        except ImportError as e:
            raise ConnectionError_(
                "pyserial not installed. Run: pip install pyserial") from e

        loop = asyncio.get_running_loop()

        def _connect() -> object:
            ser = serial.Serial(
                port=self.config.serial_port,
                baudrate=self.config.dcan_cable_baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.config.timeout,
                write_timeout=self.config.timeout,
            )
            ser.reset_input_buffer()
            return ser

        try:
            self._ser = await loop.run_in_executor(None, _connect)
            self._connected = True
            log.info("D-CAN cable connected", extra={
                "port": self.config.serial_port,
                "baud": self.config.dcan_cable_baudrate,
            })
        except Exception as e:
            raise ConnectionError_(f"D-CAN cable open failed: {e}") from e

    async def close(self) -> None:
        if self._ser is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, getattr(self._ser, "close", lambda: None))
        self._ser = None
        self._connected = False

    async def send(self, target_addr: int, payload: bytes) -> None:
        """Frame `payload` as a BMW-FAST telegram to `target_addr` and write it."""
        if self._ser is None:
            raise ConnectionError_("D-CAN cable not open")
        target = target_addr & 0xFF
        frame = encode_kwp(target, self._source, payload)
        async with self._lock:
            loop = asyncio.get_running_loop()

            def _write() -> None:
                self._ser.reset_input_buffer()  # type: ignore[union-attr]
                self._ser.write(frame)          # type: ignore[union-attr]
                self._ser.flush()               # type: ignore[union-attr]

            await loop.run_in_executor(None, _write)
            self._last_frame = frame
            self._last_target = target

    async def recv(self, timeout: Optional[float] = None) -> bytes:
        """Read until the reply to the last request arrives; return its data.

        Skips (a) the cable's echo of our own request and (b) any frame that
        isn't addressed tester ← the ECU we just asked.
        """
        if self._ser is None:
            raise ConnectionError_("D-CAN cable not open")
        to = timeout if timeout is not None else self.config.timeout
        loop = asyncio.get_running_loop()
        source = self._source
        expected_ecu = self._last_target
        sent = self._last_frame

        def _recv() -> bytes:
            prev = self._ser.timeout  # type: ignore[union-attr]
            self._ser.timeout = to    # type: ignore[union-attr]
            try:
                for _ in range(_MAX_SKIPPED_FRAMES + 1):
                    frame = KLineTransport._read_frame(self._ser)
                    if sent and frame == sent:
                        continue  # our own request echoed back by the cable
                    tgt, src = kwp_header(frame)
                    if tgt != source or (expected_ecu is not None and src != expected_ecu):
                        log.debug(f"D-CAN: skipped foreign frame {frame.hex(' ')}")
                        continue
                    return decode_kwp(frame)
            finally:
                self._ser.timeout = prev  # type: ignore[union-attr]
            raise TransportTimeout(
                "D-CAN: no reply from the addressed ECU (only foreign frames)")

        return await loop.run_in_executor(None, _recv)
