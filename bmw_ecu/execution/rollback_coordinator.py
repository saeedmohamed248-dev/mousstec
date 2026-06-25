"""Cross-strategy rollback coordinator.

If we tried SoftwareOnly, it half-succeeded, and we're falling back to
HardwareAutomation, the software side may have left the ECU in
programming session or with a partial security unlock. The coordinator
remembers every strategy that touched the ECU and rolls each back in
reverse order before the next strategy starts.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..logging_setup import get_logger

if TYPE_CHECKING:
    from .base import ExecutionStrategy, StrategyContext

log = get_logger(__name__)


class RollbackCoordinator:
    def __init__(self) -> None:
        self._stack: list["ExecutionStrategy"] = []

    def register(self, strategy: "ExecutionStrategy", ctx: "StrategyContext") -> None:
        if strategy not in self._stack:
            self._stack.append(strategy)

    async def rollback_all(self, ctx: "StrategyContext", *, reason: str) -> None:
        log.warning("Cross-strategy rollback begin", extra={"reason": reason})
        # Reverse order — most recent first.
        while self._stack:
            s = self._stack.pop()
            try:
                await s.rollback(ctx, reason=reason)
            except Exception as e:
                log.error(f"{s.name} rollback raised — continuing",
                          extra={"err": str(e)})
        log.info("Cross-strategy rollback done")
