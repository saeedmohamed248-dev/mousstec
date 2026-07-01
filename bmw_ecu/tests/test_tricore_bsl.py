"""TricoreBslLink tests — the real (safe) handshake + the honest flash refusals.

The BSL link is the no-external-device ISN fallback. These tests prove:
  • the handshake actually drives the serial line (fast-init + trigger byte) and
    verifies the chip's ack (0x55) — with an injected fake serial, no hardware;
  • a wrong/absent ack raises BslHandshakeFailed (recoverable — fix the wiring);
  • ISN read/write REFUSE (BslNotConfigured) until a confirmed BslFlashProfile
    (offset + BSL command sequence) is registered — never a guessed flash op.
"""
from __future__ import annotations

import asyncio
import unittest

from bmw_ecu.isn.tricore_bsl import (
    BslFlashProfile,
    BslHandshakeFailed,
    BslHardwareProfile,
    BslNotConfigured,
    TricoreBslLink,
    get_bsl_profile,
)


def _run(coro):
    return asyncio.run(coro)


class _FakeSerial:
    """Minimal pyserial stand-in. Records writes, returns a scripted ack byte."""

    def __init__(self, ack: bytes = b"\x55") -> None:
        self._ack = ack
        self.writes: list[bytes] = []
        self.break_condition = False
        self.closed = False

    def reset_input_buffer(self) -> None:
        pass

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    def flush(self) -> None:
        pass

    def read(self, n: int) -> bytes:
        return self._ack

    def close(self) -> None:
        self.closed = True


def _profile(**kw) -> BslHardwareProfile:
    base = dict(dme_family="MEVD17", chip="TC1797", boot_pin_label="pad",
                pull="1kΩ", serial_pin_map="TX/RX/GND")
    base.update(kw)
    return BslHardwareProfile(**base)


class HandshakeTests(unittest.TestCase):
    def test_handshake_ok_on_correct_ack(self) -> None:
        ser = _FakeSerial(ack=b"\x55")
        link = TricoreBslLink(port="/dev/fake", profile=_profile(),
                              serial_factory=lambda: ser)
        _run(link.handshake())            # must not raise
        self.assertEqual(ser.writes, [b"\x00"])  # BSL trigger byte was sent

    def test_handshake_fails_on_wrong_ack(self) -> None:
        ser = _FakeSerial(ack=b"\xAA")
        link = TricoreBslLink(port="/dev/fake", profile=_profile(),
                              serial_factory=lambda: ser)
        with self.assertRaises(BslHandshakeFailed):
            _run(link.handshake())

    def test_handshake_fails_on_no_answer(self) -> None:
        ser = _FakeSerial(ack=b"")
        link = TricoreBslLink(port="/dev/fake", profile=_profile(),
                              serial_factory=lambda: ser)
        with self.assertRaises(BslHandshakeFailed):
            _run(link.handshake())


class FlashRefusalTests(unittest.TestCase):
    def test_read_isn_refuses_without_flash_profile(self) -> None:
        link = TricoreBslLink(port="/dev/fake", profile=_profile(flash=None),
                              serial_factory=lambda: _FakeSerial())
        with self.assertRaises(BslNotConfigured):
            _run(link.read_isn())

    def test_write_isn_refuses_without_flash_profile(self) -> None:
        link = TricoreBslLink(port="/dev/fake", profile=_profile(flash=None),
                              serial_factory=lambda: _FakeSerial())
        with self.assertRaises(BslNotConfigured):
            _run(link.write_isn(bytes(32)))

    def test_write_isn_refuses_incomplete_flash_profile(self) -> None:
        # An offset alone (no confirmed command bytes) is still not enough.
        flash = BslFlashProfile(isn_offset=0x1000, read_cmd=b"", write_cmd=b"")
        link = TricoreBslLink(port="/dev/fake", profile=_profile(flash=flash),
                              serial_factory=lambda: _FakeSerial())
        with self.assertRaises(BslNotConfigured):
            _run(link.write_isn(bytes(32)))


class RegistryTests(unittest.TestCase):
    def test_mevd17_profile_ships_unverified(self) -> None:
        # No guessed hardware asserted as fact: the starter MEVD17 profile is a
        # template the operator must confirm before it is trusted.
        hw = get_bsl_profile("MEVD17")
        self.assertIsNotNone(hw)
        self.assertFalse(hw.verified)
        self.assertIsNone(hw.flash)


if __name__ == "__main__":
    unittest.main()
