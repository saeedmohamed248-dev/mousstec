"""K-Line / KWP2000 transport — REAL serial KWP2000 (ISO 14230) over pyserial.

This is the ONE native-Python path the cheap blue/white **FTDI "K+DCAN"** cable
can actually drive. That cable is not a python-can adapter (see ``kdcan.py``),
but the FTDI chip *does* expose a real bidirectional serial port — and on
PRE-03/2007 E-series gateways (E60 / early E90 era) BMW used K-Line / KWP2000
(ISO 14230-2) for OBD diagnostics on **pin 7**. Such a gateway natively accepts
KWP2000 requests and bridges them onto PT-CAN for the DME and CAS. So with a
pre-2007 gateway as the bench bridge, this transport makes the FTDI cable a
working diagnostic interface with zero extra hardware.

HARDWARE NOTE — read before wiring a cable:
    • This works ONLY against a gateway/ECU that speaks K-Line on pin 7. The
      POST-03/2007 E90 ZGW is D-CAN (pins 6/14) and will NOT answer here —
      use the python-can / DoIP path for those cars.
    • K-Line is a SINGLE-WIRE, HALF-DUPLEX bus: every byte we transmit is
      echoed straight back on RX by the transceiver. We must read and discard
      that echo before reading the ECU's real reply, or every response is
      corrupted by our own request bytes.
    • Fast-init timing (the 25ms break low + 25ms idle high wake-up) is
      latency-sensitive over USB. On FTDI, set the driver latency timer low
      (~1ms) for reliable init. This is the one place that may need bench
      tuning; framing/checksum below is exact.

KWP2000 frame (ISO 14230-2, the variant E-series gateways use):
    [Fmt] [Target] [Source] [ (Len) ] [ data... ] [Checksum]
      • Fmt   = 0x80 | length        (length = data byte count, 0..63)
      • if length == 0 the real length is carried in a separate Len byte
        AFTER Source (used for payloads > 63 bytes).
      • Target/Source are KWP addresses — tester source is conventionally
        0xF1; the TARGET (gateway/ECU) is PER-BENCH and MUST be supplied by
        the caller (config.kline_target_addr). We never guess KWP addresses.
      • Checksum = sum of all preceding bytes & 0xFF.

The UDS client hands us a ``target_addr`` (the low byte of the UDS ECU
address) on every ``send``; we use it as the KWP target unless the config
pins an explicit ``kline_target_addr`` override.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from ..exceptions import ConnectionError_, TransportTimeout
from ..logging_setup import get_logger
from .base import AbstractTransport, TransportConfig, TransportKind

log = get_logger(__name__)

_TESTER_DEFAULT_SRC = 0xF1


def encode_kwp(target: int, source: int, data: bytes) -> bytes:
    """Build one KWP2000 (ISO 14230-2) frame with header + checksum."""
    n = len(data)
    if n <= 0x3F:
        header = bytes([0x80 | n, target & 0xFF, source & 0xFF])
    else:
        # Length escape: Fmt length field 0, real length in a 4th header byte.
        header = bytes([0x80, target & 0xFF, source & 0xFF, n & 0xFF])
    frame = header + data
    checksum = sum(frame) & 0xFF
    return frame + bytes([checksum])


def decode_kwp(frame: bytes) -> bytes:
    """Validate a KWP2000 frame's checksum and return its data field."""
    if len(frame) < 4:
        raise ValueError(f"KWP frame too short: {frame.hex()}")
    fmt = frame[0]
    if (fmt & 0xC0) != 0x80:
        raise ValueError(f"Not a KWP physical frame (fmt={fmt:#04x})")
    n = fmt & 0x3F
    if n == 0:
        n = frame[3]
        data_start = 4
    else:
        data_start = 3
    expected_len = data_start + n + 1  # + checksum byte
    if len(frame) < expected_len:
        raise ValueError(
            f"KWP frame truncated: have {len(frame)} need {expected_len}")
    body = frame[:expected_len - 1]
    checksum = frame[expected_len - 1]
    if (sum(body) & 0xFF) != checksum:
        raise ValueError(
            f"KWP checksum mismatch: got {checksum:#04x} "
            f"want {sum(body) & 0xFF:#04x}")
    return bytes(frame[data_start:data_start + n])


