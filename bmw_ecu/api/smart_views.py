"""Smart Auto-Detect endpoint — the UniversalSmartOrchestrator over HTTP.

    POST /api/ecu/smart/step
        Body: {
          "session_id": "..."         # omit on the first call; returned to you
          "event": "start" | "backup" | "code" | "ready" | "extract" |
                   "sync" | "abort" | "rollback",
          "payload": { ... },          # event-specific (e.g. coding options)
          # --- only needed on the FIRST call (event="start") ---
          "profile_name": "MEVD17_2_2_N18",
          "vin": "...",
          "transport": { "kind": "doip", "host": "169.254.255.0" },
          "coding_did": 49664,         # optional: real coding region DID
          "sim": { "dme_locked": false, "transport_kind": "doip" }
        }

    Response: { "session_id": "...", "prompt": { ...UPrompt.to_dict()... } }

The orchestrator is a forward-only state machine; we persist its snapshot in a
`SmartSessionStore` between requests so the chatbot UI can drive it click by
click. The heavy bytes (the ECU backup) live in the content-addressed
BackupStore, so rollback survives across requests and reboots.

Simulator switch (`BMW_ECU_SIMULATOR=1`) swaps the live `RealUniversalEcuIo`
for the deterministic `MockUniversalEcuIo` — same orchestrator, no hardware.
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
from ..execution import KNOWN_PROFILES
from ..logging_setup import get_logger
from .runtime_mode import simulator_enabled
from ..safety import BackupStore
from ..uds import resolve_seed_key_provider
from ..uds.client import UdsClient
from ..uds.security_access import SecurityAccess
from ..universal import (
    MockUniversalEcuIo,
    RealUniversalEcuIo,
    SmartSessionRecord,
    SmartSessionStore,
    UniversalSmartOrchestrator,
)

log = get_logger(__name__)

_DEFAULT_PROFILE = "MEVD17_2_2_N18"


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
def smart_step(request: Request) -> Response:
    body: dict[str, Any] = request.data or {}
    event = (body.get("event") or "start").strip()
    store = SmartSessionStore()
    session_id = body.get("session_id") or store.new_session_id()
    record = store.load(session_id)

    if record is None and event != "start":
        return Response(
            {"error": "session_not_found",
             "detail": "No active session — send event='start' first."},
            status=status.HTTP_409_CONFLICT,
        )

    try:
        prompt_dict, record = async_to_sync(_drive)(session_id, event, body, record)
    except KeyError as e:
        return Response({"error": "unknown_profile", "detail": str(e)},
                        status=status.HTTP_400_BAD_REQUEST)
    except NoInterfaceDetected as e:
        # Honest hardware failure — NOT a crash, and NEVER a silent Mock.
        # The bench is wired but no interface answered (e.g. the blue FTDI
        # cable isn't a real CAN adapter, or the CANable/ENET isn't up yet).
        log.warning("smart_step: no interface", extra={"detail": str(e)})
        return Response(
            {"error": "hardware_not_found",
             "detail_ar": "مفيش جهاز/واجهة بترد. اتأكد إن الـ CANable أو ENET "
                          "متوصّل وإن الاجنشن ON. النظام مش هيشتغل بالمحاكاة.",
             "detail_en": "No ECU interface responded. Check the CANable/ENET "
                          "link and that ignition is ON. The system will not "
                          "fall back to simulation.",
             "detail": str(e)},
            status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except Exception as e:  # pragma: no cover - defensive top-level guard
        log.exception("smart_step crashed")
        return Response({"error": "internal", "detail": repr(e)},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    store.save(record)
    # Free the cache slot only on a truly final state. A FAILED prompt is
    # "terminal" for the current path but still offers Rollback, so we KEEP it
    # alive until the job actually completes (DONE) or is rolled back.
    if prompt_dict.get("state") in ("done", "rolled_back"):
        store.delete(session_id)
    return Response({"session_id": session_id, "prompt": prompt_dict})


# --- Orchestration driver ---------------------------------------------------
async def _drive(session_id: str, event: str, body: dict[str, Any],
                 record: Optional[SmartSessionRecord]):
    if record is None:
        record = SmartSessionRecord(
            session_id=session_id,
            vin=body.get("vin", "") or "",
            profile_name=body.get("profile_name") or _DEFAULT_PROFILE,
            simulator=_is_simulator(),
            transport=body.get("transport") or {},
            sim=body.get("sim") or {},
        )
    # The coding-region DID may be supplied on any call (the tech often only
    # has it once E-Sys is open) — never guessed server-side.
    if body.get("coding_did") is not None:
        record.sim["coding_did"] = body["coding_did"]

    io, cleanup = await _build_io(record, body)
    try:
        store = _backup_store()

        async def sink(b) -> None:
            store.save(b)
            record.backup_sha256 = b.sha256
            record.backup_ecu_name = b.ecu_name
            record.vin = b.vin

        if record.snapshot:
            backup = None
            if record.backup_sha256 and record.vin and record.backup_ecu_name:
                backup = store.load(record.vin, record.backup_ecu_name,
                                    record.backup_sha256)
            orch = UniversalSmartOrchestrator.restore(
                io=io, snapshot=record.snapshot, backup=backup, backup_sink=sink)
        else:
            orch = UniversalSmartOrchestrator(io=io, backup_sink=sink)

        prompt = await orch.handle(event, body.get("payload") or {})

        record.snapshot = orch.snapshot()
        if orch.data.vin:
            record.vin = orch.data.vin
        return prompt.to_dict(), record
    finally:
        await cleanup()


async def _build_io(record: SmartSessionRecord, body: dict[str, Any]):
    """Construct the orchestrator's I/O — mock (simulator) or live hardware.

    Returns (io, cleanup_coro_factory). `cleanup` closes any live transport.
    """
    async def _noop_cleanup() -> None:
        return None

    if record.simulator:
        sim = record.sim or {}
        io = MockUniversalEcuIo(
            transport_kind=sim.get("transport_kind")
            or (record.transport or {}).get("kind") or "doip",
            vin=record.vin or "WBAUNIVERSAL00001",
            dme_locked=bool(sim.get("dme_locked", False)),
            pinout=sim.get("pinout"),
        )
        return io, _noop_cleanup

    # --- live hardware ----------------------------------------------------
    profile = KNOWN_PROFILES[record.profile_name]
    cm = ConnectionManager()
    cfg_raw = record.transport or {}
    # Only force a specific transport when the caller gave us enough to address
    # it (a DoIP host, a serial port, or a CAN channel). The bare {"kind":...}
    # the UI sends is NOT enough — DoIPTransport needs a host — so we fall back
    # to ConnectionManager auto-detect, which uses the standard BMW ENET
    # link-local (BMW_ECU_DOIP_HOST or 169.254.255.0). This is what makes the
    # "Smart Flow" button actually connect to an F30 over ENET with no typing.
    has_address = bool(
        cfg_raw.get("host") or cfg_raw.get("serial_port") or cfg_raw.get("channel"))
    if has_address:
        cfg = TransportConfig(
            kind=TransportKind(cfg_raw.get("kind", "doip")),
            host=cfg_raw.get("host"),
            port=int(cfg_raw.get("port", 13400)),
            serial_port=cfg_raw.get("serial_port"),
            channel=cfg_raw.get("channel"),
        )
        transport = await cm.connect(prefer=cfg)
    else:
        transport = await cm.connect()

    ecu_addr = profile.uds_isn_did >> 8
    client = UdsClient(transport, ecu_addr=ecu_addr, session_name="smart")
    provider = resolve_seed_key_provider(
        family=profile.seed_key_family,
        security_level=profile.isn_security_level,
        simulator=False,
    )
    security = SecurityAccess(client, provider)

    coding_did = (record.sim or {}).get("coding_did")
    if coding_did is not None:
        coding_did = int(coding_did)

    io = RealUniversalEcuIo(
        client=client, security=security,
        transport_kind=transport.kind.value,
        coding_did=coding_did,
        coding_level=profile.isn_security_level,
        vin=record.vin or None,
        pinout=None,  # confirmed pinout comes from the DB catalog, not guessed
    )

    async def _cleanup() -> None:
        try:
            await transport.close()
        except Exception:  # pragma: no cover - close best-effort
            pass

    return io, _cleanup
