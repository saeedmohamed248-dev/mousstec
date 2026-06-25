"""ISN injection into DME (e.g. N20 on F30/F10/F25).

⚠️  Reality check:
    On N20 (Bosch MEVD17) the ISN sits in a protected region of the Tricore
    flash. Writing it via UDS alone is NOT possible on most production
    firmware revisions — you need either:
        (a) DME in boot mode (BSL) over the K-line, or
        (b) Bench programming via Tricore BDM (Xprog, KESS, Trasdata).

    This class implements the UDS path (works on some early MSD81 / MSV90
    DMEs and on later units with workshop-mode unlock). For BDM, plug
    `bench_provider` and bypass `inject_via_uds`.
"""
from __future__ import annotations

from typing import Optional, Protocol

from ..exceptions import IsnMismatch
from ..logging_setup import get_logger
from ..safety.preflight import PreflightGate
from ..safety.rollback import RollbackGuard
from ..safety.backup import EcuBackup
from ..uds.client import UdsClient
from ..uds.security_access import SecurityAccess
from ..uds.services import BmwDID, DiagSession
from .extractor import ISN_LENGTH

log = get_logger(__name__)


class BenchProvider(Protocol):
    """Hardware-probe escape hatch for ECUs that can't be ISN-written via UDS."""
    async def write_isn(self, isn: bytes) -> None: ...
    async def dump_eeprom(self) -> bytes: ...
    async def restore(self, backup: EcuBackup) -> None: ...


class IsnInjector:
    def __init__(self, client: UdsClient, security: SecurityAccess,
                 preflight: PreflightGate, *, bench: Optional[BenchProvider] = None) -> None:
        self.client = client
        self.security = security
        self.preflight = preflight
        self.bench = bench

    async def inject(self, *, vin: str, ecu_name: str, isn: bytes,
                     did: int = BmwDID.ISN) -> None:
        if len(isn) != ISN_LENGTH:
            raise IsnMismatch(f"ISN must be {ISN_LENGTH} bytes, got {len(isn)}")

        # Pre-flight: voltage + backup of current DME EEPROM.
        dump_fn = self.bench.dump_eeprom if self.bench else self._dump_via_uds
        result = await self.preflight.check(
            vin=vin, ecu_name=ecu_name, memory_region="EEPROM",
            dump_callable=dump_fn, write_kind="coding",
        )
        backup = self.preflight.store.load(vin, ecu_name, result.backup_sha)
        assert backup is not None

        restore_fn = self.bench.restore if self.bench else self._restore_via_uds

        async with RollbackGuard(backup, restore_fn=restore_fn) as guard:
            if self.bench is not None:
                log.info("ISN inject via bench (BDM)", extra={"ecu": ecu_name})
                await self.bench.write_isn(isn)
            else:
                log.info("ISN inject via UDS", extra={"ecu": ecu_name})
                await self._inject_via_uds(vin=vin, isn=isn, did=did)

            # Verify by read-back.
            await self.security.unlock(vin=vin)
            read_back = await self.client.read_data_by_identifier(did)
            if read_back != isn:
                raise IsnMismatch(
                    "Verification failed: read-back differs from injected ISN",
                )
            guard.commit()
        log.info("ISN injection verified", extra={"ecu": ecu_name})

    # --- UDS path ----------------------------------------------------------
    async def _inject_via_uds(self, *, vin: str, isn: bytes, did: int) -> None:
        await self.client.diagnostic_session_control(DiagSession.PROGRAMMING)
        await self.security.unlock(vin=vin)
        await self.client.write_data_by_identifier(did, isn)

    async def _dump_via_uds(self) -> bytes:
        # Minimal: read the ISN DID as the "before" snapshot. Real DME backup
        # uses RequestUpload (0x35) over a region range — implement per ECU.
        await self.client.diagnostic_session_control(DiagSession.EXTENDED)
        await self.security.unlock()
        try:
            return await self.client.read_data_by_identifier(BmwDID.ISN)
        except Exception:
            # Virgin ECU may refuse — return a placeholder so backup is still saved.
            return bytes(32)

    async def _restore_via_uds(self, backup: EcuBackup) -> None:
        await self.client.diagnostic_session_control(DiagSession.PROGRAMMING)
        # Best-effort: rewrite the same DID with the captured value.
        await self.client.write_data_by_identifier(BmwDID.ISN, backup.data)
