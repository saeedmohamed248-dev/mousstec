"""🚗 خدمات الروبوت — أكواد الأعطال ومراجع الـ ECU."""

from __future__ import annotations


import logging



logger = logging.getLogger("mouss_tec_core")


# ---------------------------------------------------------------------------
# Diagnostics + ECU enrichment (part codes, fault codes, coding refs)
# ---------------------------------------------------------------------------

def lookup_fault_code(code: str) -> dict:
    """Resolve a DTC / fault code to its definition + guided steps + causes.

    Backs the voice assistant when a mechanic asks about a code (e.g. "P0301").
    Reads the shared `diagnostics_catalog.DTCDefinition`.
    """
    try:
        from diagnostics_catalog.models import DTCDefinition
    except Exception:
        return {"found": False, "code": code}

    dtc = DTCDefinition.objects.filter(code__iexact=(code or "").strip()).first()
    if not dtc:
        return {"found": False, "code": code}
    # 🐛 [FIX]: «long_description» مش موجود في الموديل — الوصف التفصيلي والقطع اللي غالباً السبب
    # (`full_description`، `likely_oem_parts`) عمرهم ما وصلوا للروبوت.
    parts = getattr(dtc, "likely_oem_parts", []) or []
    return {
        "found": True,
        "code": dtc.code,
        "severity": getattr(dtc, "severity", None),
        "description": getattr(dtc, "short_description", "") or getattr(dtc, "full_description", ""),
        "details": getattr(dtc, "full_description", "") or "",
        "guided_steps": getattr(dtc, "guided_steps", []) or [],
        "likely_causes": getattr(dtc, "likely_causes", []) or [],
        "likely_parts": [str(p.get("part_number") or p.get("name") or p) if isinstance(p, dict) else str(p)
                         for p in parts],
    }


def _normalize_key(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def lookup_ecu_profile(ecu_name: str) -> dict:
    """Return ECU hardware/coding reference (pinout + physical steps) for a module.

    Backs the "programming/coding" requirement — the robot can read out the
    verified physical steps and pinout callouts for an ECU from `bmw_ecu`.
    """
    try:
        from bmw_ecu.models import EcuHardwareProfile
    except Exception:
        return {"found": False, "ecu_name": ecu_name}

    profile = EcuHardwareProfile.objects.filter(
        ecu_name__iexact=(ecu_name or "").strip()
    ).first()
    if not profile:
        return {"found": False, "ecu_name": ecu_name}
    return {
        "found": True,
        "ecu_name": profile.ecu_name,
        "board_revision": getattr(profile, "board_revision", ""),
        "verified": getattr(profile, "verified", False),
        "callouts": getattr(profile, "callouts", []) or [],
        "physical_steps_ar": getattr(profile, "physical_steps_ar", []) or [],
        "physical_steps_en": getattr(profile, "physical_steps_en", []) or [],
    }
