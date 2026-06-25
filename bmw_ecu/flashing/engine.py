"""Flash engine: bootloader → write → verify → finalize.

Implements the canonical UDS flash sequence:
    1. 0x10 0x02 — programming session
    2. 0x27 — security access
    3. 0x31 0x01 routineErase
    4. 0x34 — RequestDownload (set address + length)
    5. 0x36 — TransferData (loop, chunked)
    6. 0x37 — RequestTransferExit
    7. 0x31 0x01 routineCheckProgrammingDependencies
    8. 0x11 — ECU reset

Everything is wrapped in a RollbackGuard. Pre-flight (battery + backup +
payload validation) runs BEFORE the sequence starts.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from ..exceptions import ChecksumMismatch, FlashError
from ..logging_setup import get_logger
from ..safety.backup import EcuBackup
from ..safety.preflight import PreflightGate
from ..safety.rollback import RollbackGuard
from ..uds.client import UdsClient
from ..uds.security_access import SecurityAccess
from ..uds.services import SID, DiagSession
from ..validation.payload_validator import PayloadValidator
from .checksum import compute_checksum

log = get_logger(__name__)

ROUTINE_ERASE = 0xFF00
ROUTINE_CHECK_DEPS = 0xFF01
ROUTINE_START = 0x01


@dataclass
class FlashPlan:
    ecu_name: str
    vin: str
    target_addr: int
    payload: bytes
    chunk_size: int = 0x400        # 1 KiB per TransferData
    expected_checksum: Optional[int] = None
    checksum_algo: str = "crc32"


class FlashEngine:
    def __init__(self, client: UdsClient, security: SecurityAccess,
                 preflight: PreflightGate, validator: PayloadValidator) -> None:
        self.client = client
        self.security = security
        self.preflight = preflight
        self.validator = validator

    async def flash(self, plan: FlashPlan, *, dump_callable) -> None:
        log.info("Flash begin", extra={
            "ecu": plan.ecu_name, "size": len(plan.payload),
            "addr": hex(plan.target_addr),
        })

        # 1. Static validation.
        v = self.validator.validate(
            ecu_name=plan.ecu_name, payload=plan.payload, target_addr=plan.target_addr,
        )
        v.raise_if_failed()

        # 2. Pre-flight (voltage + backup).
        result = await self.preflight.check(
            vin=plan.vin, ecu_name=plan.ecu_name, memory_region="FLASH",
            dump_callable=dump_callable, write_kind="flash",
        )
        backup = self.preflight.store.load(plan.vin, plan.ecu_name, result.backup_sha)
        assert backup is not None

        async with RollbackGuard(backup, restore_fn=self._rollback_restore) as guard:
            await self._run_flash_sequence(plan)
            guard.commit()
        log.info("Flash complete", extra={"ecu": plan.ecu_name})

    async def _run_flash_sequence(self, plan: FlashPlan) -> None:
        await self.client.diagnostic_session_control(DiagSession.PROGRAMMING)
        await self.security.unlock(vin=plan.vin)

        # Erase
        await self.client.routine_control(ROUTINE_START, ROUTINE_ERASE,
                                          self._addr_to_bytes(plan.target_addr))

        # RequestDownload
        size_bytes = len(plan.payload).to_bytes(4, "big")
        await self.client.raw_request(
            bytes([SID.REQUEST_DOWNLOAD, 0x00, 0x44]) +
            self._addr_to_bytes(plan.target_addr) + size_bytes,
        )

        # TransferData chunks
        seq = 1
        for off in range(0, len(plan.payload), plan.chunk_size):
            chunk = plan.payload[off:off + plan.chunk_size]
            await self.client.raw_request(
                bytes([SID.TRANSFER_DATA, seq & 0xFF]) + chunk, timeout=10.0,
            )
            seq += 1

        # Exit
        await self.client.raw_request(bytes([SID.REQUEST_TRANSFER_EXIT]))

        # Verify checksum if provided.
        expected = plan.expected_checksum
        actual = compute_checksum(plan.payload, algo=plan.checksum_algo)
        if expected is not None and actual != expected:
            raise ChecksumMismatch(
                f"Local checksum {actual:#x} != expected {expected:#x}",
            )

        # Dependency check — ECU-side acceptance.
        try:
            await self.client.routine_control(ROUTINE_START, ROUTINE_CHECK_DEPS)
        except Exception as e:
            raise FlashError(f"Dependency check failed: {e}") from e

        # Reset
        await self.client.ecu_reset(0x01)
        # Brief settle so the next caller doesn't hit the bus during reboot.
        await asyncio.sleep(2.0)

    async def _rollback_restore(self, backup: EcuBackup) -> None:
        log.warning("Rolling back flash", extra={"ecu": backup.ecu_name})
        rollback_plan = FlashPlan(
            ecu_name=backup.ecu_name, vin=backup.vin,
            target_addr=int(backup.metadata.get("origin_addr", 0)),
            payload=backup.data, expected_checksum=None,
        )
        await self._run_flash_sequence(rollback_plan)

    @staticmethod
    def _addr_to_bytes(addr: int) -> bytes:
        return addr.to_bytes(4, "big")
