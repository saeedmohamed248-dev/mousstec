"""SoftwareOnlyStrategy — exploit + extract + inject via UDS/DoIP only."""
from __future__ import annotations

from ...exceptions import (
    IsnMismatch,
    SecurityAccessDenied,
    UdsNegativeResponse,
)
from ...isn.extractor import ISN_LENGTH
from ...logging_setup import get_logger
from ...uds.client import UdsClient
from ...uds.services import DiagSession
from ..base import ExecutionStrategy, StrategyContext, StrategyOutcome, StrategyResult
from . import catalog
from . import boot_mode

log = get_logger(__name__)


class SoftwareOnlyStrategy(ExecutionStrategy):
    name = "software_only"
    requires_software_capable = True

    async def extract_isn(self, ctx: StrategyContext) -> StrategyResult:
        client = UdsClient(ctx.transport, ecu_addr=ctx.profile.uds_isn_did >> 8,
                           session_name="sw_extract")
        # Try the cleanest path first: normal security access.
        try:
            await client.diagnostic_session_control(DiagSession.EXTENDED)
            await ctx.security.unlock(vin=ctx.vin)
            isn = await client.read_data_by_identifier(ctx.profile.uds_isn_did)
            return self._verified(isn)
        except (SecurityAccessDenied, UdsNegativeResponse) as e:
            log.info("Normal path failed — trying catalog exploits", extra={"err": str(e)})

        # Try every catalog exploit that targets this profile.
        for ex_id in ctx.profile.known_software_exploit_ids:
            try:
                exploit = catalog.get(ex_id)
            except KeyError:
                log.warning("Profile references unknown exploit", extra={"id": ex_id})
                continue
            try:
                log.info("Applying exploit", extra={"id": ex_id})
                await exploit.apply(client)
                isn = await client.read_data_by_identifier(ctx.profile.uds_isn_did)
                return self._verified(isn, used_exploit=ex_id)
            except Exception as e:
                log.info("Exploit ineffective", extra={"id": ex_id, "err": str(e)})

        # Last resort: force boot mode then try again.
        if await boot_mode.force_boot_mode(client):
            try:
                isn = await client.read_data_by_identifier(ctx.profile.uds_isn_did)
                return self._verified(isn, used_exploit="boot_mode")
            except Exception as e:
                return StrategyResult(
                    outcome=StrategyOutcome.FAILED_ROLLED_BACK,
                    strategy_name=self.name,
                    error_code="ISN_READ_AFTER_BOOT_FAILED",
                    error_message=str(e),
                )

        return StrategyResult(
            outcome=StrategyOutcome.FAILED_ROLLED_BACK,
            strategy_name=self.name,
            error_code="SOFTWARE_PATHS_EXHAUSTED",
            error_message="All software exploits ineffective for this ECU",
        )

    async def inject_isn(self, ctx: StrategyContext) -> StrategyResult:
        if ctx.target_isn is None or len(ctx.target_isn) != ISN_LENGTH:
            raise IsnMismatch("target_isn missing or wrong length")
        client = UdsClient(ctx.transport, ecu_addr=ctx.profile.uds_isn_did >> 8,
                           session_name="sw_inject")
        await client.diagnostic_session_control(DiagSession.PROGRAMMING)
        await ctx.security.unlock(vin=ctx.vin)

        async def dump() -> bytes:
            try:
                return await client.read_data_by_identifier(ctx.profile.uds_isn_did)
            except Exception:
                return bytes(ISN_LENGTH)

        pre = await ctx.preflight.check(
            vin=ctx.vin, ecu_name=ctx.profile.name, memory_region="EEPROM",
            dump_callable=dump, write_kind="coding",
        )
        await client.write_data_by_identifier(ctx.profile.uds_isn_did, ctx.target_isn)
        readback = await client.read_data_by_identifier(ctx.profile.uds_isn_did)
        if readback != ctx.target_isn:
            return StrategyResult(
                outcome=StrategyOutcome.FAILED_ROLLED_BACK,
                strategy_name=self.name,
                error_code="ISN_VERIFY_FAILED",
                error_message="read-back differs from injected",
                backup_sha256=pre.backup_sha,
            )
        return StrategyResult(
            outcome=StrategyOutcome.SUCCESS,
            strategy_name=self.name,
            isn=ctx.target_isn,
            backup_sha256=pre.backup_sha,
        )

    async def rollback(self, ctx: StrategyContext, *, reason: str) -> StrategyResult:
        """Software-only cleanup is mostly returning the ECU to default session."""
        try:
            client = UdsClient(ctx.transport, ecu_addr=ctx.profile.uds_isn_did >> 8,
                               session_name="sw_rollback")
            await client.diagnostic_session_control(DiagSession.DEFAULT)
            await client.ecu_reset(0x01)
        except Exception as e:
            log.warning("Software rollback raised", extra={"err": str(e)})
        return StrategyResult(
            outcome=StrategyOutcome.FAILED_ROLLED_BACK,
            strategy_name=self.name, error_code="ROLLBACK", error_message=reason,
        )

    # --- helpers ----------------------------------------------------------
    @staticmethod
    def _verified(isn: bytes, *, used_exploit: str = "") -> StrategyResult:
        if len(isn) != ISN_LENGTH:
            raise IsnMismatch(f"Got {len(isn)}-byte ISN, expected {ISN_LENGTH}")
        if all(b == 0 for b in isn) or all(b == 0xFF for b in isn):
            raise IsnMismatch("Virgin ISN — refuse")
        return StrategyResult(
            outcome=StrategyOutcome.SUCCESS, strategy_name="software_only",
            isn=isn, diagnostics={"exploit": used_exploit} if used_exploit else {},
        )
