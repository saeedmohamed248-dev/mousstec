"""Safety layer: low-voltage abort + rollback guard."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from bmw_ecu.exceptions import FlashRolledBack, LowVoltage
from bmw_ecu.safety import BackupStore, BatteryMonitor, PreflightGate
from bmw_ecu.safety.backup import EcuBackup
from bmw_ecu.safety.rollback import RollbackGuard


class SafetyTests(unittest.TestCase):
    def test_low_voltage_blocks_flash(self) -> None:
        async def run() -> None:
            mon = BatteryMonitor(reader=lambda: _const(12.5))
            with self.assertRaises(LowVoltage):
                await mon.assert_flash_safe()

        asyncio.run(run())

    def test_backup_dedup_by_sha(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            store = BackupStore(Path(d))
            b = EcuBackup(vin="X", ecu_name="FEM", memory_region="EEPROM",
                          data=b"\x00" * 256)
            p1 = store.save(b)
            p2 = store.save(b)
            self.assertEqual(p1, p2)

    def test_rollback_invoked_on_failure(self) -> None:
        restored: list[EcuBackup] = []

        async def restore(b: EcuBackup) -> None:
            restored.append(b)

        async def run() -> None:
            backup = EcuBackup(vin="X", ecu_name="DME", memory_region="FLASH",
                               data=b"original")
            with self.assertRaises(FlashRolledBack):
                async with RollbackGuard(backup, restore_fn=restore):
                    raise RuntimeError("simulated flash failure")
            self.assertEqual(len(restored), 1)

        asyncio.run(run())

    def test_commit_skips_rollback(self) -> None:
        restored: list[EcuBackup] = []

        async def restore(b: EcuBackup) -> None:
            restored.append(b)

        async def run() -> None:
            backup = EcuBackup(vin="X", ecu_name="DME", memory_region="FLASH",
                               data=b"ok")
            async with RollbackGuard(backup, restore_fn=restore) as g:
                g.commit()
            self.assertEqual(restored, [])

        asyncio.run(run())


async def _const(v: float) -> float:
    return v
