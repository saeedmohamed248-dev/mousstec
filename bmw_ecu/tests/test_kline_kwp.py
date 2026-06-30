"""K-Line / KWP2000 (ISO 14230) transport — framing + half-duplex echo.

The KWP2000 frame encode/decode is pure and deterministic, so we test it with
no hardware. We also drive ``open``/``send``/``recv`` against a *fake* serial
port that faithfully models the one detail that breaks naive K-Line code: the
bus is half-duplex, so every TX byte is echoed back on RX and must be discarded
before the ECU's real reply is read.

This keeps the main suite green without an FTDI cable plugged in.
"""
from __future__ import annotations

import asyncio
import unittest

from bmw_ecu.connection.base import TransportConfig, TransportKind
from bmw_ecu.connection.kline import (
    KLineTransport,
    decode_kwp,
    encode_kwp,
)


def _cfg(**kw) -> TransportConfig:
    base = dict(
        kind=TransportKind.KLINE,
        serial_port="/dev/fake-ftdi",
        kline_target_addr=0x12,
        kline_fast_init=False,  # no init wake-up in framing tests
        timeout=1.0,
    )
    base.update(kw)
    return TransportConfig(**base)


class KwpFramingTests(unittest.TestCase):
    def test_short_frame_roundtrip(self) -> None:
        data = bytes([0x22, 0xF1, 0x90])
        frame = encode_kwp(target=0x12, source=0xF1, data=data)
        # Fmt = 0x80 | 3, then target, source, data..., checksum.
        self.assertEqual(frame[0], 0x83)
        self.assertEqual(frame[1], 0x12)
        self.assertEqual(frame[2], 0xF1)
        self.assertEqual(frame[-1], sum(frame[:-1]) & 0xFF)
        self.assertEqual(decode_kwp(frame), data)

    def test_length_escape_for_long_payload(self) -> None:
        data = bytes(range(70))  # > 63 ⇒ length-escape header
        frame = encode_kwp(target=0x12, source=0xF1, data=data)
        self.assertEqual(frame[0], 0x80)        # length field 0 ⇒ escaped
        self.assertEqual(frame[3], 70)          # real length in 4th byte
        self.assertEqual(decode_kwp(frame), data)

    def test_decode_rejects_bad_checksum(self) -> None:
        frame = bytearray(encode_kwp(0x12, 0xF1, bytes([0x10])))
        frame[-1] ^= 0xFF
        with self.assertRaises(ValueError):
            decode_kwp(bytes(frame))

    def test_decode_rejects_non_kwp_format(self) -> None:
        with self.assertRaises(ValueError):
            decode_kwp(bytes([0x00, 0x12, 0xF1, 0x00]))


class KlineConfigTests(unittest.TestCase):
    def test_requires_serial_port(self) -> None:
        with self.assertRaises(ValueError):
            KLineTransport(TransportConfig(
                kind=TransportKind.KLINE, kline_target_addr=0x12))

    def test_requires_target_addr(self) -> None:
        # Safety rule: never guess KWP addresses.
        with self.assertRaises(ValueError):
            KLineTransport(TransportConfig(
                kind=TransportKind.KLINE, serial_port="/dev/x"))


class _FakeSerial:
    """Minimal pyserial stand-in modelling half-duplex echo.

    Anything written is appended to the RX buffer (the echo), followed by any
    pre-loaded ECU response bytes. ``read(n)`` pops from the front.
    """

    def __init__(self, response: bytes = b"") -> None:
        self._rx = bytearray()
        self._pending_response = bytearray(response)
        self.timeout = 1.0
        self.break_condition = False

    # writing echoes back, then releases the staged ECU response
    def write(self, data: bytes) -> int:
        self._rx.extend(data)
        self._rx.extend(self._pending_response)
        self._pending_response.clear()
        return len(data)

    def flush(self) -> None:
        pass

    def read(self, n: int = 1) -> bytes:
        chunk = bytes(self._rx[:n])
        del self._rx[:n]
        return chunk

    def reset_input_buffer(self) -> None:
        self._rx.clear()

    def reset_output_buffer(self) -> None:
        pass

    def close(self) -> None:
        pass


class KlineEchoTests(unittest.TestCase):
    @staticmethod
    def _open_with(fake: _FakeSerial) -> KLineTransport:
        # Built inside a running loop (asyncio.Lock needs one on py3.9).
        t = KLineTransport(_cfg())
        t._ser = fake
        t._connected = True
        return t

    def test_send_discards_tx_echo(self) -> None:
        fake = _FakeSerial()

        async def _run() -> None:
            t = self._open_with(fake)
            await t.send(0x12, bytes([0x22, 0xF1, 0x90]))

        asyncio.run(_run())
        # The echo of our own request must be fully consumed.
        self.assertEqual(len(fake._rx), 0)

    def test_recv_decodes_ecu_reply(self) -> None:
        # Stage a positive response 0x62 F1 90 <vin-ish> as the ECU reply.
        reply_data = bytes([0x62, 0xF1, 0x90, 0xAB, 0xCD])
        reply_frame = encode_kwp(target=0xF1, source=0x12, data=reply_data)
        fake = _FakeSerial(response=reply_frame)

        async def _run() -> bytes:
            t = self._open_with(fake)
            await t.send(0x12, bytes([0x22, 0xF1, 0x90]))
            return await t.recv(timeout=1.0)

        self.assertEqual(asyncio.run(_run()), reply_data)


if __name__ == "__main__":
    unittest.main()
