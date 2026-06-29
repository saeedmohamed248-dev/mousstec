"""RealUniversalEcuIo — the bridge from the orchestrator to physical hardware.

`MockUniversalEcuIo` lets the whole UniversalSmartOrchestrator run in tests and
the simulator. This module is its production counterpart: it routes the
orchestrator's high-level operations down a live `UdsClient` over the real
ENET (DoIP) / K+DCAN transport.

DESIGN GUARANTEES (same spirit as the seed-key providers):

  • SAFE FAILURE — every transport/UDS/socket error (timeouts, dropped links,
    negative responses) is translated into `UniversalIoError`. The orchestrator
    catches that and lands in a FAILED state that still offers rollback. We
    never let a raw socket exception escape mid-flow and leave the ECU in an
    ambiguous state.

  • NEVER FABRICATE — operations that require a proprietary coding map
    (interpreting feature *options* into CAFD/FDL bytes) are NOT invented here.
    `code_dme` only writes bytes the caller supplied explicitly
    (`raw_coding_hex`), exactly like the DLL seed-key provider only returns a
    key the licensed library computed. If asked to "code" without real bytes,
    it refuses with a clear message rather than guessing.

  • SYMMETRIC ROLLBACK — `read_coding_snapshot` / `write_coding_snapshot` are a
    matched pair over a single configured coding DID, so the snapshot we back
    up is exactly what we can write back. No transform, no guessing.

  • NO GUESSED PINS — `bench_pinout` returns only what the DB hardware catalog
    actually has (passed in by the caller); unknown ⇒ None, and the
    orchestrator tells the tech to register the board in admin.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from ..exceptions import (
    BmwEcuError,
    SecurityAccessDenied,
    UdsNegativeResponse,
)
from ..logging_setup import get_logger
from ..uds.client import UdsClient
from ..uds.security_access import SecurityAccess
from .provider import AbstractUniversalEcuIo, UniversalIoError

log = get_logger(__name__)

# Standard UDS DID for the VIN (ISO 14229 / BMW alike).
_VIN_DID = 0xF190

# Errors that mean "the wire/ECU misbehaved" — all funnel to UniversalIoError
# so the orchestrator fails safe instead of crashing mid-flow.
_WIRE_ERRORS = (
    asyncio.TimeoutError,
    TimeoutError,
    OSError,             # socket reset / connection refused / broken pipe
    ConnectionError,
    UdsNegativeResponse,
    SecurityAccessDenied,
    BmwEcuError,
)


class RealUniversalEcuIo(AbstractUniversalEcuIo):
    """Live-hardware implementation of the orchestrator's I/O contract.

    Args:
        client:        an open `UdsClient` bound to the live transport.
        security:      `SecurityAccess` (for unlocking before a coding write).
        transport_kind: the kind the ConnectionManager actually connected with
                        ('doip' | 'kdcan' | 'socketcan') — detection already
                        happened at connect time, we just report it.
        coding_did:    the DID that holds the coding/ISN block for THIS ecu.
                        Required for backup/restore; if unknown we refuse to
                        guess one (the whole rollback contract depends on it).
        coding_level:  security level to unlock before writing coding.
        pinout:        confirmed bench pinout from the DB catalog, or None.
    """

    def __init__(self, *, client: UdsClient, security: SecurityAccess,
                 transport_kind: str,
                 coding_did: Optional[int] = None,
                 coding_level: Optional[int] = None,
                 vin: Optional[str] = None,
                 pinout: Optional[dict[str, Any]] = None) -> None:
        self._client = client
        self._security = security
        self._transport_kind = transport_kind
        self._coding_did = coding_did
        self._coding_level = coding_level
        self._vin_hint = vin
        self._pinout = pinout

    # -- error funnel ------------------------------------------------------
    @staticmethod
    def _wrap(op: str, exc: Exception) -> UniversalIoError:
        return UniversalIoError(f"{op} failed on live hardware: {exc!r}")

    # -- auto-detect -------------------------------------------------------
    async def detect_transport(self) -> str:
        # The active transport kind is decided when ConnectionManager.connect
        # picks a link; we simply surface it. No I/O ⇒ nothing to fail.
        return self._transport_kind

    async def read_vin(self) -> str:
        if self._vin_hint:
            return self._vin_hint
        try:
            raw = await self._client.read_data_by_identifier(_VIN_DID)
        except _WIRE_ERRORS as e:
            raise self._wrap("read_vin", e) from e
        # VIN is ASCII; tolerate trailing padding/nulls.
        return raw.decode("ascii", "ignore").strip("\x00 ").strip()

    async def probe_dme_locked(self) -> bool:
        """Dynamically probe lock state via a Security Access seed request.

        Per UDS, an *already-unlocked* ECU answers a seed request with an
        all-zero seed. A non-zero seed ⇒ locked. Anything that errors out we
        treat as LOCKED — the conservative, safe assumption (it routes to the
        bench path rather than attempting a direct write we may not be allowed
        to make).
        """
        level = (self._coding_level
                 if self._coding_level is not None
                 else self._security.provider.security_level)
        try:
            resp = await self._client.raw_request(bytes([0x27, level]))
        except _WIRE_ERRORS:
            # Denied / no answer ⇒ assume locked (route to bench, never force).
            return True
        # resp = [0x67, level, seed...]
        if len(resp) < 3 or resp[0] != 0x67:
            return True
        seed = resp[2:]
        return not all(b == 0 for b in seed)

    # -- backup / restore (the rollback contract) --------------------------
    async def read_coding_snapshot(self) -> bytes:
        if self._coding_did is None:
            raise UniversalIoError(
                "No coding-region DID is configured for this ECU, so there is "
                "nothing safe to back up. Register the coding DID for this "
                "profile (admin / EcuHardwareProfile) before continuing — we "
                "never guess memory regions.")
        try:
            return bytes(await self._client.read_data_by_identifier(self._coding_did))
        except _WIRE_ERRORS as e:
            raise self._wrap("read_coding_snapshot", e) from e

    async def write_coding_snapshot(self, data: bytes) -> None:
        """Write previously-saved bytes back (rollback / restore).

        This is the EXACT bytes we read in `read_coding_snapshot`, so it is a
        genuine, non-fabricated restore. Requires a security unlock first.
        """
        if self._coding_did is None:
            raise UniversalIoError(
                "Cannot restore: no coding-region DID configured for this ECU.")
        try:
            await self._security.unlock(vin=self._vin_hint, level=self._coding_level)
            await self._client.write_data_by_identifier(self._coding_did, bytes(data))
        except _WIRE_ERRORS as e:
            raise self._wrap("write_coding_snapshot", e) from e

    # -- the actual work ---------------------------------------------------
    async def clear_dtcs(self) -> dict[str, Any]:
        """Live ClearDiagnosticInformation (0x14, all DTCs) before coding.

        Best-effort: a DME that rejects the clear (NRC) must not abort the
        coding flow, so we report supported=False instead of raising. This is
        a real wire operation — no fabricated success.
        """
        try:
            await self._client.clear_diagnostic_information()
        except _WIRE_ERRORS:
            return {"cleared": False, "supported": False}
        return {"cleared": True, "supported": True}

    async def code_dme(self, options: dict[str, Any]) -> dict[str, Any]:
        """Apply coding to the DME.

        We do NOT interpret feature *options* into coding bytes here — that map
        is proprietary and inventing it would risk bricking an ECU. Instead we
        accept exact, caller-supplied coding bytes via ``options['raw_coding_hex']``
        (produced upstream by E-Sys / the licensed DLL). Anything else is
        refused, loudly, rather than guessed.
        """
        raw_hex = (options or {}).get("raw_coding_hex")
        if not raw_hex:
            raise UniversalIoError(
                "Real DME coding needs exact coding bytes — pass "
                "options['raw_coding_hex'] produced by E-Sys / the licensed "
                "tool. This bridge does not synthesise CAFD/FDL bytes from "
                "feature flags (we never guess coding).")
        try:
            payload = bytes.fromhex(raw_hex)
        except ValueError as e:
            raise UniversalIoError(f"raw_coding_hex is not valid hex: {e}") from e
        if self._coding_did is None:
            raise UniversalIoError("Cannot code: no coding-region DID configured.")
        try:
            await self._security.unlock(vin=self._vin_hint, level=self._coding_level)
            await self._client.write_data_by_identifier(self._coding_did, payload)
        except _WIRE_ERRORS as e:
            raise self._wrap("code_dme", e) from e
        return {"coded_options": len((options or {}).get("options") or {}),
                "bytes_written": len(payload)}

    async def sync_module(self, module: str) -> dict[str, Any]:
        """Sync the paired body module (FEM/CAS) to the coded DME.

        The FEM/CAS pairing routine is module-and-ISN specific; it is not yet
        defined as a confirmed UDS routine in this bridge, so we refuse rather
        than pretend it synced. Wire a confirmed routine before enabling.
        """
        raise UniversalIoError(
            f"Real {module} sync routine is not yet wired on this bridge. "
            f"Define the confirmed pairing routine before enabling sync — "
            f"reporting a fake success here would be dangerous.")

    async def extract_bench(self) -> dict[str, Any]:
        """Locked path: bench read/flash once the harness is wired.

        Bench extraction is hardware-rig specific (boot mode, pin sequence) and
        is not driven over the OBD UdsClient. Refuse here; the bench rig
        integration is a separate, confirmed-data path.
        """
        raise UniversalIoError(
            "Bench extraction runs on the dedicated bench rig, not over the "
            "OBD UDS link. Use the bench harness path with confirmed pinout "
            "data — this bridge will not improvise a bench read.")

    # -- optional bench pinout (NEVER guessed) -----------------------------
    async def bench_pinout(self) -> Optional[dict[str, Any]]:
        return self._pinout
