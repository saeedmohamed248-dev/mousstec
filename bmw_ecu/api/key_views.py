"""Key-Programming endpoint — the BenchOrchestrator over HTTP.

    POST /api/ecu/key/step
        Body: {
          "session_id": "..."          # omit on the first call; returned to you
          "event": "select_profile" | "confirm_wiring" | "power_on" |
                   "enter_bench" | "dump_now" | "extract_isn" |
                   "pick_key_slot" | "burn_key" | "verify" | "finish" | "abort",
          "payload": { ... },           # event-specific (e.g. {"slot": 2})
          # --- only needed on the FIRST call (event="select_profile") ---
          "vin": "...",
          "family": "CAS3" | "CAS3+" | "FEM" | "BDC",
          "used_key": true              # adopting a second-hand key
        }

    Response: { "session_id": "...", "prompt": { ...BenchPrompt.to_dict()... } }

The BenchOrchestrator is forward-only; its snapshot is persisted in a
`KeyLearningSessionStore` between requests so the chatbot UI drives it click
by click. The orchestrator talks to the bench through the
`AbstractSmartHarness` seam:

  • BMW_ECU_SIMULATOR=1  → a deterministic `MockSmartHarness` (no hardware),
    so a technician can walk the whole flow as a dry run / demo.
  • otherwise            → live hardware, which needs the Mousstec Breakout
    Box bridge. That bridge is NOT shipped in this package, so rather than
    silently faking key data we return an honest 503 telling the tech to
    enable the simulator or install the bridge.
"""
from __future__ import annotations

from typing import Any, Optional

from asgiref.sync import async_to_sync
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from ..key_learning import (
    BenchOrchestrator,
    BenchState,
    MockSmartHarness,
    get_profile,
)
from ..key_learning.eeprom_dump import build_test_dump
from ..key_learning.profiles import ReadFlow
from ..key_learning.session import (
    KeyLearningSessionRecord,
    KeyLearningSessionStore,
)
from ..key_learning.smart_harness import AbstractSmartHarness
from ..logging_setup import get_logger
from .runtime_mode import simulator_enabled

log = get_logger(__name__)

_DEFAULT_FAMILY = "CAS3"

# A deterministic 32-byte demo ISN for the simulator's UDS-flow families
# (FEM / BDC) where there is no EEPROM blob to parse. Never used on live
# hardware — the real ISN is read from the module.
_SIM_UDS_ISN = bytes(range(0x40, 0x60))


# --- Endpoint ---------------------------------------------------------------
@csrf_exempt
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def key_step(request: Request) -> Response:
    body: dict[str, Any] = request.data or {}
    event = (body.get("event") or "select_profile").strip()
    store = KeyLearningSessionStore()
    session_id = body.get("session_id") or store.new_session_id()
    record = store.load(session_id)

    if record is None and event != "select_profile":
        return Response(
            {"error": "session_not_found",
             "detail": "No active session — send event='select_profile' first."},
            status=status.HTTP_409_CONFLICT,
        )

    try:
        prompt_dict, record = async_to_sync(_drive)(session_id, event, body, record)
    except _HardwareBridgeMissing as e:
        log.warning("key_step: no live harness", extra={"detail": str(e)})
        return Response(
            {"error": "hardware_not_found",
             "detail_ar": "لسه مفيش جسر للـ Smart Harness الحقيقي على السيرفر ده. "
                          "شغّل الوضع التجريبي (BMW_ECU_SIMULATOR=1) عشان تجرب "
                          "الخطوات، أو ركّب بريدج الـ Mousstec Breakout Box.",
             "detail_en": "No live Smart-Harness bridge on this server. Enable the "
                          "simulator (BMW_ECU_SIMULATOR=1) for a dry run, or install "
                          "the Mousstec Breakout Box bridge.",
             "detail": str(e)},
            status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except Exception as e:  # pragma: no cover - defensive top-level guard
        log.exception("key_step crashed")
        return Response({"error": "internal", "detail": repr(e)},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    store.save(record)
    # Free the cache slot only on a truly final state. FAILED keeps the record
    # so the UI can show the reason before the tech starts a new session.
    if prompt_dict.get("state") == BenchState.DONE.value:
        store.delete(session_id)
    return Response({"session_id": session_id, "prompt": prompt_dict})


class _HardwareBridgeMissing(RuntimeError):
    """Raised when live hardware is requested but no real harness exists."""


# --- Orchestration driver ---------------------------------------------------
async def _drive(session_id: str, event: str, body: dict[str, Any],
                 record: Optional[KeyLearningSessionRecord]):
    if record is None:
        family = (body.get("family") or _DEFAULT_FAMILY).strip()
        record = KeyLearningSessionRecord(
            session_id=session_id,
            vin=(body.get("vin") or "").strip(),
            family=family,
            used_key=bool(body.get("used_key")),
            simulator=simulator_enabled(),
        )

    harness = _build_harness(record)

    if record.snapshot:
        orch = BenchOrchestrator.restore(harness, record.snapshot)
    else:
        orch = BenchOrchestrator(harness)

    # Merge the session-level bootstrap fields into the very first event so the
    # UI only has to send them once.
    payload = dict(body.get("payload") or {})
    if event == "select_profile":
        payload.setdefault("family", record.family)
        payload.setdefault("vin", record.vin)
        payload.setdefault("used_key", record.used_key)

    prompt = await orch.handle(event, payload)

    # Simulator convenience: UDS-flow families (FEM / BDC) have no EEPROM blob,
    # so the orchestrator expects the ISN to be injected (production wires the
    # real UDS extractor). In sim we inject a deterministic demo ISN the moment
    # bench mode captures, so the tech can drive the whole click-path.
    if (record.simulator and orch.state == BenchState.DUMP_CAPTURED
            and not orch.data.isn_hex and orch.data.family is not None
            and get_profile(orch.data.family).read_flow == ReadFlow.UDS):
        orch.inject_isn_for_uds_flow(_SIM_UDS_ISN)

    record.snapshot = orch.snapshot()
    if orch.data.vin:
        record.vin = orch.data.vin
    if orch.data.family is not None:
        record.family = orch.data.family.value
    record.used_key = orch.data.used_key
    return prompt.to_dict(), record


def _build_harness(record: KeyLearningSessionRecord) -> AbstractSmartHarness:
    """Construct the orchestrator's bench harness — mock (simulator) or live.

    The EEPROM-flow families (CAS3 / CAS3+) need a preloaded dump so the sim
    can walk DUMP_NOW → EXTRACT_ISN; we synthesise a valid, non-virgin dump
    with a free slot to burn into. UDS families get the ISN injected in
    `_drive` instead.
    """
    if not record.simulator:
        raise _HardwareBridgeMissing(
            "live Smart-Harness bridge not installed on this server")

    family = record.family or _DEFAULT_FAMILY
    try:
        profile = get_profile(family)
    except KeyError:
        # Let the orchestrator surface the unknown-family error uniformly.
        return MockSmartHarness()

    if profile.read_flow == ReadFlow.EEPROM:
        dump = build_test_dump(
            chip=profile.eeprom_chip or "M35080",
            isn=bytes(range(0x10, 0x30)),
        )
        return MockSmartHarness(eeprom_payload=dump)
    return MockSmartHarness()
