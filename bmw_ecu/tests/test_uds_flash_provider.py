"""Real UDS-backed flash provider (#4).

Exercises UdsFlashProvider against a minimal in-test UDS bus that speaks the
flash SIDs (0x10/0x22/0x23/0x27/0x31/0x34/0x36/0x37/0x11), proving the byte
formats and the backup→restore round-trip. Also proves that with no licensed
seed-key provider, unlock_security fails loudly (FlashSecurityDenied) instead
of sending a fake key.
"""
from __future__ import annotations

import asyncio
import unittest

from bmw_ecu.flashing import FlashSecurityDenied, UdsFlashProvider
from bmw_ecu.uds import (
    MockSeedKeyProvider,
    SecurityAccess,
    UdsClient,
    UnavailableSeedKeyProvider,
)


class _FlashBus:
    """Minimal UDS responder for the flash SIDs."""

    def __init__(self, *, mem: bytes = b"", max_block: int = 0x100,
                 already_unlocked: bool = True) -> None:
        self.mem = bytearray(mem)
        self.max_block = max_block
        self.already_unlocked = already_unlocked
        self._dl_addr = 0

    async def open(self) -> None:
        pass

    async def request(self, addr: int, payload: bytes, *, timeout: float = 5.0) -> bytes:
        sid = payload[0]
        if sid == 0x10:  # session control
            return bytes([0x50, payload[1], 0x00, 0x32, 0x01, 0xF4])
        if sid == 0x27:  # security access
            sub = payload[1]
            if sub % 2 == 1:  # request seed
                seed = bytes(4) if self.already_unlocked else b"\x01\x02\x03\x04"
                return bytes([0x67, sub]) + seed
            return bytes([0x67, sub])  # key accepted
        if sid == 0x22:  # RDBI (version)
            return bytes([0x62, payload[1], payload[2]]) + b"SW_99"
        if sid == 0x23:  # ReadMemoryByAddress
            a = int.from_bytes(payload[2:6], "big")
            n = int.from_bytes(payload[6:10], "big")
            return bytes([0x63]) + bytes(self.mem[a:a + n])
        if sid == 0x31:  # routine control (erase / check deps)
            return bytes([0x71]) + payload[1:4] + b"\x00"
        if sid == 0x34:  # RequestDownload
            self._dl_addr = int.from_bytes(payload[3:7], "big")
            size = int.from_bytes(payload[7:11], "big")
            if self._dl_addr + size > len(self.mem):
                self.mem.extend(b"\x00" * (self._dl_addr + size - len(self.mem)))
            # lengthFormatId 0x20 → 2-byte maxBlockLen (incl. 2-byte header)
            return bytes([0x74, 0x20]) + self.max_block.to_bytes(2, "big")
        if sid == 0x36:  # TransferData
            data = payload[2:]
            self.mem[self._dl_addr:self._dl_addr + len(data)] = data
            self._dl_addr += len(data)
            return bytes([0x76, payload[1]])
        if sid == 0x37:  # TransferExit
            return bytes([0x77])
        if sid == 0x11:  # ECU reset
            return bytes([0x51, payload[1]])
        return bytes([0x7F, sid, 0x11])  # service not supported

    async def recv(self, timeout=None) -> bytes:  # pragma: no cover
        raise AssertionError("recv not expected in this fake")


def _provider(bus: _FlashBus, *, provider=None) -> UdsFlashProvider:
    client = UdsClient(bus, ecu_addr=0x40, session_name="flash")
    security = SecurityAccess(client, provider or MockSeedKeyProvider())
    return UdsFlashProvider(client, security, ecu_name="FEM", read_block_size=0x40)


class UdsFlashProviderTests(unittest.TestCase):
    def test_version_read(self) -> None:
        bus = _FlashBus()
        v = asyncio.run(_provider(bus).read_current_version())
        self.assertEqual(v, "SW_99")

    def test_backup_round_trip(self) -> None:
        original = bytes(range(256))
        bus = _FlashBus(mem=original)

        async def go():
            p = _provider(bus)
            await p.enter_programming_session()
            await p.unlock_security(vin="WBA0")
            backup = await p.read_backup(addr=0, size=256)
            self.assertEqual(backup.data, original)
            # Corrupt memory, then restore should make it whole again.
            bus.mem[0:4] = b"\xDE\xAD\xBE\xEF"
            await p.restore_backup(backup)
            return bytes(bus.mem[:256])

        self.assertEqual(asyncio.run(go()), original)

    def test_request_download_parses_max_block_len(self) -> None:
        bus = _FlashBus(max_block=0x102)  # 0x102 incl. 2-byte header

        async def go():
            p = _provider(bus)
            return await p.request_download(addr=0x1000, size=0x10)

        # provider subtracts the 2-byte header → 0x100 usable payload.
        self.assertEqual(asyncio.run(go()), 0x100)

    def test_no_licensed_provider_blocks_unlock(self) -> None:
        bus = _FlashBus(already_unlocked=False)  # forces a real key computation
        p = _provider(bus, provider=UnavailableSeedKeyProvider("FEM", 0x05))

        async def go():
            await p.unlock_security(vin="WBA0")

        with self.assertRaises(FlashSecurityDenied):
            asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
