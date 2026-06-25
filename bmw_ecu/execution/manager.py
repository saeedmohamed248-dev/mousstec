"""ExecutionStrategyManager — Factory + selection rules + fallback chain.

Selection logic (deterministic, testable):

    requires_bench AND has_bdm           → InteractiveGuided (tech runs BDM)
    profile supports software_only       → SoftwareOnly preferred
    has_smart_breakout_box               → HardwareAutomation preferred
    can_run_interactive_guided           → InteractiveGuided fallback
    nothing                              → raise NoStrategyAvailable

The Manager hands the chosen strategy back to the caller, then orchestrates
extract → inject → cross-strategy rollback through RollbackCoordinator.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..exceptions import BmwEcuError
from ..logging_setup import get_logger
from .base import ExecutionStrategy, StrategyContext, StrategyResult, StrategyOutcome
from .ecu_profiles import EcuProfile, ProtectionLevel
from .rollback_coordinator import RollbackCoordinator

log = get_logger(__name__)


class NoStrategyAvailable(BmwEcuError):
    code = "NO_STRATEGY_AVAILABLE"


@dataclass
class StrategyChoice:
    primary: ExecutionStrategy
    fallbacks: tuple[ExecutionStrategy, ...]
    reason: str


class ExecutionStrategyManager:
    """Stateless. Construct once per request with the three strategy instances."""

    def __init__(self,
                 software_only: ExecutionStrategy,
                 hardware_automation: ExecutionStrategy,
                 interactive_guided: ExecutionStrategy) -> None:
        self._sw = software_only
        self._hw = hardware_automation
        self._wizard = interactive_guided
        self._rollback = RollbackCoordinator()

    # --- Selection --------------------------------------------------------
    def select(self, ctx: StrategyContext) -> StrategyChoice:
        log.info("Strategy selection begin", extra={
            "ecu": ctx.profile.name, "protection": ctx.profile.protection.name,
        })

        chain: list[ExecutionStrategy] = []
        reasons: list[str] = []

        # 1. requires_bench (e.g. MEVD17 N20) and we have the probe → wizard.
        if ctx.profile.requires_bench and ctx.capabilities.has_bdm_probe:
            chain.append(self._wizard)
            reasons.append("ECU requires bench probe; wizard guides BDM session")

        # 2. ECU is software-defeatable AND we have a transport → software preferred.
        if ctx.profile.supports_software_only() and ctx.capabilities.can_run_software_only():
            chain.append(self._sw)
            reasons.append(f"software exploit available for {ctx.profile.name}")

        # 3. Smart Box present → great deterministic option.
        if ctx.capabilities.can_run_hardware_automation():
            chain.append(self._hw)
            reasons.append("Mousstec Smart Box available")

        # 4. Interactive wizard is the universal fallback when a tech is qualified.
        if ctx.capabilities.can_run_interactive_guided() and self._wizard not in chain:
            chain.append(self._wizard)
            reasons.append("interactive guided as fallback")

        # Filter chain by is_eligible() so caller never gets a strategy that
        # will refuse its first call.
        eligible: list[ExecutionStrategy] = []
        for s in chain:
            ok, why = s.is_eligible(ctx)
            if ok:
                eligible.append(s)
            else:
                log.info("Strategy filtered", extra={"strategy": s.name, "why": why})

        if not eligible:
            raise NoStrategyAvailable(
                f"No execution strategy is viable for {ctx.profile.name} "
                f"with current workshop capabilities",
                profile=ctx.profile.name,
            )

        # HIGH/CRITICAL protection + no bench → force wizard to the front.
        if (ctx.profile.protection >= ProtectionLevel.HIGH
                and not ctx.capabilities.has_bdm_probe
                and self._wizard in eligible):
            eligible.sort(key=lambda s: 0 if s is self._wizard else 1)

        primary = eligible[0]
        log.info("Strategy selected", extra={
            "primary": primary.name,
            "fallbacks": [s.name for s in eligible[1:]],
        })
        return StrategyChoice(
            primary=primary,
            fallbacks=tuple(eligible[1:]),
            reason="; ".join(reasons),
        )

    # --- Orchestration ----------------------------------------------------
    async def run_extract_then_inject(self, ctx: StrategyContext,
                                      target_fem_ctx: Optional[StrategyContext] = None
                                      ) -> StrategyResult:
        """End-to-end: extract from source ECU, optionally inject into target FEM."""
        choice = self.select(ctx)
        strategy = choice.primary

        # Extract
        result = await self._with_fallback(
            choice, ctx, op="extract",
            run=lambda s: s.extract_isn(ctx),
        )
        if not result.succeeded and result.outcome != StrategyOutcome.SUSPENDED:
            return result
        if result.outcome == StrategyOutcome.SUSPENDED:
            return result  # Wizard — caller will resume later

        # Hand the extracted ISN to the target context for injection.
        if target_fem_ctx is not None and result.isn is not None:
            target_fem_ctx.target_isn = result.isn
            target_choice = self.select(target_fem_ctx)
            inject_result = await self._with_fallback(
                target_choice, target_fem_ctx, op="inject",
                run=lambda s: s.inject_isn(target_fem_ctx),
            )
            return inject_result

        return result

    async def _with_fallback(self, choice: StrategyChoice, ctx: StrategyContext,
                             *, op: str, run) -> StrategyResult:
        last: Optional[StrategyResult] = None
        for strategy in (choice.primary, *choice.fallbacks):
            try:
                log.info(f"{op} via {strategy.name}")
                result = await run(strategy)
                self._rollback.register(strategy, ctx)
                if result.succeeded or result.outcome == StrategyOutcome.SUSPENDED:
                    return result
                last = result
            except Exception as e:
                log.warning(f"{strategy.name} raised — trying fallback", extra={"err": str(e)})
                await self._rollback.rollback_all(ctx, reason=f"{strategy.name}: {e}")
                last = StrategyResult(
                    outcome=StrategyOutcome.FAILED_ROLLED_BACK,
                    strategy_name=strategy.name,
                    error_code=getattr(e, "code", "UNCAUGHT"),
                    error_message=str(e),
                )
                continue
        assert last is not None
        return last
