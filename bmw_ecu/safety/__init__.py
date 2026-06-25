"""Safety / failsafe layer — the un-brickable guarantee.

Every write goes through `PreflightGate` (battery + backup) and is wrapped
in `RollbackGuard`. If you bypass either, you're on your own.
"""
from .battery import BatteryMonitor  # noqa: F401
from .backup import BackupStore, EcuBackup  # noqa: F401
from .preflight import PreflightGate  # noqa: F401
from .rollback import RollbackGuard  # noqa: F401
