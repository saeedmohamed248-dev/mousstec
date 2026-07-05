"""FA engine-change endpoint — preview + guarded write.

    POST /api/ecu/fa/plan     — dry-run: show old→new FA, never writes.
    POST /api/ecu/fa/write     — writes the new FA (needs confirm + cable).

    Body: {
      "fa_raw": "3F30-N14-...",   # current FA (pasted or from a prior read)
      "to_engine": "N18",
      "confirm": true              # /write only — refuses without it
    }

The change is CATALOG-GATED: `plan_fa_engine_change` refuses to invent the
new model/type code, so an unregistered N14→N18 transform returns 409 with
"register the verified value first" — never a fabricated FA that would
mis-code the car. `/write` additionally requires an explicit confirm, backs
up the current FA first, then writes over the CANable and reads back to
verify. Simulator mode returns a labelled dry-run of the write path with no
hardware.
"""
from __future__ import annotations

from typing import Any

from asgiref.sync import async_to_sync
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from ..coding.fa_transform import (
    UnverifiedFaTransform,
    plan_from_raw,
)
from ..logging_setup import get_logger
from .runtime_mode import simulator_enabled

log = get_logger(__name__)


def _build_plan(body: dict[str, Any]):
    """Parse + plan; returns (plan, error_response). One of them is None."""
    fa_raw = (body.get("fa_raw") or "").strip()
    to_engine = (body.get("to_engine") or "").strip()
    if not fa_raw:
        return None, Response(
            {"error": "missing_fa", "detail_ar": "لازم fa_raw (الـ FA الحالي).",
             "detail_en": "fa_raw (the current FA) is required."},
            status=status.HTTP_400_BAD_REQUEST)
    if not to_engine:
        return None, Response(
            {"error": "missing_to_engine",
             "detail_ar": "لازم to_engine (الموتور المطلوب، مثلاً N18).",
             "detail_en": "to_engine (target engine, e.g. N18) is required."},
            status=status.HTTP_400_BAD_REQUEST)
    try:
        plan = plan_from_raw(fa_raw, to_engine=to_engine)
    except UnverifiedFaTransform as e:
        return None, Response(
            {"error": "unverified_transform",
             "detail_ar": f"مفيش تحويل FA متأكّد مسجّل: {e}. سجّل كود الموديل "
                          "المتأكّد قبل الكتابة (عشان FA غلط بيبوّظ الكودنج).",
             "detail_en": f"No verified FA transform: {e}. Register the "
                          "confirmed model code before writing."},
            status=status.HTTP_409_CONFLICT)
    return plan, None


@csrf_exempt
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def fa_plan(request: Request) -> Response:
    """Dry-run: return the old→new FA plan. Never writes."""
    plan, err = _build_plan(request.data or {})
    if err is not None:
        return err
    return Response({"plan": plan.to_dict()})


@csrf_exempt
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def fa_write(request: Request) -> Response:
    """Write the new FA. Requires an explicit confirm; backs up first."""
    body: dict[str, Any] = request.data or {}
    plan, err = _build_plan(body)
    if err is not None:
        return err

    if not body.get("confirm"):
        # Safe default: never write without an explicit confirm. Return the
        # plan so the UI can show it and ask.
        return Response(
            {"error": "confirm_required",
             "detail_ar": "الكتابة محتاجة confirm=true. راجع الخطة الأول.",
             "detail_en": "Writing needs confirm=true. Review the plan first.",
             "plan": plan.to_dict()},
            status=status.HTTP_412_PRECONDITION_FAILED)

    if simulator_enabled():
        return Response({
            "simulated": True, "written": True, "verified": True,
            "plan": plan.to_dict(),
            "detail_ar": "محاكاة: اتكتب الـ FA الجديد واتأكد (مفيش هاردوير).",
            "detail_en": "Simulated: new FA written + verified (no hardware).",
        })

    try:
        result = async_to_sync(_write_fa_over_cable)(body, plan)
    except _FaWriteUnavailable as e:
        return Response(
            {"error": "fa_write_unavailable",
             "detail_ar": f"الكتابة الحية مش متاحة: {e}. وصّل الكابل "
                          "(CANable) وظبط الـ CAN IDs.",
             "detail_en": f"Live FA write unavailable: {e}. Connect the "
                          "CANable and set the CAN IDs.",
             "plan": plan.to_dict()},
            status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except Exception as e:  # pragma: no cover - defensive
        log.exception("fa_write crashed")
        return Response({"error": "internal", "detail": repr(e),
                         "plan": plan.to_dict()},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    result["plan"] = plan.to_dict()
    result["simulated"] = False
    return Response(result)


class _FaWriteUnavailable(RuntimeError):
    """Live FA write couldn't run (no cable / no python-can)."""


async def _write_fa_over_cable(body: dict[str, Any], plan) -> dict[str, Any]:
    """Backup the current FA, write the new one via UDS 0x2E, read back.

    Wired on the existing cable transport + VCM reader; kept behind an
    explicit confirm and a verified transform. Any transport/library problem
    surfaces as _FaWriteUnavailable so the caller returns an honest 503."""
    from ..connection.cable import cable_config_from_request
    from ..connection.kdcan import KDCANTransport
    from ..coding.vo_parser import read_vo_from_vcm
    from ..exceptions import ConnectionError_
    from ..uds.client import UdsClient

    try:
        cfg = cable_config_from_request(body)
    except Exception as e:
        raise _FaWriteUnavailable(str(e)) from e

    transport = KDCANTransport(cfg)
    try:
        await transport.open()
    except ConnectionError_ as e:
        raise _FaWriteUnavailable(str(e)) from e

    try:
        client = UdsClient(transport, ecu_addr=(cfg.can_rx_id or 0x40),
                           session_name="fa-write")
        # Backup the current FA before touching anything.
        backup = await read_vo_from_vcm(client)
        # Write the new FA (WriteDataByIdentifier on the FA DID) then verify.
        await client.write_data_by_identifier(0xF802, plan.new_raw.encode("ascii"))
        after = await read_vo_from_vcm(client)
        verified = plan.new_type_code.upper() in (after.raw or "").upper()
        return {
            "written": True, "verified": verified,
            "backup_fa": backup.raw or "",
            "detail_ar": ("اتكتب الـ FA الجديد" +
                          (" واتأكد ✅" if verified else " بس التأكيد مطابقش")),
            "detail_en": ("New FA written" +
                          (" and verified ✅" if verified else
                           " but verify didn't match")),
        }
    finally:
        try:
            await transport.close()
        except Exception:  # pragma: no cover
            pass
