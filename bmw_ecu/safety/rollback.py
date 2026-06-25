"""RollbackGuard — async context manager wrapping every write operation.

On exception inside the `with` block, the guard replays the pre-write
backup back into the ECU. If that fails too, raises FlashRollbackFailed
which the UI must treat as "STOP — do not power-cycle, call senior tech".
"""
from __future__ import annotations

from typing import Awaitable, Callable, Optional

from ..exceptions import FlashRolledBack, FlashRollbackFailed
from ..logging_setup import get_logger
from .backup import EcuBackup

log = get_logger(__name__)

RestoreCallable = Callable[[EcuBackup], Awaitable[None]]


class RollbackGuard:
    """Use as:

        async with RollbackGuard(backup, restore_fn=ecu.restore):
            await flasher.write(...)
    """

    def __init__(self, backup: EcuBackup, restore_fn: RestoreCallable) -> None:
        self._backup = backup
        self._restore = restore_fn
        self._committed = False

    def commit(self) -> None:
        """Mark the write as successful. Skips rollback on exit."""
        self._committed = True

    async def __aenter__(self) -> "RollbackGuard":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc is None or self._committed:
            return False  # do not suppress; nothing to do
        log.error("Write failed, beginning rollback",
                  extra={"ecu": self._backup.ecu_name, "err": str(exc)})
        try:
            await self._restore(self._backup)
        except Exception as re:
            log.critical("ROLLBACK FAILED — ECU in unknown state",
                         extra={"ecu": self._backup.ecu_name, "err": str(re)})
            raise FlashRollbackFailed(
                f"{self._backup.ecu_name}: rollback raised {re}",
            ) from re
        log.info("Rollback complete", extra={"ecu": self._backup.ecu_name})
        raise FlashRolledBack(
            f"{self._backup.ecu_name} write failed and was rolled back",
        ) from exc
