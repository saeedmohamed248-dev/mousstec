"""ECU module catalog for the Full-System Auto-Scan.

A modern BMW carries 30-60 networked control units. A "full system scan"
walks every reachable module, pulls its fault memory, and rolls the
result up into one health report. To do that the orchestrator needs a
map of *which* modules to expect on *which* chassis, plus a stable code
+ bilingual name + functional address + category for each.

This file is the single source of truth for that map. The scan provider
returns raw module codes; the orchestrator looks each one up here to
render a human-friendly report. Unknown codes (a module we haven't
catalogued yet) still appear in the report — they just fall back to a
generic label, never get dropped.

Addresses are BMW diagnostic addresses (the byte you'd see in INPA /
ISTA), kept here for display + future real-bus addressing. Tests never
touch the bus, so the addresses are documentation-grade, not asserted.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class ModuleCategory(str, enum.Enum):
    POWERTRAIN = "powertrain"     # engine / transmission
    CHASSIS = "chassis"           # brakes / steering / suspension
    SAFETY = "safety"             # airbag / restraint
    BODY = "body"                 # access / lighting / comfort
    INFOTAINMENT = "infotainment"  # head unit / nav / audio
    NETWORK = "network"           # gateway / body-domain controllers


@dataclass(frozen=True)
class EcuModule:
    code: str            # stable slug, e.g. "dme"
    name_ar: str
    name_en: str
    address: int         # BMW diagnostic address (display / future bus use)
    category: ModuleCategory
    is_safety_critical: bool = False

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name_ar": self.name_ar,
            "name_en": self.name_en,
            "address": f"0x{self.address:02X}",
            "category": self.category.value,
            "is_safety_critical": self.is_safety_critical,
        }


# ─────────────────────────────────────────────────────────────────────
# Master catalog — code → EcuModule.
# ─────────────────────────────────────────────────────────────────────
MODULE_CATALOG: dict[str, EcuModule] = {
    m.code: m for m in [
        # ── Powertrain ────────────────────────────────────────────────
        EcuModule("dme", "كمبيوتر الموتور (DME)", "Engine (DME/DDE)",
                  0x12, ModuleCategory.POWERTRAIN),
        EcuModule("egs", "كمبيوتر الجير (EGS)", "Transmission (EGS)",
                  0x18, ModuleCategory.POWERTRAIN),
        EcuModule("vtg", "كمبيوتر الدفع الرباعي (VTG)", "Transfer Case (VTG)",
                  0x19, ModuleCategory.POWERTRAIN),
        # ── Chassis ───────────────────────────────────────────────────
        EcuModule("dsc", "نظام الثبات/الفرامل (DSC)", "Stability/ABS (DSC)",
                  0x29, ModuleCategory.CHASSIS, is_safety_critical=True),
        EcuModule("eps", "باور الاستيرنج الكهربائي (EPS)", "Electric Steering (EPS)",
                  0x57, ModuleCategory.CHASSIS, is_safety_critical=True),
        EcuModule("epb", "فرامل اليد الكهربائية (EPB)", "Parking Brake (EPB)",
                  0x34, ModuleCategory.CHASSIS, is_safety_critical=True),
        EcuModule("edc", "المساعدين الكهربائي (EDC)", "Adaptive Damping (EDC)",
                  0x37, ModuleCategory.CHASSIS),
        # ── Safety ────────────────────────────────────────────────────
        EcuModule("acsm", "وحدة الإيرباج (ACSM)", "Airbag/Restraint (ACSM)",
                  0x01, ModuleCategory.SAFETY, is_safety_critical=True),
        # ── Body / access ─────────────────────────────────────────────
        EcuModule("cas", "وحدة الوصول/التشغيل (CAS)", "Access/Start (CAS)",
                  0x40, ModuleCategory.BODY),
        EcuModule("fem", "وحدة الجسم الأمامية (FEM)", "Front Electronic Module (FEM)",
                  0x40, ModuleCategory.BODY),
        EcuModule("bdc", "وحدة الجسم (BDC)", "Body Domain Controller (BDC)",
                  0x40, ModuleCategory.BODY),
        EcuModule("frm", "وحدة الإضاءة (FRM)", "Footwell/Lighting Module (FRM)",
                  0x72, ModuleCategory.BODY),
        EcuModule("jbe", "صندوق الفيوزات الذكي (JBE)", "Junction Box (JBE)",
                  0x00, ModuleCategory.BODY),
        EcuModule("ihka", "وحدة التكييف (IHKA)", "Climate Control (IHKA)",
                  0x78, ModuleCategory.BODY),
        EcuModule("rdc", "حساسات ضغط الإطارات (RDC)", "Tyre Pressure (RDC/TPMS)",
                  0x65, ModuleCategory.BODY),
        EcuModule("pdc", "حساسات الركن (PDC/PMA)", "Park Distance (PDC/PMA)",
                  0x64, ModuleCategory.BODY),
        # ── Instrument / network ──────────────────────────────────────
        EcuModule("kombi", "طبلون العدادات (KOMBI)", "Instrument Cluster (KOMBI)",
                  0x60, ModuleCategory.NETWORK),
        EcuModule("zgw", "البوابة المركزية (ZGW)", "Central Gateway (ZGW)",
                  0x10, ModuleCategory.NETWORK),
        # ── Infotainment ──────────────────────────────────────────────
        EcuModule("headunit", "شاشة النظام (CIC/NBT/EVO)", "Head Unit (CIC/NBT/EVO)",
                  0x63, ModuleCategory.INFOTAINMENT),
        EcuModule("kombi_hud", "الشاشة الأمامية (HUD)", "Head-Up Display (HUD)",
                  0x62, ModuleCategory.INFOTAINMENT),
    ]
}


# ─────────────────────────────────────────────────────────────────────
# Per-chassis expected module sets. The scan provider tells us which
# modules actually answered; these sets tell us which we EXPECTED, so
# a missing safety-critical module ("ACSM did not answer") can be
# surfaced as a finding rather than silently ignored.
# ─────────────────────────────────────────────────────────────────────
class ChassisFamily(str, enum.Enum):
    E_SERIES = "e_series"   # E90, E60, E70... (CAS + FRM)
    F_SERIES = "f_series"   # F10, F30, F20... (FEM/CAS4)
    G_SERIES = "g_series"   # G20, G30, G05... (BDC)
    MINI = "mini"           # R56, F56


_CHASSIS_MODULES: dict[ChassisFamily, tuple[str, ...]] = {
    ChassisFamily.E_SERIES: (
        "dme", "egs", "dsc", "eps", "acsm", "cas", "frm",
        "ihka", "rdc", "pdc", "kombi", "headunit",
    ),
    ChassisFamily.F_SERIES: (
        "dme", "egs", "dsc", "eps", "epb", "acsm", "fem",
        "ihka", "rdc", "pdc", "kombi", "zgw", "headunit",
    ),
    ChassisFamily.G_SERIES: (
        "dme", "egs", "vtg", "dsc", "eps", "epb", "edc", "acsm", "bdc",
        "ihka", "rdc", "pdc", "kombi", "zgw", "headunit", "kombi_hud",
    ),
    ChassisFamily.MINI: (
        "dme", "egs", "dsc", "eps", "acsm", "fem", "frm",
        "ihka", "rdc", "kombi", "headunit",
    ),
}


def chassis_family(value: str | ChassisFamily) -> ChassisFamily:
    if isinstance(value, ChassisFamily):
        return value
    return ChassisFamily(str(value).strip().lower())


def expected_modules(family: str | ChassisFamily) -> tuple[EcuModule, ...]:
    """Modules we EXPECT for the chassis, in catalog order."""
    fam = chassis_family(family)
    return tuple(MODULE_CATALOG[c] for c in _CHASSIS_MODULES[fam])


def expected_module_codes(family: str | ChassisFamily) -> tuple[str, ...]:
    return tuple(_CHASSIS_MODULES[chassis_family(family)])


def get_module(code: str) -> EcuModule | None:
    return MODULE_CATALOG.get(code)


def describe_module(code: str) -> EcuModule:
    """Always return an EcuModule — unknown codes get a generic fallback
    so a module the bus reports but we haven't catalogued still renders
    in the report instead of being dropped."""
    m = MODULE_CATALOG.get(code)
    if m is not None:
        return m
    label = code.upper()
    return EcuModule(
        code=code,
        name_ar=f"وحدة غير معروفة ({label})",
        name_en=f"Unknown module ({label})",
        address=0x00,
        category=ModuleCategory.NETWORK,
        is_safety_critical=False,
    )
