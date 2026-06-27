"""BDM (Background Debug Mode) physical transport.

BDM is the single-wire debug protocol Freescale exposed on the
MC9S12X family. It rides on a dedicated BKGD pin and lets a host
read/write memory + flash regions on a halted CPU without going
through normal firmware. We use it to recover bricked FRM3 modules
that no longer respond to UDS.

The abstraction is intentionally minimal — exactly the operations
the orchestrator needs to drive the recovery flow:

  connect()        : hold RESET low + drive BKGD → enter active BDM
  disconnect()     : release lines + let the chip resume normally
  read_dflash()    : pull `length` bytes starting at the chosen address
  write_dflash()   : burn back the rebuilt blob (page-aligned writes)
  read_byte() / write_byte() : single-byte helpers for VO/FA touch-ups

Production wires this to the Mousstec Smart Harness's BDM channel
(USB-CDC over the breakout box). Tests use MockBdmTransport which
keeps a writeable byte-buffer that records every operation so
assertions can verify the orchestrator's behaviour.
"""
from __future__ import annotations

import abc
from typing import Optional


class BdmConnectionError(Exception):
    """Raised when the harness fails to bring the chip into BDM."""


class BdmReadError(Exception):
    """Raised on a verified-bad memory read (CRC mismatch, NACK, etc.)."""


class BdmWriteError(Exception):
    """Raised when a flash-write fails its readback verification."""


# ─────────────────────────────────────────────────────────────────────
class AbstractBdmTransport(abc.ABC):
    """Each method is awaitable so production can run the BDM POD's
    USB-CDC waits without blocking the asyncio loop."""

    @abc.abstractmethod
    async def connect(self, *, bdm_clock_khz: int = 800,
                      reset_low_ms: int = 100) -> None:
        """Enter active BDM on the target. Raises BdmConnectionError
        if the target doesn't respond inside reset_low_ms × 2."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Release BKGD + RESET so the target can resume."""

    @abc.abstractmethod
    async def read_dflash(self, *, address: int, length: int) -> bytes:
        """Sequential read of `length` bytes starting at `address`."""

    @abc.abstractmethod
    async def write_dflash(self, *, address: int, data: bytes) -> None:
        """Page-aligned write of `data` starting at `address`. Implementations
        are expected to verify each page with a read-back; failure raises
        BdmWriteError so the orchestrator can pause and not advance state."""

    async def read_byte(self, address: int) -> int:
        """Single-byte read. Default implementation reuses read_dflash."""
        b = await self.read_dflash(address=address, length=1)
        return b[0]

    async def write_byte(self, address: int, value: int) -> None:
        """Single-byte write. Default implementation reuses write_dflash."""
        if not (0 <= value <= 0xFF):
            raise BdmWriteError(f"value {value!r} not in [0, 255]")
        await self.write_dflash(address=address, data=bytes((value,)))


# ─────────────────────────────────────────────────────────────────────
# Mock — pure-Python, deterministic. Backs onto a bytearray sized to
# match the variant's D-Flash window so the orchestrator can read +
# write without any real hardware.
# ─────────────────────────────────────────────────────────────────────
class MockBdmTransport(AbstractBdmTransport):
    """Test double. Construct with a backing bytearray that simulates
    the FRM3's D-Flash memory; reads/writes operate directly on it.

    Usage:
        buf = bytearray(b"\\xFF" * 8192)
        buf[0x40:0x51] = b"WBA12345678901234"
        bdm = MockBdmTransport(memory=buf, dflash_base=0x100000)
        bdm.simulate_no_connect = False   # set True to force connect failure
    """

    def __init__(
        self,
        *,
        memory: Optional[bytearray] = None,
        dflash_base: int = 0x10_0000,
        simulate_no_connect: bool = False,
        simulate_write_error_at: Optional[int] = None,
    ) -> None:
        self.memory = memory if memory is not None else bytearray(b"\xFF" * 8192)
        self.dflash_base = dflash_base
        self.simulate_no_connect = simulate_no_connect
        # If set, the next write_dflash that overlaps this absolute
        # address raises BdmWriteError. Tests use it to verify the
        # orchestrator pauses on a verify miss.
        self.simulate_write_error_at = simulate_write_error_at

        # ── Audit trail ──────────────────────────────────────────
        self.connect_calls: list[dict] = []
        self.read_calls: list[tuple[int, int]] = []   # (addr, length)
        self.write_calls: list[tuple[int, int]] = []  # (addr, length)
        self.connected: bool = False

    def _to_offset(self, address: int) -> int:
        """Translate a linear-map address to an index in self.memory."""
        offset = address - self.dflash_base
        if offset < 0 or offset >= len(self.memory):
            raise BdmReadError(
                f"address 0x{address:X} outside D-Flash window "
                f"[0x{self.dflash_base:X}..0x{self.dflash_base + len(self.memory):X})",
            )
        return offset

    async def connect(self, *, bdm_clock_khz: int = 800,
                      reset_low_ms: int = 100) -> None:
        self.connect_calls.append({
            "bdm_clock_khz": bdm_clock_khz, "reset_low_ms": reset_low_ms,
        })
        if self.simulate_no_connect:
            raise BdmConnectionError(
                "MockBdmTransport: simulated no-ACK from target. "
                "Real harness would re-try with a slower BKGD clock.",
            )
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def read_dflash(self, *, address: int, length: int) -> bytes:
        if not self.connected:
            raise BdmReadError("read_dflash called before connect()")
        if length <= 0:
            raise BdmReadError(f"length must be > 0, got {length}")
        offset = self._to_offset(address)
        if offset + length > len(self.memory):
            raise BdmReadError(
                f"read window [{address:X}+{length}] overruns D-Flash window",
            )
        self.read_calls.append((address, length))
        return bytes(self.memory[offset:offset + length])

    async def write_dflash(self, *, address: int, data: bytes) -> None:
        if not self.connected:
            raise BdmWriteError("write_dflash called before connect()")
        if not data:
            return
        offset = self._to_offset(address)
        if offset + len(data) > len(self.memory):
            raise BdmWriteError(
                f"write window [{address:X}+{len(data)}] overruns D-Flash window",
            )

        # Simulated failure path for tests.
        if (self.simulate_write_error_at is not None
                and address <= self.simulate_write_error_at
                < address + len(data)):
            raise BdmWriteError(
                f"MockBdmTransport: simulated verify-miss at "
                f"0x{self.simulate_write_error_at:X}",
            )

        self.write_calls.append((address, len(data)))
        self.memory[offset:offset + len(data)] = data
