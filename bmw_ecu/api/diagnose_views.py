"""Engine-swap Auto-Diagnose endpoint.

    POST /api/ecu/diagnose/swap
        Body: {
          "profile_name": "MEVD17_2_2_N18",   # the fitted DME's profile
          "cas_isn_hex":  "...",   # 32-byte CAS ISN   (optional)
          "dme_isn_hex":  "...",   # 32-byte DME ISN   (optional)
          "fa_engine":    "N14"    # engine the stored FA claims (optional)
        }

    Response: { "simulated": bool, "diagnosis": { ...EngineSwapDiagnosis... } }

Single-shot (no persistent session): it turns the four facts a scan gives
us into the structured "why won't it start after a DME swap" diagnosis.

Where the facts come from:
  • If the caller passes them (a real scan already read the ISNs / FA), we
    diagnose those verbatim.
  • Otherwise, in SIMULATOR mode we fill a demo swap scenario (mismatched
    ISN + an FA engine that differs from the profile) so the bot is
    driveable now; the response is flagged `simulated: true`.
  • With the simulator OFF and no facts supplied we return an honest 503 —
    the live UDS + seed-key read layer isn't wired here, and we never
    fabricate ISN/FA data against a real car.
"""
from __future__ import annotations

from typing import Any, Optional

from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from asgiref.sync import async_to_sync

from ..coding.fa_engine import engine_from_vo
from ..coding.fa_vo import parse_fa
from ..logging_setup import get_logger
from ..repair.swap_diagnosis import diagnose_engine_swap
from .runtime_mode import simulator_enabled

log = get_logger(__name__)

_DEFAULT_PROFILE = "MEVD17_2_2_N18"

# Deterministic demo facts for the simulator: a used DME carrying a DIFFERENT
# ISN than the car's CAS, and a stored FA that still claims the old engine.
_SIM_CAS_ISN = bytes(range(0x10, 0x30))
_SIM_DME_ISN = bytes(range(0x40, 0x60))
_SIM_FA_ENGINE = "N14"


def _hex_to_isn(raw: Optional[str]) -> Optional[bytes]:
    if not raw:
        return None
    try:
        return bytes.fromhex(raw.replace(" ", ""))
    except ValueError:
        return None


def _profile_facts(profile_name: str) -> tuple[str, bool]:
    """(engine, requires_bench) from KNOWN_PROFILES; imported lazily."""
    from ..execution import KNOWN_PROFILES
    profile = KNOWN_PROFILES[profile_name]
    return (getattr(profile, "engine", "") or "",
            bool(getattr(profile, "requires_bench", False)))


def _fa_block(raw: str) -> dict[str, Any]:
    """Parse a raw FA string into a display block + derived engine."""
    vo = parse_fa(raw)
    return {
        "raw": raw,
        "type_code": vo.type_code,
        "options": sorted(vo.options),
        "engine": engine_from_vo(vo),   # "" when not derivable — never guessed
    }


async def _read_fa_over_cable(body: dict[str, Any]) -> str:
    """Best-effort live FA read over the CANable via the existing VCM reader.

    Uses `read_vo_from_vcm` (UDS ReadDataByIdentifier — no SecurityAccess).
    Returns the raw FA string, or "" if the car returned nothing on the
    known gateway DIDs (on R56 the FA lives in the CAS, so this may miss and
    the caller falls back to a pasted FA — never a fabricated one)."""
    from ..connection.cable import cable_config_from_request
    from ..connection.kdcan import KDCANTransport
    from ..coding.vo_parser import read_vo_from_vcm
    from ..uds.client import UdsClient

    cfg = cable_config_from_request(body)
    transport = KDCANTransport(cfg)
    await transport.open()
    try:
        client = UdsClient(transport, ecu_addr=(cfg.can_rx_id or 0x40),
                           session_name="fa")
        vo = await read_vo_from_vcm(client)
        return vo.raw or ""
    finally:
        try:
            await transport.close()
        except Exception:  # pragma: no cover
            pass


@csrf_exempt
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def swap_diagnose(request: Request) -> Response:
    body: dict[str, Any] = request.data or {}
    profile_name = (body.get("profile_name") or _DEFAULT_PROFILE).strip()

    try:
        engine, requires_bench = _profile_facts(profile_name)
    except KeyError:
        return Response({"error": "unknown_profile", "detail": profile_name},
                        status=status.HTTP_400_BAD_REQUEST)

    cas_isn = _hex_to_isn(body.get("cas_isn_hex"))
    dme_isn = _hex_to_isn(body.get("dme_isn_hex"))
    fa_engine = (body.get("fa_engine") or "").strip()

    # ── FA: pasted, read live over the cable, or (simulator) demo ──────────
    fa_raw = (body.get("fa_raw") or "").strip()
    fa: dict[str, Any] = {}
    if not fa_raw and body.get("read_fa") and not simulator_enabled():
        try:
            fa_raw = async_to_sync(_read_fa_over_cable)(body)
        except Exception as e:  # honest fallback — never fabricate an FA
            log.info("live FA read failed", extra={"err": str(e)})
            return Response(
                {"error": "fa_read_failed",
                 "detail_ar": "معرفتش أقرا الـ FA من العربية عبر الكابل "
                              f"({e}). الصق الـ FA يدوي في الحقل fa_raw.",
                 "detail_en": "Couldn't read the FA over the cable "
                              f"({e}). Paste the FA into fa_raw instead."},
                status=status.HTTP_502_BAD_GATEWAY)
    if fa_raw:
        fa = _fa_block(fa_raw)
        if not fa_engine:
            fa_engine = fa.get("engine", "")

    # The ISN pair is what decides start; fa_engine is a secondary signal the
    # diagnosis tolerates as empty (no false mismatch).
    isn_pair_supplied = bool(cas_isn and dme_isn)
    simulated = False

    if not isn_pair_supplied:
        if not simulator_enabled():
            return Response(
                {"error": "read_layer_unavailable",
                 "detail_ar": "قراية الـ ISN الحية لسه مش موصّلة على السيرفر "
                              "ده. ابعت (cas_isn_hex, dme_isn_hex) أو شغّل "
                              "الوضع التجريبي (BMW_ECU_SIMULATOR=1).",
                 "detail_en": "Live ISN read isn't wired on this server. "
                              "Supply (cas_isn_hex, dme_isn_hex) or enable "
                              "the simulator."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE)
        # Simulator: fill the demo swap scenario.
        simulated = True
        cas_isn = cas_isn or _SIM_CAS_ISN
        dme_isn = dme_isn or _SIM_DME_ISN
        if not fa_engine:
            fa_engine = _SIM_FA_ENGINE

    diag = diagnose_engine_swap(
        cas_isn=cas_isn,
        dme_isn=dme_isn,
        fa_engine=fa_engine,
        dme_reported_engine=engine,
        dme_requires_bench=requires_bench,
    )
    return Response({"simulated": simulated, "diagnosis": diag.to_dict(),
                     "fa": fa})
