"""ECU memory backup store.

Every write-class operation (coding, flashing, ISN injection) MUST take a
backup first. Backups are content-addressed (SHA-256) and stored both on
disk and in the Mousstec DB so a dead laptop doesn't lose the only copy.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..exceptions import BackupVerificationFailed
from ..logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class EcuBackup:
    vin: str
    ecu_name: str            # "FEM", "DME_N20", "EPS", "CAS4"
    memory_region: str       # "EEPROM", "NVRAM", "FLASH"
    data: bytes
    sha256: str = ""
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.sha256:
            self.sha256 = hashlib.sha256(self.data).hexdigest()

    def verify(self) -> bool:
        return hashlib.sha256(self.data).hexdigest() == self.sha256


class BackupStore:
    """Local disk store. The cloud_sync recorder mirrors to the DB."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, b: EcuBackup) -> Path:
        # vin/ecu/sha256.bin — content-addressed, immutable.
        d = self.root / b.vin / b.ecu_name
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{b.sha256}.bin"

    def save(self, backup: EcuBackup) -> Path:
        path = self._path(backup)
        if path.exists() and path.stat().st_size == len(backup.data):
            log.info("Backup already on disk (dedup)", extra={"sha": backup.sha256[:12]})
            return path
        path.write_bytes(backup.data)
        meta = {
            "vin": backup.vin, "ecu": backup.ecu_name, "region": backup.memory_region,
            "sha256": backup.sha256, "captured_at": backup.captured_at.isoformat(),
            "size": len(backup.data), "metadata": backup.metadata,
        }
        path.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        log.info("Backup saved", extra={"path": str(path), "size": len(backup.data)})
        return path

    def load(self, vin: str, ecu_name: str, sha256: str) -> Optional[EcuBackup]:
        path = self.root / vin / ecu_name / f"{sha256}.bin"
        meta_path = path.with_suffix(".json")
        if not path.exists() or not meta_path.exists():
            return None
        data = path.read_bytes()
        meta = json.loads(meta_path.read_text())
        b = EcuBackup(
            vin=vin, ecu_name=ecu_name, memory_region=meta["region"],
            data=data, sha256=sha256,
            captured_at=datetime.fromisoformat(meta["captured_at"]),
            metadata=meta.get("metadata", {}),
        )
        if not b.verify():
            raise BackupVerificationFailed(
                f"Stored backup {sha256[:12]} failed SHA verification — disk corruption?",
            )
        return b
