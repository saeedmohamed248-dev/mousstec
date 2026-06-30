"""RealDmeSwapProvider — live-hardware backend for DmeSwapOrchestrator.

This is the production counterpart to ``MockDmeSwapProvider``. It composes the
real diagnostic primitives over ONE open transport (the new K-Line transport
through the pre-2007 E-series gateway, or any other AbstractTransport):

    read_cas_isn  → IsnExtractor over the CAS UDS client  (real, over the wire)
    backup_dme    → bench EEPROM/flash dump, persisted in BackupStore
    write_dme_isn → bench (BDM) write for MEVD17 (over_uds=False), or UDS write
    verify_dme_isn→ read-back compare
    align_ews     → EwsSync RoutineControl on DME + CAS

HONESTY (project policy — never fake, never guess), enforced here as REFUSALS:

  • No guessed addresses. The CAS and DME diagnostic addresses, the gateway
    KWP target, the CAS-ISN DID/level, and the E-series EWS-align routine ID
    are ALL caller/operator-supplied (see SwapAddressConfig / *_from_env). If a
    required value is missing we raise a clear error naming exactly what to
    provide — we never substitute a placeholder against a real car.

  • Unverified ISN specs are refused. The CAS3/CAS3+ ISN DID + security level
    in isn_map default to verified=False. read_cas_isn refuses to run an
    unverified spec unless the operator explicitly opts in with confirmed
    values (allow_unverified=True), so a guessed DID can never read garbage and
    be written into a DME as a "real" ISN.

  • MEVD17 DME ISN write is bench-only. over_uds=False ⇒ writing it via UDS is
    not possible; it is a Tricore BDM/boot operation on the dedicated bench
    harness. If no bench provider is wired, write/backup/verify REFUSE rather
    than pretend a UDS write succeeded.

  • Safe failure. Wire/UDS errors surface as SwapProviderError; the
    orchestrator catches it and lands in a FAILED state that still offers the
    backup for rollback.

──────────────────────────────────────────────────────────────────────────────
ENVIRONMENT VARIABLES — the real E-series Target IDs you must provide
──────────────────────────────────────────────────────────────────────────────
Transport (already used by ConnectionManager / KLineTransport):
    BMW_ECU_KLINE_PORT        FTDI serial device (e.g. /dev/cu.usbserial-A50285BI)
    BMW_ECU_KLINE_TARGET      KWP address of the GATEWAY on pin 7 (the bridge)

Swap addressing (this provider) — all hex like "0x40" accepted:
    BMW_ECU_SWAP_CAS_ADDR     UDS/KWP diagnostic address of the CAS3/CAS3+
    BMW_ECU_SWAP_DME_ADDR     UDS/KWP diagnostic address of the MEVD17 DME
    BMW_ECU_SWAP_CAS_ISN_DID  CONFIRMED CAS-ISN ReadDataByIdentifier DID
    BMW_ECU_SWAP_CAS_ISN_LEVEL CONFIRMED CAS SecurityAccess level for the ISN
    BMW_ECU_SWAP_EWS_ROUTINE  CONFIRMED E-series EWS-align RoutineControl ID
    BMW_ECU_SWAP_ALLOW_UNVERIFIED  set 1 ONLY when the DID/level above are the
                                   real confirmed values (opt-in safety override)

These are intentionally NOT defaulted: the gateway bridges the frames, but the
CAS/DME addresses and the ISN DID/level are per-platform and must be the values
you confirmed from the E90 ZGW / CAS3+ / MEVD17 documentation.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Protocol

from ..connection.base import AbstractTransport
from ..logging_setup import get_logger
from ..safety.backup import BackupStore, EcuBackup
from ..uds.client import UdsClient
from ..uds.seed_key_providers import AbstractSeedKeyProvider
from ..uds.security_access import SecurityAccess
from .dme_swap_orchestrator import AbstractDmeSwapProvider, ISN_LENGTH
from .ews_sync import EwsSync
from .extractor import IsnExtractor
from .isn_map import IsnAccessSpec, get_isn_spec

log = get_logger(__name__)


class SwapProviderError(RuntimeError):
    """Any live-hardware/config failure in the real swap provider.

    The orchestrator catches this and degrades to an honest FAILED prompt
    (which still surfaces the backup reference for rollback)."""


class SwapBenchHarness(Protocol):
    """The dedicated bench rig (Xprog / KESS / Trasdata) for Tricore DMEs
    whose ISN region is NOT UDS-writable (MEVD17). Supplied by the caller; we
    never improvise a bench read/write."""

    async def dump(self) -> bytes: ...
    async def write_isn(self, isn: bytes) -> None: ...


@dataclass(frozen=True)
class SwapAddressConfig:
    """Per-bench E-series addressing — every field caller-supplied, no guesses."""
    cas_ecu_addr: int                       # CAS3/CAS3+ diagnostic address
    dme_ecu_addr: int                       # MEVD17 DME diagnostic address
    cas_isn_spec: Optional[IsnAccessSpec] = None   # confirmed CAS ISN DID/level
    ews_routine_id: Optional[int] = None    # confirmed E-series EWS-align routine
    allow_unverified: bool = False          # opt-in once DID/level are confirmed


def _env_int(name: str) -> Optional[int]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return int(raw.strip(), 0)  # accepts "0x40" and "64"


def swap_address_config_from_env() -> SwapAddressConfig:
    """Build SwapAddressConfig from the BMW_ECU_SWAP_* env vars (see module
    docstring). Raises SwapProviderError naming any missing required address —
    we will not invent CAS/DME addresses."""
    cas = _env_int("BMW_ECU_SWAP_CAS_ADDR")
    dme = _env_int("BMW_ECU_SWAP_DME_ADDR")
    missing = [n for n, v in (("BMW_ECU_SWAP_CAS_ADDR", cas),
                              ("BMW_ECU_SWAP_DME_ADDR", dme)) if v is None]
    if missing:
        raise SwapProviderError(
            "Missing required E-series diagnostic address(es): "
            f"{', '.join(missing)}. Supply the confirmed CAS3/CAS3+ and MEVD17 "
            "DME addresses (per your ZGW/CAS/DME docs) — we never guess them.")

    cas_did = _env_int("BMW_ECU_SWAP_CAS_ISN_DID")
    cas_level = _env_int("BMW_ECU_SWAP_CAS_ISN_LEVEL")
    allow = (os.environ.get("BMW_ECU_SWAP_ALLOW_UNVERIFIED", "")
             .strip().lower() in ("1", "true", "yes", "on"))
    cas_spec: Optional[IsnAccessSpec] = None
    if cas_did is not None and cas_level is not None:
        # Operator-supplied confirmed spec → mark verified so the extractor runs.
        cas_spec = IsnAccessSpec(
            family="CAS", did=cas_did, security_level=cas_level,
            length=ISN_LENGTH, over_uds=True, verified=True,
            notes="Operator-confirmed CAS ISN spec from BMW_ECU_SWAP_* env.")

    return SwapAddressConfig(
        cas_ecu_addr=cas, dme_ecu_addr=dme,
        cas_isn_spec=cas_spec,
        ews_routine_id=_env_int("BMW_ECU_SWAP_EWS_ROUTINE"),
        allow_unverified=allow,
    )


class RealDmeSwapProvider(AbstractDmeSwapProvider):
    """Live backend: real CAS ISN read + EWS align over the transport, bench
    write for the Tricore DME. Refuses (never fakes) where confirmed data or a
    bench harness is missing."""

    def __init__(self, *, transport: AbstractTransport,
                 cas_seed_provider: AbstractSeedKeyProvider,
                 dme_seed_provider: AbstractSeedKeyProvider,
                 addr: SwapAddressConfig,
                 backup_store: BackupStore,
                 bench: Optional[SwapBenchHarness] = None) -> None:
        self._transport = transport
        self._addr = addr
        self._store = backup_store
        self._bench = bench

        self._cas_client = UdsClient(transport, ecu_addr=addr.cas_ecu_addr,
                                     session_name="swap-cas")
        self._dme_client = UdsClient(transport, ecu_addr=addr.dme_ecu_addr,
                                     session_name="swap-dme")
        self._cas_security = SecurityAccess(self._cas_client, cas_seed_provider)
        self._dme_security = SecurityAccess(self._dme_client, dme_seed_provider)
        self._cas_extractor = IsnExtractor(self._cas_client, self._cas_security)

    # ── 1. read the genuine ISN from the car's CAS (REAL, over the wire) ──
    async def read_cas_isn(self, *, vin: str, cas_family: str) -> bytes:
        spec = self._addr.cas_isn_spec or get_isn_spec(cas_family) or get_isn_spec("CAS")
        if spec is None:
            raise SwapProviderError(
                f"No ISN access spec for CAS family {cas_family!r}. Provide the "
                "confirmed DID/level via BMW_ECU_SWAP_CAS_ISN_DID/LEVEL.")
        if not spec.verified and not self._addr.allow_unverified:
            raise SwapProviderError(
                f"CAS ISN spec is unverified (DID 0x{spec.did:04X}, level "
                f"0x{spec.security_level:02X}). Confirm the CAS3/CAS3+ ISN "
                "DID + security level and set BMW_ECU_SWAP_CAS_ISN_DID/LEVEL "
                "(or BMW_ECU_SWAP_ALLOW_UNVERIFIED=1 once confirmed). Refusing "
                "to read a guessed DID and treat the result as a real ISN.")
        try:
            return await self._cas_extractor.extract(
                vin=vin, did=spec.did, security_level=spec.security_level,
                length=spec.length)
        except Exception as e:  # noqa: BLE001 — wire/UDS error → honest failure
            raise SwapProviderError(f"CAS ISN read failed on live hardware: {e!r}") from e

    # ── 2. full backup of the used DME BEFORE any write ──────────────────
    async def backup_dme(self, *, vin: str, dme_name: str) -> str:
        if self._bench is None:
            raise SwapProviderError(
                f"Backing up the used {dme_name} requires the bench harness "
                "(its ISN region is not UDS-readable). Wire a confirmed bench "
                "provider (Xprog/KESS/Trasdata) — refusing to skip the backup.")
        try:
            data = await self._bench.dump()
        except Exception as e:  # noqa: BLE001
            raise SwapProviderError(f"Bench DME dump failed: {e!r}") from e
        if not data:
            raise SwapProviderError("Bench DME dump returned no data — refusing to write.")
        backup = EcuBackup(vin=vin, ecu_name=dme_name,
                           memory_region="FLASH", data=bytes(data))
        self._store.save(backup)
        return backup.sha256

    # ── 3. write the car's ISN into the used DME ─────────────────────────
    async def write_dme_isn(self, *, vin: str, dme_name: str, isn: bytes,
                            requires_bench: bool) -> None:
        if len(isn) != ISN_LENGTH:
            raise SwapProviderError(
                f"ISN must be {ISN_LENGTH} bytes, got {len(isn)} — refusing to write.")
        if requires_bench:
            if self._bench is None:
                raise SwapProviderError(
                    f"{dme_name} ISN write is a bench/BDM operation (Tricore "
                    "flash, not UDS-writable). No bench harness is configured. "
                    "Wire a confirmed bench provider — refusing to fake a UDS "
                    "write that would not actually change the DME.")
            try:
                await self._bench.write_isn(bytes(isn))
            except Exception as e:  # noqa: BLE001
                raise SwapProviderError(f"Bench ISN write failed: {e!r}") from e
            return
        # UDS-writable DME path (NOT MEVD17): exact confirmed DID required.
        raise SwapProviderError(
            f"UDS ISN write for {dme_name} is not enabled without a confirmed, "
            "verified DME ISN DID/level. This path is intentionally locked to "
            "avoid guessing a writable DID on a real DME.")

    # ── 4. verify by read-back ───────────────────────────────────────────
    async def verify_dme_isn(self, *, vin: str, dme_name: str,
                             isn: bytes) -> bool:
        if self._bench is None:
            raise SwapProviderError(
                f"Cannot verify {dme_name} ISN without the bench harness "
                "(UDS read-back of the protected region is not available).")
        try:
            dump = await self._bench.dump()
        except Exception as e:  # noqa: BLE001
            raise SwapProviderError(f"Bench verify dump failed: {e!r}") from e
        # No guessed offset: confirm the exact 32-byte ISN landed somewhere in
        # the re-read image. (Exact-region verify needs a confirmed offset map.)
        return bytes(isn) in bytes(dump)

    # ── 5. EWS align DME↔CAS ─────────────────────────────────────────────
    async def align_ews(self, *, vin: str) -> None:
        if self._addr.ews_routine_id is None:
            raise SwapProviderError(
                "E-series EWS-align RoutineControl ID is not configured. The "
                "F-series default (0xAF11) is NOT valid for CAS3 — set "
                "BMW_ECU_SWAP_EWS_ROUTINE to the confirmed routine ID. Refusing "
                "to fire a guessed routine at the immobilizer.")
        ews = EwsSync(self._dme_client, self._cas_client,
                      routine_id=self._addr.ews_routine_id)
        try:
            await ews.synchronize()
        except Exception as e:  # noqa: BLE001
            raise SwapProviderError(f"EWS align failed on live hardware: {e!r}") from e
