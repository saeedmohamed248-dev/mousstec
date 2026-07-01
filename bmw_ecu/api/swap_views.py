"""Used-DME swap endpoint — the DmeSwapOrchestrator over HTTP.

    POST /api/ecu/swap/step
        Body: {
          "session_id": "..."          # omit on the first call; returned to you
          "event": "select_profile" | "read_cas_isn" | "backup_dme" |
                   "write_dme_isn" | "bsl_start" | "verify" | "align" |
                   "finish" | "abort",
          "payload": { ... },           # event-specific
          # --- only needed on the FIRST call (event="select_profile") ---
          "profile_key": "R56_N18_MEVD17",
          "vin": "...",
          "transport": { "kind": "kline", "serial_port": "/dev/cu.usbserial-..." },
          "sim": { "uds_reject_nrc": 51 }   # simulator-only: demo the BSL divert
        }

    Response: { "session_id": "...", "prompt": { ...SwapPrompt.to_dict()... } }

This mirrors `smart_step` one-for-one: a forward-only state machine whose
snapshot is persisted in a `SwapSessionStore` between requests so the chatbot
UI can drive it click by click. Crucially, the paused `DME_BSL_FALLBACK` wizard
survives across requests (and a laptop sleep) because `uds_reject_nrc` + the
fallback state round-trip through snapshot/restore.

Simulator switch (`BMW_ECU_SIMULATOR=1`, honoring the production hardware lock)
swaps `RealDmeSwapProvider` for the deterministic `MockDmeSwapProvider` — same
orchestrator, no hardware. The mock's `sim` flags let the demo drive the exact
UDS→BSL fallback the technician sees on a real MEVD17.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from asgiref.sync import async_to_sync
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from ..connection.base import TransportConfig, TransportKind
from ..connection.manager import ConnectionManager
from ..exceptions import NoInterfaceDetected
from ..isn import (
    DME_SWAP_PROFILES,
    DmeSwapOrchestrator,
    MockDmeSwapProvider,
    RealDmeSwapProvider,
    SwapProviderError,
    SwapSessionRecord,
    SwapSessionStore,
    get_dme_swap_profile,
    swap_address_config_from_env,
)
from ..logging_setup import get_logger
from ..safety import BackupStore
from ..uds import resolve_seed_key_provider
from .runtime_mode import simulator_enabled

log = get_logger(__name__)

# The one profile the swap flow ships with today (the user's MINI R56 N18 case).
_DEFAULT_PROFILE = "R56_N18_MEVD17"
# The opener event: unlike the Smart flow's "start", the swap flow begins by
# selecting the DME/CAS profile. A missing session on any OTHER event is a 409.
_OPENER = "select_profile"


def _is_simulator() -> bool:
    # Single source of truth; honors the BMW_ECU_REQUIRE_HARDWARE production
    # lock so a stale SIMULATOR flag can never silently serve fake data.
    return simulator_enabled()


def _backup_store() -> BackupStore:
    root = os.environ.get(
        "BMW_ECU_BACKUP_ROOT", "/var/lib/mousstec/bmw_ecu/backups")
    return BackupStore(Path(root))


# --- Endpoint ---------------------------------------------------------------
@csrf_exempt
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def swap_step(request: Request) -> Response:
    body: dict[str, Any] = request.data or {}
    event = (body.get("event") or _OPENER).strip()
    store = SwapSessionStore()
    session_id = body.get("session_id") or store.new_session_id()
    record = store.load(session_id)

    if record is None and event != _OPENER:
        return Response(
            {"error": "session_not_found",
             "detail": "No active session — send event='select_profile' first."},
            status=status.HTTP_409_CONFLICT,
        )

    try:
        prompt_dict, record = async_to_sync(_drive)(session_id, event, body, record)
    except SwapProviderError as e:
        # Honest config/hardware refusal (e.g. missing confirmed CAS/DME address)
        # — NOT a crash and NEVER a silent fake. Tell the tech what to provide.
        log.warning("swap_step: provider refusal", extra={"detail": str(e)})
        return Response(
            {"error": "swap_not_configured",
             "detail_ar": "إعدادات الـ swap ناقصة على السيرفر. لازم عناوين الـ "
                          "CAS/DME والـ DID المؤكدة (BMW_ECU_SWAP_*). النظام مش "
                          "هيخمّن قيم على عربية حقيقية.",
             "detail_en": "Swap addressing is not configured. Provide the "
                          "confirmed CAS/DME addresses + ISN DID/level via the "
                          "BMW_ECU_SWAP_* env vars. The system will not guess.",
             "detail": str(e)},
            status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except NoInterfaceDetected as e:
        # Bench wired but no interface answered — honest, never a silent Mock.
        log.warning("swap_step: no interface", extra={"detail": str(e)})
        return Response(
            {"error": "hardware_not_found",
             "detail_ar": "مفيش جهاز/واجهة بترد. اتأكد إن كابل الـ K-Line/FTDI "
                          "متوصّل والجيتواي شغّال. النظام مش هيشتغل بالمحاكاة.",
             "detail_en": "No ECU interface responded. Check the K-Line/FTDI "
                          "link and the gateway. No fallback to simulation.",
             "detail": str(e)},
            status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except Exception as e:  # pragma: no cover - defensive top-level guard
        log.exception("swap_step crashed")
        return Response({"error": "internal", "detail": repr(e)},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    store.save(record)
    # Free the cache slot only when the job is truly finished. A FAILED prompt is
    # terminal for the current path but the tech may still want the backup ref,
    # so we let it expire by TTL rather than deleting it here.
    if prompt_dict.get("state") == "done":
        store.delete(session_id)
    return Response({"session_id": session_id, "prompt": prompt_dict})


# --- Orchestration driver ---------------------------------------------------
async def _drive(session_id: str, event: str, body: dict[str, Any],
                 record: Optional[SwapSessionRecord]):
    if record is None:
        record = SwapSessionRecord(
            session_id=session_id,
            vin=(body.get("vin") or "").strip(),
            profile_key=(body.get("profile_key") or _DEFAULT_PROFILE).strip(),
            simulator=_is_simulator(),
            transport=body.get("transport") or {},
            sim=body.get("sim") or {},
        )
    # The profile_key can also arrive inside the select_profile payload; keep the
    # record in sync so the live provider can pick the right seed-key families.
    payload = body.get("payload") or {}
    if payload.get("profile_key"):
        record.profile_key = str(payload["profile_key"]).strip()

    provider, cleanup = await _build_provider(record)
    try:
        if record.snapshot:
            orch = DmeSwapOrchestrator.restore(provider, record.snapshot)
        else:
            orch = DmeSwapOrchestrator(provider)

        prompt = await orch.handle(event, payload)

        record.snapshot = orch.snapshot()
        if orch.data.vin:
            record.vin = orch.data.vin
        if orch.data.profile_key:
            record.profile_key = orch.data.profile_key
        return prompt.to_dict(), record
    finally:
        await cleanup()


async def _build_provider(record: SwapSessionRecord):
    """Construct the swap provider — mock (simulator) or live hardware.

    Returns (provider, cleanup_coro). `cleanup` closes any live transport.
    """
    async def _noop_cleanup() -> None:
        return None

    if record.simulator:
        sim = record.sim or {}
        # The mock's flags let the clickable demo reproduce the exact fallback
        # the tech sees on a real MEVD17: an NRC on the UDS write → the BSL
        # wizard, and then a handshake-fail / not-configured Phase-2 outcome.
        return MockDmeSwapProvider(
            fail_read=bool(sim.get("fail_read", False)),
            fail_backup=bool(sim.get("fail_backup", False)),
            fail_write=bool(sim.get("fail_write", False)),
            corrupt_verify=bool(sim.get("corrupt_verify", False)),
            fail_align=bool(sim.get("fail_align", False)),
            uds_reject_nrc=sim.get("uds_reject_nrc"),
            bsl_handshake_fail=bool(sim.get("bsl_handshake_fail", False)),
            bsl_not_configured=bool(sim.get("bsl_not_configured", False)),
        ), _noop_cleanup

    # --- live hardware ----------------------------------------------------
    profile = get_dme_swap_profile(record.profile_key or _DEFAULT_PROFILE)
    if profile is None:
        raise SwapProviderError(
            f"Unknown swap profile {record.profile_key!r}. "
            f"Known: {sorted(DME_SWAP_PROFILES)}.")

    # Confirmed E-series addressing (raises SwapProviderError naming anything
    # missing — we never invent CAS/DME addresses or the ISN DID/level).
    addr = swap_address_config_from_env()

    cm = ConnectionManager()
    cfg_raw = record.transport or {}
    has_address = bool(
        cfg_raw.get("host") or cfg_raw.get("serial_port") or cfg_raw.get("channel"))
    if has_address:
        cfg = TransportConfig(
            kind=TransportKind(cfg_raw.get("kind", "kline")),
            host=cfg_raw.get("host"),
            port=int(cfg_raw.get("port", 13400)),
            serial_port=cfg_raw.get("serial_port"),
            channel=cfg_raw.get("channel"),
            kline_target_addr=cfg_raw.get("kline_target_addr"),
        )
        transport = await cm.connect(prefer=cfg)
    else:
        transport = await cm.connect()

    cas_seed = resolve_seed_key_provider(
        family=profile.cas_family, simulator=False)
    dme_seed = resolve_seed_key_provider(
        family=profile.dme_family, simulator=False)

    provider = RealDmeSwapProvider(
        transport=transport,
        cas_seed_provider=cas_seed,
        dme_seed_provider=dme_seed,
        addr=addr,
        backup_store=_backup_store(),
    )

    async def _cleanup() -> None:
        try:
            await transport.close()
        except Exception:  # pragma: no cover - close best-effort
            pass

    return provider, _cleanup
