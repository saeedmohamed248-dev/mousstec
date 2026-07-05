"""Cable connectivity check — "is the CANable talking to the car?"

    POST /api/ecu/cable/ping
        Body (all optional if set in server env): {
          "serial_port": "/dev/ttyACM0",
          "can_tx_id": "0x6F1",     # tester → ECU request ID
          "can_rx_id": "0x612",     # ECU → tester response ID
          "bitrate": 500000
        }

    Response: {
      "adapter_ok": bool,     # the CANable opened on the PC
      "ecu_answered": bool,   # an ECU replied to Tester-Present
      "detail_ar/detail_en": "...",
      "hint": {ar,en}         # adapter guidance (FTDI vs CANable)
    }

The first thing a tech does after plugging the cable in: confirm the link.
This opens the adapter and sends one UDS Tester-Present (0x3E 0x00) at the
supplied CAN IDs — no SecurityAccess, no crypto, so it works over the bare
CANable. It never simulates a real link: with the simulator on it returns a
clearly-labelled demo so the UI can be exercised without hardware; with it
off it does a genuine open + probe and reports exactly what happened.
"""
from __future__ import annotations

from typing import Any

from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from asgiref.sync import async_to_sync

from ..connection.cable import (
    CableConfigError,
    cable_config_from_request,
    describe_adapter_hint,
)
from ..logging_setup import get_logger
from .runtime_mode import simulator_enabled

log = get_logger(__name__)

# UDS Tester-Present, no response-suppression → the ECU must answer 0x7E 0x00.
_TESTER_PRESENT = b"\x3E\x00"


@csrf_exempt
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cable_ping(request: Request) -> Response:
    body: dict[str, Any] = request.data or {}
    hint = describe_adapter_hint()

    # Simulator: labelled demo so the UI works with no adapter attached.
    if simulator_enabled():
        return Response({
            "simulated": True,
            "adapter_ok": True,
            "ecu_answered": True,
            "detail_ar": "محاكاة: الكابل والعربية بيردّوا (مفيش هاردوير فعلي).",
            "detail_en": "Simulated: cable + car responding (no real hardware).",
            "hint": hint,
        })

    try:
        cfg = cable_config_from_request(body)
    except CableConfigError as e:
        return Response(
            {"error": "bad_cable_config",
             "detail_ar": f"إعداد الكابل ناقص: {e}. لازم serial_port و "
                          f"can_tx_id و can_rx_id.",
             "detail_en": f"Incomplete cable config: {e}. Need serial_port, "
                          f"can_tx_id and can_rx_id.",
             "hint": hint},
            status=status.HTTP_400_BAD_REQUEST)

    try:
        result = async_to_sync(_probe)(cfg)
    except Exception as e:  # pragma: no cover - defensive
        log.exception("cable_ping crashed")
        return Response({"error": "internal", "detail": repr(e), "hint": hint},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    result["hint"] = hint
    result["simulated"] = False
    return Response(result)


async def _probe(cfg) -> dict[str, Any]:
    """Open the CANable and send one Tester-Present. Returns a status dict —
    it never raises for an honest 'no answer'; only a truly unexpected error
    propagates to the view's 500 guard."""
    from ..connection.kdcan import KDCANTransport
    from ..exceptions import ConnectionError_, TransportTimeout

    transport = KDCANTransport(cfg)
    try:
        try:
            await transport.open()
        except ConnectionError_ as e:
            # Adapter/library problem (FTDI cable, missing python-can, wrong port)
            return {
                "adapter_ok": False, "ecu_answered": False,
                "detail_ar": f"الأدابتر مافتحش: {e}",
                "detail_en": f"Adapter did not open: {e}",
            }

        try:
            resp = await transport.request(
                cfg.target_addr, _TESTER_PRESENT, timeout=1.5)
            answered = bool(resp) and resp[0] in (0x7E, 0x7F)
            return {
                "adapter_ok": True, "ecu_answered": answered,
                "detail_ar": ("الكابل والعربية بيردّوا ✅"
                              if answered else
                              "الأدابتر فتح بس مفيش رد من العربية — راجع الـ "
                              "CAN IDs والكونتاكت ON."),
                "detail_en": ("Cable and car responding ✅" if answered else
                              "Adapter opened but no ECU reply — check the CAN "
                              "IDs and that ignition is ON."),
            }
        except TransportTimeout:
            return {
                "adapter_ok": True, "ecu_answered": False,
                "detail_ar": "الأدابتر فتح بس العربية مردّتش (timeout). راجع الـ "
                             "CAN IDs والكونتاكت ON.",
                "detail_en": "Adapter opened but the car didn't answer "
                             "(timeout). Check the CAN IDs and ignition ON.",
            }
    finally:
        try:
            await transport.close()
        except Exception:  # pragma: no cover
            pass
