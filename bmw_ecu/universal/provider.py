"""Hardware-free I/O contract for the UniversalSmartOrchestrator.

Mirrors the design of `flashing.flash_provider`: the orchestrator drives an
ABSTRACT provider so the whole Plug-&-Play state machine — auto-detect,
auto-backup, code, sync, bench-extract, rollback — is unit-testable without a
single real OBD interface. Production wires a real UDS-backed provider; tests
use `MockUniversalEcuIo`.

Nothing here invents hardware data. The bench pinout, when offered, comes from
whatever the provider actually has (the DB hardware catalog / confirmed
profile) — never a guessed pin.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Optional


class UniversalIoError(Exception):
    """Any provider-side failure. The orchestrator turns this into a safe
    FAILED state with a rollback offer (when a backup already exists)."""


@dataclass
class DetectResult:
    """What auto-detect learned about the live vehicle."""
    transport_kind: str          # "doip" | "kdcan" | "socketcan"
    vin: str
    dme_locked: bool
    dme_family: str = "MEVD17"
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "transport_kind": self.transport_kind,
            "vin": self.vin,
            "dme_locked": self.dme_locked,
            "dme_family": self.dme_family,
            "notes": list(self.notes),
        }


class AbstractUniversalEcuIo(abc.ABC):
    """One live ECU session, abstracted to the operations the master flow needs."""

    # --- auto-detect ------------------------------------------------------
    @abc.abstractmethod
    async def detect_transport(self) -> str:
        """Return the active transport kind ('doip' | 'kdcan' | 'socketcan')."""

    @abc.abstractmethod
    async def read_vin(self) -> str: ...

    @abc.abstractmethod
    async def probe_dme_locked(self) -> bool:
        """Dynamically probe whether the DME is read/write protected."""

    # --- backup / restore (the rollback contract) -------------------------
    @abc.abstractmethod
    async def read_coding_snapshot(self) -> bytes:
        """Read the current coding/ISN state to back up BEFORE any write."""

    @abc.abstractmethod
    async def write_coding_snapshot(self, data: bytes) -> None:
        """Write a previously-saved snapshot back (rollback / restore)."""

    # --- the actual work --------------------------------------------------
    @abc.abstractmethod
    async def code_dme(self, options: dict[str, Any]) -> dict[str, Any]:
        """Apply coding (CAFD/VO/FDL) to the DME. Returns a summary."""

    @abc.abstractmethod
    async def sync_module(self, module: str) -> dict[str, Any]:
        """Sync a paired body module (FEM/CAS) to the coded DME."""

    @abc.abstractmethod
    async def extract_bench(self) -> dict[str, Any]:
        """Locked path: bench-extract / flash once the harness is wired."""

    # --- optional bench pinout (NEVER guessed) ----------------------------
    async def bench_pinout(self) -> Optional[dict[str, Any]]:
        """Confirmed bench pinout for the locked path, or None if unknown.

        Default None — a provider returns a real pinout only when it has
        confirmed data. The orchestrator then tells the tech to register the
        board in admin rather than inventing pins.
        """
        return None


class MockUniversalEcuIo(AbstractUniversalEcuIo):
    """Deterministic in-memory provider for tests / the simulator.

    Scriptable: choose the transport, lock state, and whether any step should
    raise, then assert on the recorded call log and the restored snapshot.
    """

    def __init__(self, *, transport_kind: str = "doip",
                 vin: str = "WBAUNIVERSAL00001",
                 dme_locked: bool = False,
                 snapshot: bytes = b"\xCA\xFD\x00\x01ORIGINAL",
                 pinout: Optional[dict[str, Any]] = None,
                 fail_on: Optional[str] = None) -> None:
        self._transport_kind = transport_kind
        self._vin = vin
        self._dme_locked = dme_locked
        self._live_snapshot = snapshot
        self._pinout = pinout
        self._fail_on = fail_on
        # Observability for assertions.
        self.calls: list[str] = []
        self.restored_with: Optional[bytes] = None

    def _maybe_fail(self, op: str) -> None:
        self.calls.append(op)
        if self._fail_on == op:
            raise UniversalIoError(f"injected failure at {op}")

    async def detect_transport(self) -> str:
        self._maybe_fail("detect_transport")
        return self._transport_kind

    async def read_vin(self) -> str:
        self._maybe_fail("read_vin")
        return self._vin

    async def probe_dme_locked(self) -> bool:
        self._maybe_fail("probe_dme_locked")
        return self._dme_locked

    async def read_coding_snapshot(self) -> bytes:
        self._maybe_fail("read_coding_snapshot")
        return self._live_snapshot

    async def write_coding_snapshot(self, data: bytes) -> None:
        self._maybe_fail("write_coding_snapshot")
        self.restored_with = bytes(data)
        self._live_snapshot = bytes(data)

    async def code_dme(self, options: dict[str, Any]) -> dict[str, Any]:
        self._maybe_fail("code_dme")
        # Simulate the live state changing after coding.
        self._live_snapshot = b"\xCA\xFD\x00\x02CODED"
        return {"coded_options": len(options or {}), "options": dict(options or {})}

    async def sync_module(self, module: str) -> dict[str, Any]:
        self._maybe_fail("sync_module")
        return {"module": module, "synced": True}

    async def extract_bench(self) -> dict[str, Any]:
        self._maybe_fail("extract_bench")
        return {"extracted": True, "image_size": 2048}

    async def bench_pinout(self) -> Optional[dict[str, Any]]:
        return self._pinout
