"""Hardware-free flash transport for the guided orchestrator.

The guided `FlashOrchestrator` talks to the ECU exclusively through this
ABC, so the whole flash flow — including the rollback path — is testable
with the deterministic `MockFlashProvider`, no bus, no risk.

The method set mirrors the UDS programming sequence the real `FlashEngine`
runs, but split into the discrete, awaitable steps the orchestrator
sequences and can fail/rollback between:

  enter_programming_session → unlock_security → read_backup → erase →
  request_download → transfer_block (×N) → request_transfer_exit →
  check_dependencies → ecu_reset
        … and restore_backup() for the rollback path.

`read_backup` is FIRST-class: the orchestrator refuses to erase before it
holds a verified backup, and `restore_backup` is what makes a failed
flash recoverable instead of a brick.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional


class FlashTransportError(Exception):
    """Bus-level failure mid-flash (module stopped answering)."""


class FlashSecurityDenied(Exception):
    """Security access rejected before programming."""


class FlashRejected(Exception):
    """The ECU refused a programming step (erase / download / transfer)."""


class FlashDependencyError(Exception):
    """check_dependencies failed — the ECU will not accept the new image."""


@dataclass
class FlashBackup:
    ecu_name: str
    vin: str
    origin_addr: int
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


class AbstractFlashProvider(abc.ABC):
    @abc.abstractmethod
    async def read_current_version(self) -> str: ...

    @abc.abstractmethod
    async def enter_programming_session(self) -> None: ...

    @abc.abstractmethod
    async def unlock_security(self, *, vin: str) -> None: ...

    @abc.abstractmethod
    async def read_backup(self, *, addr: int, size: int) -> FlashBackup:
        """Read the current flash region so it can be restored on failure."""

    @abc.abstractmethod
    async def erase(self, *, addr: int) -> None: ...

    @abc.abstractmethod
    async def request_download(self, *, addr: int, size: int) -> int:
        """Negotiate the download; return the max block length the ECU
        will accept per transfer_block call."""

    @abc.abstractmethod
    async def transfer_block(self, *, seq: int, data: bytes) -> None: ...

    @abc.abstractmethod
    async def request_transfer_exit(self) -> None: ...

    @abc.abstractmethod
    async def check_dependencies(self) -> None:
        """ECU-side acceptance of the freshly written image."""

    @abc.abstractmethod
    async def ecu_reset(self) -> None: ...

    @abc.abstractmethod
    async def restore_backup(self, backup: FlashBackup) -> None:
        """Re-write the saved image — the rollback path. MUST be safe to
        call after a partial/failed flash."""


# ─────────────────────────────────────────────────────────────────────
@dataclass
class MockFlashProvider(AbstractFlashProvider):
    """Deterministic test double.

    Config:
      • bus_down        — enter_programming_session raises.
      • deny_security   — unlock_security raises.
      • fail_on         — step name that raises FlashRejected
                          ('erase' | 'request_download' | 'transfer' |
                           'transfer_exit').
      • fail_dependencies — check_dependencies raises FlashDependencyError.
      • backup_data     — bytes returned by read_backup.
      • current_version / new_version — version strings.
      • max_block_len   — block size negotiated by request_download.

    Records every call so tests can assert ordering + rollback.
    """
    bus_down: bool = False
    deny_security: bool = False
    fail_on: str = ""
    fail_dependencies: bool = False
    backup_data: bytes = b"\xFF" * 64
    current_version: str = "SW_01"
    new_version: str = "SW_02"
    max_block_len: int = 0x400

    version_reads: int = 0
    session_calls: int = 0
    security_calls: list[str] = field(default_factory=list)
    backup_calls: list[tuple[int, int]] = field(default_factory=list)
    erase_calls: list[int] = field(default_factory=list)
    download_calls: list[tuple[int, int]] = field(default_factory=list)
    transfer_calls: list[tuple[int, int]] = field(default_factory=list)  # (seq, len)
    exit_calls: int = 0
    dependency_calls: int = 0
    reset_calls: int = 0
    restore_calls: list[int] = field(default_factory=list)  # backup sizes
    # Flips to True once the live image differs from the backup; the
    # rollback resets it so a test can prove the ECU was made whole again.
    image_written: bool = False

    async def read_current_version(self) -> str:
        self.version_reads += 1
        # After a committed flash the version reflects the new image.
        return self.new_version if self.image_written else self.current_version

    async def enter_programming_session(self) -> None:
        self.session_calls += 1
        if self.bus_down:
            raise FlashTransportError(
                "الوحدة مش بترد على جلسة البرمجة — اتأكد من الشاحن والباص."
            )

    async def unlock_security(self, *, vin: str) -> None:
        self.security_calls.append(vin)
        if self.deny_security:
            raise FlashSecurityDenied(
                "الوحدة رفضت security access قبل البرمجة."
            )

    async def read_backup(self, *, addr: int, size: int) -> FlashBackup:
        self.backup_calls.append((addr, size))
        return FlashBackup(ecu_name="mock", vin="", origin_addr=addr,
                           data=bytes(self.backup_data))

    async def erase(self, *, addr: int) -> None:
        self.erase_calls.append(addr)
        if self.fail_on == "erase":
            raise FlashRejected(f"الوحدة رفضت مسح المنطقة 0x{addr:08X}.")
        # Erase makes the live image diverge from the backup.
        self.image_written = True

    async def request_download(self, *, addr: int, size: int) -> int:
        self.download_calls.append((addr, size))
        if self.fail_on == "request_download":
            raise FlashRejected("الوحدة رفضت RequestDownload.")
        return self.max_block_len

    async def transfer_block(self, *, seq: int, data: bytes) -> None:
        self.transfer_calls.append((seq, len(data)))
        if self.fail_on == "transfer":
            raise FlashRejected(f"الوحدة رفضت بلوك التحويل رقم {seq}.")

    async def request_transfer_exit(self) -> None:
        self.exit_calls += 1
        if self.fail_on == "transfer_exit":
            raise FlashRejected("الوحدة رفضت RequestTransferExit.")

    async def check_dependencies(self) -> None:
        self.dependency_calls += 1
        if self.fail_dependencies:
            raise FlashDependencyError(
                "فحص الاعتماد فشل — الوحدة مش هتقبل النسخة دي."
            )

    async def ecu_reset(self) -> None:
        self.reset_calls += 1

    async def restore_backup(self, backup: FlashBackup) -> None:
        self.restore_calls.append(backup.size)
        # The ECU is whole again — back on the original image.
        self.image_written = False
