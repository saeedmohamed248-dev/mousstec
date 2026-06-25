"""FDL (Funktions-Daten-Liste) coding catalog — hidden features for F-series.

Each FdlFeature describes a single CAFD bit/byte tweak addressable via
UDS ReadDataByIdentifier (0x22) → mutate → WriteDataByIdentifier (0x2E).
The apply routine is intentionally generic so adding a new feature =
adding one FdlFeature row to the catalog, no code change.

The catalog ships the popular F-series tweaks. CAFD addresses are taken
from publicly-circulated reverse-engineering notes; verify against your
specific firmware before mass-deploying. Always pre-flight + backup.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional

from ..exceptions import CodingError
from ..logging_setup import get_logger
from ..uds.client import UdsClient
from ..uds.security_access import SecurityAccess
from ..uds.services import DiagSession

log = get_logger(__name__)


class FdlCategory(str, enum.Enum):
    COMFORT = "comfort"
    LIGHTING = "lighting"
    KOMBI = "kombi"
    SAFETY_DISABLE = "safety_disable"
    AUDIO = "audio"
    PERFORMANCE_DISPLAY = "performance_display"


@dataclass(frozen=True)
class FdlFeature:
    """One togglable hidden feature.

    Read DID `did`, locate `byte_offset`, set bits matching `bit_mask`
    to `enable_value` (when enabling) or `disable_value` (when disabling),
    write back. Conservative: a feature toggle only flips the masked bits,
    never the whole byte — so other settings in the same byte are preserved.
    """
    id: str                              # stable slug for API + UI
    name_ar: str
    name_en: str
    description_ar: str
    description_en: str
    category: FdlCategory
    ecu_target: str                      # "FEM", "KOMBI", "EPS", ...
    applicable_chassis: tuple[str, ...]  # ("F30", "F32", "F20")
    did: int                             # the CAFD DID to read/write
    byte_offset: int                     # within the read payload
    bit_mask: int                        # 0xFF flips the whole byte; 0x01 only LSB
    enable_value: int                    # value (post-mask) when enabled
    disable_value: int                   # value (post-mask) when disabled
    requires_security: bool = True
    notes: str = ""


# ---------------------------------------------------------------------------
# Catalog — popular F-series tweaks.
# Validated against community CAFD references; verify on first use per firmware.
# ---------------------------------------------------------------------------
CATALOG: dict[str, FdlFeature] = {
    "folding_mirrors_via_fob": FdlFeature(
        id="folding_mirrors_via_fob",
        name_ar="طي المرايا بريموت المفتاح",
        name_en="Fold mirrors via key fob",
        description_ar="ضغط مفتاح القفل مرتين يطوي المرايا الجانبية تلقائياً.",
        description_en="Double-press lock on the fob to fold mirrors.",
        category=FdlCategory.COMFORT,
        ecu_target="FEM",
        applicable_chassis=("F20", "F22", "F30", "F32", "F36"),
        did=0x3000, byte_offset=4, bit_mask=0x01,
        enable_value=0x01, disable_value=0x00,
    ),
    "m_sport_kombi_layout": FdlFeature(
        id="m_sport_kombi_layout",
        name_ar="عداد M Sport (شكل M)",
        name_en="M-Sport KOMBI cluster layout",
        description_ar="تفعيل شكل عدّاد الـ M-Sport على لوحة العدادات.",
        description_en="Switch instrument cluster to M-Sport layout.",
        category=FdlCategory.KOMBI,
        ecu_target="KOMBI",
        applicable_chassis=("F30", "F32", "F22"),
        did=0x3010, byte_offset=2, bit_mask=0xF0,
        enable_value=0x60, disable_value=0x00,  # M-style = nibble 6
        notes="Reboot KOMBI required after write.",
    ),
    "seatbelt_chime_off": FdlFeature(
        id="seatbelt_chime_off",
        name_ar="إيقاف نغمة حزام الأمان",
        name_en="Disable seatbelt chime",
        description_ar="إيقاف الإنذار الصوتي لحزام الأمان (الكرسي الأمامي).",
        description_en="Disable the driver-seat seatbelt warning chime.",
        category=FdlCategory.SAFETY_DISABLE,
        ecu_target="FEM",
        applicable_chassis=("F20", "F22", "F30", "F32", "F36"),
        did=0x3000, byte_offset=7, bit_mask=0x03,
        enable_value=0x03, disable_value=0x00,
        notes=(
            "Liability: disables a safety system. Workshop must obtain "
            "written customer consent. Surface a warning in the chatbot UI."
        ),
    ),
    "digital_speed_in_hud": FdlFeature(
        id="digital_speed_in_hud",
        name_ar="السرعة الرقمية في HUD",
        name_en="Digital speed in HUD",
        description_ar="إظهار السرعة الرقمية في الـ Head-Up Display دائماً.",
        description_en="Always show digital speed in Head-Up Display.",
        category=FdlCategory.PERFORMANCE_DISPLAY,
        ecu_target="KOMBI",
        applicable_chassis=("F30", "F32", "F36"),
        did=0x3010, byte_offset=5, bit_mask=0x01,
        enable_value=0x01, disable_value=0x00,
    ),
    "drl_as_indicator_off": FdlFeature(
        id="drl_as_indicator_off",
        name_ar="DRL لا تُطفأ مع الإشارة",
        name_en="DRL stays on with indicator",
        description_ar="لمبة النهار تبقى مضاءة حتى أثناء تشغيل الإشارة.",
        description_en="Keep DRL on even when indicator is active.",
        category=FdlCategory.LIGHTING,
        ecu_target="FEM",
        applicable_chassis=("F20", "F30", "F32"),
        did=0x3001, byte_offset=1, bit_mask=0x01,
        enable_value=0x01, disable_value=0x00,
    ),
}


def list_features(*, chassis: Optional[str] = None,
                  category: Optional[FdlCategory] = None) -> list[FdlFeature]:
    """Filter by chassis + category. Returns deterministic order."""
    features = list(CATALOG.values())
    if chassis:
        c = chassis.upper()
        features = [f for f in features if c in f.applicable_chassis]
    if category is not None:
        features = [f for f in features if f.category == category]
    return sorted(features, key=lambda f: (f.category.value, f.id))


def get(feature_id: str) -> FdlFeature:
    if feature_id not in CATALOG:
        raise CodingError(f"Unknown FDL feature {feature_id!r}")
    return CATALOG[feature_id]


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
async def apply_feature(client: UdsClient, security: SecurityAccess, *,
                        feature: FdlFeature, enable: bool,
                        vin: Optional[str] = None) -> bytes:
    """Read-modify-write the targeted DID with the masked bit change.

    Returns the new full DID payload (after write). Caller is responsible
    for PreflightGate + RollbackGuard — `apply_feature` is the inner
    primitive, not the safety boundary.
    """
    log.info("FDL apply begin", extra={
        "feature": feature.id, "enable": enable, "did": hex(feature.did),
    })
    await client.diagnostic_session_control(DiagSession.EXTENDED)
    if feature.requires_security:
        await security.unlock(vin=vin)

    current = await client.read_data_by_identifier(feature.did)
    if feature.byte_offset >= len(current):
        raise CodingError(
            f"Feature {feature.id}: DID {feature.did:#06x} returned only "
            f"{len(current)} bytes, byte_offset={feature.byte_offset} out of range",
        )

    new_payload = mutate_byte(
        current, offset=feature.byte_offset, bit_mask=feature.bit_mask,
        new_masked_value=(feature.enable_value if enable
                          else feature.disable_value),
    )
    await client.write_data_by_identifier(feature.did, new_payload)

    # Verify by read-back.
    readback = await client.read_data_by_identifier(feature.did)
    if readback[: len(new_payload)] != new_payload:
        raise CodingError(
            f"Feature {feature.id}: read-back differs from written payload",
        )
    log.info("FDL apply done", extra={"feature": feature.id})
    return new_payload


def mutate_byte(payload: bytes, *, offset: int, bit_mask: int,
                new_masked_value: int) -> bytes:
    """Pure bit-twiddler: keep bits outside `bit_mask` untouched.

    new_masked_value MUST already be aligned to the mask
    (e.g. mask=0xF0 → values like 0x60, not 0x06).
    """
    if not 0 <= offset < len(payload):
        raise ValueError(f"offset {offset} out of range for {len(payload)}-byte payload")
    if new_masked_value & ~bit_mask:
        raise ValueError(
            f"new_masked_value 0x{new_masked_value:02X} has bits outside "
            f"mask 0x{bit_mask:02X}",
        )
    buf = bytearray(payload)
    buf[offset] = (buf[offset] & ~bit_mask) | new_masked_value
    return bytes(buf)


def feature_to_chatbot_option(f: FdlFeature) -> dict:
    """Render an FdlFeature as one item in the chatbot's feature-menu input_schema."""
    return {
        "id": f.id,
        "label_ar": f.name_ar, "label_en": f.name_en,
        "description_ar": f.description_ar, "description_en": f.description_en,
        "category": f.category.value, "ecu_target": f.ecu_target,
        "warning": f.notes if f.category == FdlCategory.SAFETY_DISABLE else "",
    }
