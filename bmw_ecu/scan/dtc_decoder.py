"""DTC decoding + severity classification.

A scan provider returns raw fault entries: a 3-byte DTC code (SAE J2012)
plus a 1-byte ISO 14229 status mask. This module turns each raw entry
into a `DecodedDtc` the report and the chatbot can present:

  • a printable code string ("P0301", "C1234", "U0100", "B1000"),
  • a bilingual description (known codes get a curated AR/EN line; the
    rest get a category-level fallback so nothing is ever blank),
  • a severity (INFO / SOFT / HARD / SAFETY) used to colour the report
    and to decide GREEN / YELLOW / RED overall status,
  • decoded status flags (confirmed vs pending vs test-failed) so the
    UI can separate "stored history" from "happening right now".

Severity policy
---------------
  SAFETY  — airbag / brakes / steering faults, or any code in
            SAFETY_CODES. Always forces overall RED.
  HARD    — a *confirmed* powertrain/chassis fault (engine, trans, ABS).
  SOFT    — a confirmed comfort/body/network fault, OR any pending fault
            (intermittent, not yet matured).
  INFO    — informational / "history only" with no active status bits.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class DtcSeverity(enum.IntEnum):
    INFO = 0
    SOFT = 1
    HARD = 2
    SAFETY = 3


# ── ISO 14229 DTC status mask bits ───────────────────────────────────
STATUS_TEST_FAILED            = 0x01  # failed this operation cycle
STATUS_TEST_FAILED_THIS_CYCLE = 0x02
STATUS_PENDING                = 0x04  # failed since last clear, not confirmed
STATUS_CONFIRMED              = 0x08  # matured / stored
STATUS_TEST_NOT_COMPLETED     = 0x40
STATUS_WARNING_INDICATOR      = 0x80  # MIL / warning lamp requested


# Codes we always treat as life-safety regardless of which module
# reported them. Curated, extend freely.
SAFETY_CODES: frozenset[str] = frozenset({
    # Airbag / restraint
    "B1000", "B1001", "B1010", "B1018", "B1024",
    # Brakes / ABS / DSC
    "C1234", "C1000", "C1011", "C1012",
    # Steering
    "C1500", "C1510",
})


# Curated bilingual descriptions for the codes a BMW workshop sees daily.
_KNOWN: dict[str, tuple[str, str]] = {
    "P0300": ("خبط عشوائي في أكتر من سلندر (Random Misfire)",
              "Random/multiple cylinder misfire"),
    "P0301": ("خبط في السلندر رقم 1", "Cylinder 1 misfire"),
    "P0302": ("خبط في السلندر رقم 2", "Cylinder 2 misfire"),
    "P0303": ("خبط في السلندر رقم 3", "Cylinder 3 misfire"),
    "P0304": ("خبط في السلندر رقم 4", "Cylinder 4 misfire"),
    "P0171": ("الخليط فقير - بنك 1 (System too Lean)",
              "System too lean (Bank 1)"),
    "P0174": ("الخليط فقير - بنك 2", "System too lean (Bank 2)"),
    "P0420": ("كفاءة الحفاز ضعيفة - بنك 1 (Catalyst Efficiency)",
              "Catalyst efficiency below threshold (Bank 1)"),
    "P0455": ("تسريب كبير في نظام التبخير (EVAP Leak)",
              "EVAP system large leak"),
    "P0011": ("توقيت عمود الكامات متقدم - بنك 1 (VANOS)",
              "Camshaft timing over-advanced (Bank 1 / VANOS)"),
    "P0128": ("حرارة المياه أقل من المطلوب (Thermostat)",
              "Coolant temp below regulating temp (thermostat)"),
    "P0a0f": ("عطل في نظام الهايبرد (Hybrid)", "Engine fail-safe (hybrid)"),
    "P0700": ("طلب لمبة عطل من الجير (Transmission)",
              "Transmission control system (MIL request)"),
    "P0730": ("نسبة تروس الجير غلط", "Incorrect gear ratio"),
    "U0100": ("فقد الاتصال مع كمبيوتر الموتور (Lost Comm DME)",
              "Lost communication with ECM/PCM"),
    "U0101": ("فقد الاتصال مع كمبيوتر الجير", "Lost communication with TCM"),
    "U0121": ("فقد الاتصال مع وحدة ABS/DSC",
              "Lost communication with ABS module"),
    "B1000": ("عطل داخلي في وحدة الإيرباج (ACSM)",
              "Airbag control unit internal fault"),
    "B1018": ("بيانات تصادم مخزّنة في الإيرباج (Crash Data)",
              "Crash data stored in restraint module"),
    "C1234": ("عطل في حساس سرعة العجلة (ABS Wheel Speed)",
              "Wheel speed sensor fault (ABS)"),
    "C1500": ("عطل في حساس زاوية الاستيرنج (SAS)",
              "Steering angle sensor fault"),
}


_CATEGORY_FALLBACK: dict[str, tuple[str, str]] = {
    "P": ("عطل في مجموعة نقل الحركة/الموتور", "Powertrain fault"),
    "C": ("عطل في الشاسيه (فرامل/تعليق/استيرنج)", "Chassis fault"),
    "B": ("عطل في كهرباء الجسم", "Body fault"),
    "U": ("عطل في شبكة الاتصال بين الوحدات", "Network/communication fault"),
}


@dataclass
class DecodedDtc:
    code: str
    status_byte: int
    description_ar: str
    description_en: str
    severity: DtcSeverity
    is_confirmed: bool
    is_pending: bool
    is_warning_lamp: bool

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "status_byte": f"0x{self.status_byte:02X}",
            "description_ar": self.description_ar,
            "description_en": self.description_en,
            "severity": self.severity.name.lower(),
            "is_confirmed": self.is_confirmed,
            "is_pending": self.is_pending,
            "is_warning_lamp": self.is_warning_lamp,
        }


def normalize_code(code: str) -> str:
    """Upper-case, strip whitespace. Pads bare hex like '0301' → 'P0301'
    only if it already starts with a letter; otherwise returned as-is."""
    return code.strip().upper()


def _category_letter(code: str) -> str:
    c = code[:1].upper()
    return c if c in ("P", "C", "B", "U") else "P"


def _classify(code: str, *, module_is_safety_critical: bool,
              is_confirmed: bool, is_pending: bool) -> DtcSeverity:
    norm = code.upper()
    if norm in SAFETY_CODES:
        return DtcSeverity.SAFETY
    letter = _category_letter(norm)
    # A confirmed fault inside a safety-critical module (airbag, ABS,
    # steering) is always SAFETY even if the specific code isn't curated.
    if module_is_safety_critical and is_confirmed:
        return DtcSeverity.SAFETY
    if not is_confirmed and not is_pending:
        return DtcSeverity.INFO
    if is_pending and not is_confirmed:
        return DtcSeverity.SOFT
    # Confirmed:
    if letter in ("P", "C"):
        return DtcSeverity.HARD
    return DtcSeverity.SOFT


def decode_dtc(code: str, status_byte: int, *,
               module_is_safety_critical: bool = False) -> DecodedDtc:
    norm = normalize_code(code)
    is_confirmed = bool(status_byte & STATUS_CONFIRMED)
    is_pending = bool(status_byte & STATUS_PENDING)
    is_warning = bool(status_byte & STATUS_WARNING_INDICATOR)

    desc = _KNOWN.get(norm)
    if desc is None:
        desc = _CATEGORY_FALLBACK[_category_letter(norm)]

    severity = _classify(
        norm,
        module_is_safety_critical=module_is_safety_critical,
        is_confirmed=is_confirmed, is_pending=is_pending,
    )
    return DecodedDtc(
        code=norm,
        status_byte=status_byte & 0xFF,
        description_ar=desc[0],
        description_en=desc[1],
        severity=severity,
        is_confirmed=is_confirmed,
        is_pending=is_pending,
        is_warning_lamp=is_warning,
    )
