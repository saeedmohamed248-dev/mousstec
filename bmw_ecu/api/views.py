"""REST endpoints.

Two endpoints, both POST JSON in / JSON out:

    POST /api/ecu/execute
        Body: { "vin": "...", "profile_name": "FEM_F30",
                "capabilities": {...},
                "transport": {"kind": "doip", "host": "169.254.255.0"},
                "target_isn_hex": "..." (optional, inject-only) }

    POST /api/ecu/wizard/step
        Body: { "wizard_session_id": 123, "step_kind": "capture_isn",
                "confirmed": true, "isn_hex": "...",
                "vin": "...", "profile_name": "FEM_F30",
                "transport": {...} }

Auth is via the existing DRF SimpleJWT setup (the project already wires it).
"""
from __future__ import annotations

import asyncio
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
from ..execution import (
    ExecutionStrategyManager, KNOWN_PROFILES, WorkshopCapabilities,
)
from ..execution.base import StrategyContext
from ..execution.hardware_automation import HardwareAutomationStrategy
from ..execution.hardware_automation.mousstec_box import MousstecBreakoutBox
from ..execution.interactive_guided import InteractiveGuidedStrategy
from ..execution.interactive_guided.frontend_contract import WizardResponse
from ..execution.software_only import SoftwareOnlyStrategy
from ..exceptions import BmwEcuError
from ..logging_setup import get_logger
from ..safety import BackupStore, BatteryMonitor, PreflightGate
from ..uds.client import UdsClient
from ..uds.security_access import SecurityAccess
from ..uds.seed_key_providers import MockSeedKeyProvider
from .orchestrator import ApiOrchestrator
from ..services.billing_gate import LocalBillingGate
from ..services.chatbot_translator import translate_exception
from pathlib import Path

log = get_logger(__name__)


# --- Endpoint 1: POST /api/ecu/execute --------------------------------------
@csrf_exempt
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def execute(request: Request) -> Response:
    payload = request.data or {}
    try:
        return Response(async_to_sync(_run_execute)(payload),
                        status=status.HTTP_200_OK)
    except BmwEcuError as e:
        log.warning("execute: domain error", extra={"code": e.code})
        return Response(translate_exception(e).to_json(),
                        status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        log.exception("execute: unhandled")
        return Response(translate_exception(e).to_json(),
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# --- Endpoint 2: POST /api/ecu/wizard/step ----------------------------------
@csrf_exempt
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wizard_step(request: Request) -> Response:
    payload = request.data or {}
    try:
        return Response(async_to_sync(_run_wizard_step)(payload),
                        status=status.HTTP_200_OK)
    except BmwEcuError as e:
        log.warning("wizard_step: domain error", extra={"code": e.code})
        return Response(translate_exception(e).to_json(),
                        status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        log.exception("wizard_step: unhandled")
        return Response(translate_exception(e).to_json(),
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# --- Async core --------------------------------------------------------------
async def _run_execute(payload: dict[str, Any]) -> dict[str, Any]:
    # Backwards-compat: operation_type defaults to "isn" so the existing
    # chatbot flow is byte-identical.
    operation_type = (payload.get("operation_type") or "isn").lower()

    orchestrator, ctx = await _build(payload)

    if operation_type == "coding":
        # Coding flow uses a separate orchestrator + entitlement gate.
        # Same ChatbotPayload JSON shape — UI doesn't change.
        from .coding_orchestrator import CodingOrchestrator
        from ..services.billing_gate import LocalBillingGate

        coding = CodingOrchestrator(billing=LocalBillingGate())
        response = await coding.run(
            ctx=ctx, coding_request=payload.get("coding_request") or {},
        )
        return response.to_json()

    # Default = ISN flow (unchanged).
    target_isn = payload.get("target_isn_hex")
    if target_isn:
        ctx.target_isn = bytes.fromhex(target_isn)
    response = await orchestrator.execute(ctx)
    return response.to_json()


async def _run_wizard_step(payload: dict[str, Any]) -> dict[str, Any]:
    orchestrator, ctx = await _build(payload)
    ctx.wizard_session_id = int(payload["wizard_session_id"])

    # Re-instantiate just the wizard strategy — orchestrator needs the same
    # type the Manager holds, but for handle_step() we can build directly.
    wizard = InteractiveGuidedStrategy()
    wizard_response = WizardResponse.from_json(payload)
    response = await orchestrator.wizard_step(ctx, wizard, wizard_response)
    return response.to_json()


# --- Context assembly --------------------------------------------------------
async def _build(payload: dict[str, Any]) -> tuple[ApiOrchestrator, StrategyContext]:
    vin = payload["vin"]
    profile_name = payload["profile_name"]
    profile = KNOWN_PROFILES[profile_name]
    caps = WorkshopCapabilities(**(payload.get("capabilities") or {}))

    cm = ConnectionManager()
    transport_cfg = payload.get("transport") or {}
    if transport_cfg:
        cfg = TransportConfig(
            kind=TransportKind(transport_cfg.get("kind", "doip")),
            host=transport_cfg.get("host"),
            port=int(transport_cfg.get("port", 13400)),
            serial_port=transport_cfg.get("serial_port"),
            channel=transport_cfg.get("channel"),
        )
        transport = await cm.connect(prefer=cfg)
    else:
        transport = await cm.connect()

    ecu_addr = profile.uds_isn_did >> 8
    client = UdsClient(transport, ecu_addr=ecu_addr, session_name="api")
    security = SecurityAccess(client, MockSeedKeyProvider())

    async def voltage_reader() -> float:
        # Read DID 0xF40C → 2 bytes centivolts.
        data = await client.read_data_by_identifier(0xF40C)
        return int.from_bytes(data, "big") / 100.0

    battery = BatteryMonitor(reader=voltage_reader)
    store = BackupStore(Path("/var/lib/mousstec/bmw_ecu/backups"))
    preflight = PreflightGate(battery=battery, store=store)

    # Strategies. Smart Box is optional — only instantiate if capabilities
    # report it AND a serial_port is provided.
    sw = SoftwareOnlyStrategy()
    if caps.has_smart_breakout_box and payload.get("smart_box_serial_port"):
        hw_hal = MousstecBreakoutBox(payload["smart_box_serial_port"])
        hw = HardwareAutomationStrategy(hw_hal)
    else:
        # Fallback no-op HAL keeps the Manager constructor happy.
        hw = HardwareAutomationStrategy(_NullHAL())  # type: ignore[arg-type]
    wizard = InteractiveGuidedStrategy()

    manager = ExecutionStrategyManager(sw, hw, wizard)
    orchestrator = ApiOrchestrator(manager=manager, billing=LocalBillingGate())
    ctx = StrategyContext(
        vin=vin, profile=profile, capabilities=caps,
        transport=transport, security=security, preflight=preflight,
    )
    return orchestrator, ctx


class _NullHAL:
    """Used when capabilities advertise no breakout box. is_eligible filters
    HardwareAutomationStrategy out before this is ever called.
    """
    async def open(self): pass
    async def close(self): pass
    async def set_pin(self, *_a, **_kw): raise RuntimeError("no smart box")
    async def read_voltage(self, *_a, **_kw): raise RuntimeError("no smart box")
    async def all_off(self): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
