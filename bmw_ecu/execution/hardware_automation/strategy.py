"""HardwareAutomationStrategy — glitch ECU into BSL, then read/write via UDS."""
from __future__ import annotations

from ...exceptions import IsnMismatch
from ...isn.extractor import ISN_LENGTH
from ...logging_setup import get_logger
from ...uds.client import UdsClient
from ...uds.services import DiagSession
from ..base import ExecutionStrategy, StrategyContext, StrategyOutcome, StrategyResult
from . import pin_maps
from .boot_pin_sequencer import run_sequence, standard_bsl_sequence
from .hal import SmartBoxHAL

log = get_logger(__name__)


class HardwareAutomationStrategy(ExecutionStrategy):
    name = "hardware_automation"
    requires_hardware_box = True

    def __init__(self, hal: SmartBoxHAL) -> None:
        self.hal = hal

    async def extract_isn(self, ctx: StrategyContext) -> StrategyResult:
        pm = pin_maps.lookup(ctx.profile.name)
        try:
            async with self.hal:
                await run_sequence(self.hal, standard_bsl_sequence(pm))
                # Now the ECU is in BSL — UDS over the same transport must
                # respond on the bootloader address. Read ISN.
                client = UdsClient(ctx.transport,
                                   ecu_addr=ctx.profile.uds_isn_did >> 8,
                                   session_name="hw_extract")
                await client.diagnostic_session_control(DiagSession.PROGRAMMING)
                await ctx.security.unlock(vin=ctx.vin)
                isn = await client.read_data_by_identifier(ctx.profile.uds_isn_did)
        except Exception as e:
            return StrategyResult(
                outcome=StrategyOutcome.FAILED_ROLLED_BACK,
                strategy_name=self.name,
                error_code="HW_EXTRACT_FAILED", error_message=str(e),
            )
        if len(isn) != ISN_LENGTH:
            raise IsnMismatch(f"Got {len(isn)} bytes from BSL")
        return StrategyResult(
            outcome=StrategyOutcome.SUCCESS, strategy_name=self.name, isn=isn,
        )

    async def inject_isn(self, ctx: StrategyContext) -> StrategyResult:
        if ctx.target_isn is None or len(ctx.target_isn) != ISN_LENGTH:
            raise IsnMismatch("target_isn missing or wrong length")
        pm = pin_maps.lookup(ctx.profile.name)
        try:
            async with self.hal:
                await run_sequence(self.hal, standard_bsl_sequence(pm))
                client = UdsClient(ctx.transport,
                                   ecu_addr=ctx.profile.uds_isn_did >> 8,
                                   session_name="hw_inject")
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
                await client.write_data_by_identifier(ctx.profile.uds_isn_did,
                                                     ctx.target_isn)
                readback = await client.read_data_by_identifier(ctx.profile.uds_isn_did)
                if readback != ctx.target_isn:
                    return StrategyResult(
                        outcome=StrategyOutcome.FAILED_ROLLED_BACK,
                        strategy_name=self.name,
                        error_code="ISN_VERIFY_FAILED",
                        error_message="read-back differs after BSL write",
                        backup_sha256=pre.backup_sha,
                    )
        except Exception as e:
            return StrategyResult(
                outcome=StrategyOutcome.FAILED_ROLLED_BACK,
                strategy_name=self.name,
                error_code="HW_INJECT_FAILED", error_message=str(e),
            )
        return StrategyResult(
            outcome=StrategyOutcome.SUCCESS, strategy_name=self.name,
            isn=ctx.target_isn, backup_sha256=pre.backup_sha,
        )

    async def rollback(self, ctx: StrategyContext, *, reason: str) -> StrategyResult:
        # The HAL's __aexit__ already calls all_off(), but if we crashed
        # outside the context manager we still need to drop rails.
        try:
            await self.hal.all_off()
            await self.hal.close()
        except Exception as e:
            log.warning("HW rollback raised", extra={"err": str(e)})
        return StrategyResult(
            outcome=StrategyOutcome.FAILED_ROLLED_BACK,
            strategy_name=self.name, error_code="ROLLBACK", error_message=reason,
        )
