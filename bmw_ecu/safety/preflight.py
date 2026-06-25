"""Pre-flight gate — runs before every write-class operation.

If any check fails, raises a SafetyAbort subclass. Callers MUST NOT catch
SafetyAbort to "continue anyway" — the whole point is to be un-brickable.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..exceptions import BackupRequired
from ..logging_setup import get_logger
from .backup import BackupStore, EcuBackup
from .battery import BatteryMonitor

log = get_logger(__name__)


@dataclass
class PreflightResult:
    battery_volts: float
    backup_sha: str


class PreflightGate:
    def __init__(self, battery: BatteryMonitor, store: BackupStore) -> None:
        self.battery = battery
        self.store = store

    async def check(self, *, vin: str, ecu_name: str, memory_region: str,
                    dump_callable, write_kind: str = "flash") -> PreflightResult:
        """Run all gates. `dump_callable` is an async fn returning bytes."""
        log.info("Pre-flight start", extra={"vin": vin, "ecu": ecu_name, "kind": write_kind})

        if write_kind == "flash":
            reading = await self.battery.assert_flash_safe()
        else:
            reading = await self.battery.assert_diag_safe()

        data = await dump_callable()
        if not data:
            raise BackupRequired(f"Empty dump from {ecu_name} — refusing to write")
        backup = EcuBackup(vin=vin, ecu_name=ecu_name,
                           memory_region=memory_region, data=data,
                           metadata={"phase": "pre_write", "kind": write_kind})
        self.store.save(backup)
        log.info("Pre-flight ok", extra={"sha": backup.sha256[:12], "v": reading.volts})
        return PreflightResult(battery_volts=reading.volts, backup_sha=backup.sha256)