class KLineTransport(AbstractTransport):
    """KWP2000 (ISO 14230) UDS transport over an FTDI serial K-Line line."""

    kind = TransportKind.KLINE

    def __init__(self, config: TransportConfig) -> None:
        super().__init__(config)
        if config.serial_port is None:
            raise ValueError(
                "KLineTransport requires config.serial_port (e.g. "
                "'/dev/cu.usbserial-A50285BI' for the FTDI K+DCAN cable).")
        if config.kline_target_addr is None:
            raise ValueError(
                "KLineTransport requires config.kline_target_addr — the KWP "
                "address of the gateway/ECU on pin 7. This is per-bench and "
                "MUST be supplied by the caller; we never guess KWP addresses.")
        self._ser: Optional[object] = None  # serial.Serial
        self._lock = asyncio.Lock()

    @property
    def _source(self) -> int:
        return self.config.kline_source_addr or _TESTER_DEFAULT_SRC

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
                baudrate=self.config.kline_baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.config.timeout,
                write_timeout=self.config.timeout,
            )
            if self.config.kline_fast_init:
                self._fast_init(ser, serial)
            return ser

        try:
            self._ser = await loop.run_in_executor(None, _connect)
            self._connected = True
            log.info("K-Line (KWP2000) connected", extra={
                "port": self.config.serial_port,
                "baud": self.config.kline_baudrate,
                "target": hex(self.config.kline_target_addr),
                "source": hex(self._source),
            })
        except Exception as e:
            raise ConnectionError_(f"K-Line open failed: {e}") from e

    def _fast_init(self, ser: object, serial_mod: object) -> None:
        """ISO 14230 fast init: 25ms break-low + 25ms idle-high wake-up,
        then a StartCommunication request (SID 0x81) expecting 0xC1.

        Timing here is the latency-sensitive part over USB-FTDI; see module
        docstring. Framing of the StartCommunication request/response is exact.
        """
        ser.reset_input_buffer()  # type: ignore[attr-defined]
        ser.reset_output_buffer()  # type: ignore[attr-defined]
        # Drive the line low (break) for 25ms, then idle high for 25ms.
        ser.break_condition = True  # type: ignore[attr-defined]
        time.sleep(0.025)
        ser.break_condition = False  # type: ignore[attr-defined]
        time.sleep(0.025)

        target = self.config.kline_target_addr
        req = encode_kwp(target, self._source, bytes([0x81]))
        ser.write(req)  # type: ignore[attr-defined]
        ser.flush()  # type: ignore[attr-defined]
        self._read_and_drop_echo(ser, len(req))
        resp = self._read_frame(ser)
        data = decode_kwp(resp)
        if not data or data[0] != 0xC1:
            raise ConnectionError_(
                f"StartCommunication rejected: {resp.hex()} "
                "(expected positive response 0xC1)")

    @staticmethod
    def _read_and_drop_echo(ser: object, n: int) -> None:
        """K-Line is half-duplex: our own TX bytes echo back on RX. Read and
        discard exactly `n` echoed bytes before reading the ECU's reply."""
        if n <= 0:
            return
        ser.read(n)  # type: ignore[attr-defined]

    @staticmethod
    def _read_frame(ser: object) -> bytes:
        """Read one complete KWP frame using its header length field."""
        head = ser.read(1)  # type: ignore[attr-defined]
        if not head:
            raise TransportTimeout("K-Line: no response (header timeout)")
        fmt = head[0]
        n = fmt & 0x3F
        # Need target+source (2 bytes); +1 length byte if length escape.
        if n == 0:
            hdr_rest = ser.read(3)  # type: ignore[attr-defined]  # tgt, src, len
            if len(hdr_rest) < 3:
                raise TransportTimeout("K-Line: truncated header")
            n = hdr_rest[2]
        else:
            hdr_rest = ser.read(2)  # type: ignore[attr-defined]  # tgt, src
            if len(hdr_rest) < 2:
                raise TransportTimeout("K-Line: truncated header")
        body = ser.read(n + 1)  # type: ignore[attr-defined]  # data + checksum
        if len(body) < n + 1:
            raise TransportTimeout("K-Line: truncated body")
        return head + hdr_rest + body

    async def close(self) -> None:
        if self._ser is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, getattr(self._ser, "close", lambda: None))
        self._ser = None
        self._connected = False

    async def send(self, target_addr: int, payload: bytes) -> None:
        """Frame `payload` as KWP2000 and write it; discard the TX echo.

        The KWP target is `config.kline_target_addr` when pinned, else the
        `target_addr` the UDS client supplies (low byte of the UDS address).
        """
        if self._ser is None:
            raise ConnectionError_("K-Line not open")
        target = self.config.kline_target_addr or (target_addr & 0xFF)
        frame = encode_kwp(target, self._source, payload)
        async with self._lock:
            loop = asyncio.get_running_loop()

            def _write() -> None:
                self._ser.reset_input_buffer()  # type: ignore[union-attr]
                self._ser.write(frame)          # type: ignore[union-attr]
                self._ser.flush()               # type: ignore[union-attr]
                self._read_and_drop_echo(self._ser, len(frame))

            await loop.run_in_executor(None, _write)

    async def recv(self, timeout: Optional[float] = None) -> bytes:
        """Read one KWP frame and return its UDS data field."""
        if self._ser is None:
            raise ConnectionError_("K-Line not open")
        to = timeout if timeout is not None else self.config.timeout
        loop = asyncio.get_running_loop()

        def _recv() -> bytes:
            prev = self._ser.timeout  # type: ignore[union-attr]
            self._ser.timeout = to    # type: ignore[union-attr]
            try:
                frame = self._read_frame(self._ser)
            finally:
                self._ser.timeout = prev  # type: ignore[union-attr]
            return decode_kwp(frame)

        return await loop.run_in_executor(None, _recv)
