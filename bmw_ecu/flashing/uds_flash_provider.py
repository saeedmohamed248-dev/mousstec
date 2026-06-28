"""Real, UDS-backed flash provider (#4).

This is the hardware counterpart to `MockFlashProvider`: it implements the
same `AbstractFlashProvider` contract by driving a live `UdsClient` +
`SecurityAccess` over the real transport, using the exact UDS programming
byte formats the proven `FlashEngine` uses (0x10/0x27/0x31/0x34/0x36/0x37/
0x11). So the guided `FlashOrchestrator` — including its backup-before-erase
guard and rollback path — runs unchanged against a real ECU.

What is intentionally NOT here (and cannot be faked safely):
  • the licensed seed-key algorithm — `unlock_security` delegates to
    SecurityAccess, which raises SeedKeyUnavailable until a real provider is
    registered (see uds/seed_key_providers.py);
  • the firmware image itself + its signature/checksum — supplied by the
    caller's FlashPlan, validated by PayloadValidator.

So this class makes the *transport* real; it does not invent crypto or
firmware.
"""
from __future__ import annotations

from typing import Optional

from ..exceptions import SecurityAccessDenied, UdsNegativeResponse
from ..logging_setup import get_logger
from ..uds.client import UdsClient
from ..uds.security_access import SecurityAccess
from ..uds.seed_key_providers import SeedKeyUnavailable
from ..uds.services import SID, DiagSession
from .flash_provider import (
    AbstractFlashProvider,
    FlashBackup,
    FlashDependencyError,
    FlashRejected,
    FlashSecurityDenied,
    FlashTransportError,
)

log = get_logger(__name__)

ROUTINE_START = 0x01
ROUTINE_ERASE = 0xFF00
ROUTINE_CHECK_DEPS = 0xFF01


class UdsFlashProvider(AbstractFlashProvider):
    def __init__(self, client: UdsClient, security: SecurityAccess, *,
                 ecu_name: str = "", version_did: int = 0xF195,
                 read_block_size: int = 0x400,
                 default_max_block_len: int = 0x400) -> None:
        self.client = client
        self.security = security
        self.ecu_name = ecu_name
        self.version_did = version_did
        self.read_block_size = read_block_size
        self.default_max_block_len = default_max_block_len
        self._origin_addr: Optional[int] = None

    @staticmethod
    def _addr(addr: int) -> bytes:
        return addr.to_bytes(4, "big")

    async def read_current_version(self) -> str:
        try:
            raw = await self.client.read_data_by_identifier(self.version_did)
        except Exception as e:
            raise FlashTransportError(f"version read failed: {e}") from e
        return raw.decode("ascii", "ignore").strip("\x00 ").strip() or raw.hex()

    async def enter_programming_session(self) -> None:
        try:
            await self.client.diagnostic_session_control(DiagSession.PROGRAMMING)
        except Exception as e:
            raise FlashTransportError(
                f"ECU refused programming session: {e}") from e

    async def unlock_security(self, *, vin: str) -> None:
        try:
            await self.security.unlock(vin=vin)
        except SeedKeyUnavailable as e:
            # No licensed key algorithm installed → cannot proceed. Surface
            # clearly rather than letting a wrong key reach the ECU.
            raise FlashSecurityDenied(str(e)) from e
        except SecurityAccessDenied as e:
            raise FlashSecurityDenied(f"security access denied: {e}") from e

    async def read_backup(self, *, addr: int, size: int) -> FlashBackup:
        """ReadMemoryByAddress (0x23) over the region, chunked."""
        self._origin_addr = addr
        out = bytearray()
        off = 0
        while off < size:
            n = min(self.read_block_size, size - off)
            req = (bytes([SID.READ_MEMORY_BY_ADDRESS, 0x44])
                   + self._addr(addr + off) + n.to_bytes(4, "big"))
            try:
                resp = await self.client.raw_request(req, timeout=10.0)
            except Exception as e:
                raise FlashTransportError(f"backup read failed @0x{addr+off:08X}: {e}") from e
            # Positive response: [0x63, ...data]
            out += resp[1:] if resp and resp[0] == 0x63 else resp
            off += n
        return FlashBackup(ecu_name=self.ecu_name, vin="",
                           origin_addr=addr, data=bytes(out))

    async def erase(self, *, addr: int) -> None:
        self._origin_addr = addr
        try:
            await self.client.routine_control(ROUTINE_START, ROUTINE_ERASE,
                                              self._addr(addr))
        except UdsNegativeResponse as e:
            raise FlashRejected(f"erase rejected @0x{addr:08X}: NRC=0x{e.nrc:02X}") from e
        except Exception as e:
            raise FlashTransportError(f"erase failed: {e}") from e

    async def request_download(self, *, addr: int, size: int) -> int:
        self._origin_addr = addr
        req = (bytes([SID.REQUEST_DOWNLOAD, 0x00, 0x44])
               + self._addr(addr) + size.to_bytes(4, "big"))
        try:
            resp = await self.client.raw_request(req)
        except UdsNegativeResponse as e:
            raise FlashRejected(f"RequestDownload rejected: NRC=0x{e.nrc:02X}") from e
        except Exception as e:
            raise FlashTransportError(f"RequestDownload failed: {e}") from e
        # Positive: [0x74, lengthFormatId, maxBlockLen(N bytes)...]
        return self._parse_max_block_len(resp)

    def _parse_max_block_len(self, resp: bytes) -> int:
        if not resp or resp[0] != 0x74 or len(resp) < 3:
            return self.default_max_block_len
        n = (resp[1] >> 4) & 0x0F  # high nibble = #bytes of maxBlockLen
        if n == 0 or len(resp) < 2 + n:
            return self.default_max_block_len
        val = int.from_bytes(resp[2:2 + n], "big")
        # The reported length includes the 0x36+seq header; subtract it.
        return max(1, val - 2)

    async def transfer_block(self, *, seq: int, data: bytes) -> None:
        try:
            await self.client.raw_request(
                bytes([SID.TRANSFER_DATA, seq & 0xFF]) + data, timeout=10.0)
        except UdsNegativeResponse as e:
            raise FlashRejected(f"TransferData seq {seq} rejected: NRC=0x{e.nrc:02X}") from e
        except Exception as e:
            raise FlashTransportError(f"TransferData seq {seq} failed: {e}") from e

    async def request_transfer_exit(self) -> None:
        try:
            await self.client.raw_request(bytes([SID.REQUEST_TRANSFER_EXIT]))
        except UdsNegativeResponse as e:
            raise FlashRejected(f"TransferExit rejected: NRC=0x{e.nrc:02X}") from e
        except Exception as e:
            raise FlashTransportError(f"TransferExit failed: {e}") from e

    async def check_dependencies(self) -> None:
        try:
            await self.client.routine_control(ROUTINE_START, ROUTINE_CHECK_DEPS)
        except Exception as e:
            raise FlashDependencyError(f"dependency check failed: {e}") from e

    async def ecu_reset(self) -> None:
        await self.client.ecu_reset(0x01)

    async def restore_backup(self, backup: FlashBackup) -> None:
        """Rollback: re-write the saved image to its origin address.

        Mirrors erase → download → transfer → exit. Must be safe after a
        partial flash, so it re-runs the full write of the known-good bytes.
        """
        addr = backup.origin_addr
        await self.erase(addr=addr)
        block = await self.request_download(addr=addr, size=backup.size)
        seq = 1
        for off in range(0, backup.size, block):
            await self.transfer_block(seq=seq, data=backup.data[off:off + block])
            seq += 1
        await self.request_transfer_exit()
