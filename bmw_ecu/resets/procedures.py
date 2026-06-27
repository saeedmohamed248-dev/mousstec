"""Declarative catalog of service-reset procedures.

Oil reset, EPB pad-change service mode, SAS calibration, DPF forced
regeneration, throttle-body adaptation — every one of these is the SAME
shape on the bus:

  enter extended session → check pre-conditions → run one or more
  RoutineControl (0x31) routines in order → (optionally) read back a
  result DID to confirm → done.

So instead of five near-identical orchestrators, each procedure is a
declarative `ServiceProcedure`: which module, which feature gates it,
what the SafetyGate must confirm first, and the ordered list of routine
steps. ONE generic `ServiceResetOrchestrator` (reset_orchestrator.py)
interprets any procedure. Adding a sixth reset later = adding a row
here, no new state machine.

Pre-condition vocabulary maps straight onto the shared SafetyGate
`require` dict (premium/safety_checks.py): voltage band, gear, ignition
state, forbidden DTCs.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field

from ..premium.safety_checks import GearPosition, IgnitionState


class StepKind(str, enum.Enum):
    ROUTINE_START   = "routine_start"    # 0x31 0x01 — start routine
    ROUTINE_RESULT  = "routine_result"   # 0x31 0x03 — request results
    READ_DID        = "read_did"         # 0x22 — read a data identifier
    WRITE_DID       = "write_did"        # 0x2E — write a data identifier


@dataclass(frozen=True)
class ResetStep:
    """One bus operation inside a procedure."""
    label_ar: str
    kind: StepKind
    rid: int = 0x0000       # routine id (for ROUTINE_* steps)
    did: int = 0x0000       # data id (for READ_DID / WRITE_DID steps)

    def to_dict(self) -> dict:
        return {
            "label_ar": self.label_ar,
            "kind": self.kind.value,
            "rid": f"0x{self.rid:04X}" if self.rid else "",
            "did": f"0x{self.did:04X}" if self.did else "",
        }


@dataclass(frozen=True)
class SafetyRequirement:
    """Maps to the SafetyGate `require` dict. Only the fields a procedure
    actually cares about are set; the rest fall back to gate defaults."""
    voltage_min_v: float = 12.0
    voltage_max_v: float = 14.8
    gear_in: tuple[GearPosition, ...] = (GearPosition.P,)
    ignition_in: tuple[IgnitionState, ...] = (IgnitionState.KOEO,)
    forbidden_dtcs: tuple[str, ...] = ()

    def to_require(self) -> dict:
        return {
            "voltage_min_v": self.voltage_min_v,
            "voltage_max_v": self.voltage_max_v,
            "gear_in": list(self.gear_in),
            "ignition_in": list(self.ignition_in),
            "forbidden_dtcs": tuple(self.forbidden_dtcs),
        }


@dataclass(frozen=True)
class ServiceProcedure:
    code: str
    name_ar: str
    name_en: str
    feature_code: str            # granular-billing gate
    target_module: str           # module code (see scan.module_map)
    needs_security_access: bool
    safety: SafetyRequirement
    steps: tuple[ResetStep, ...]
    success_message_ar: str
    # A short technician-facing pre-flight instruction (e.g. "حط العربية
    # على P وشغّل الكونتاكت من غير ما تدوّر الموتور").
    preflight_ar: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name_ar": self.name_ar,
            "name_en": self.name_en,
            "feature_code": self.feature_code,
            "target_module": self.target_module,
            "needs_security_access": self.needs_security_access,
            "preflight_ar": self.preflight_ar,
            "steps": [s.to_dict() for s in self.steps],
        }


# All service resets share one saleable feature in the catalog.
SERVICE_RESETS_FEATURE = "service_resets"


# ─────────────────────────────────────────────────────────────────────
# The catalog. RIDs/DIDs are representative BMW service routines — they
# are documentation-grade here (the bus is mocked in tests) and the real
# values are filled in by the concrete provider per chassis.
# ─────────────────────────────────────────────────────────────────────
PROCEDURE_CATALOG: dict[str, ServiceProcedure] = {
    p.code: p for p in [
        # ── Oil service reset ──────────────────────────────────────────
        ServiceProcedure(
            code="oil_reset",
            name_ar="تصفير صيانة الزيت",
            name_en="Engine Oil Service Reset",
            feature_code=SERVICE_RESETS_FEATURE,
            target_module="kombi",
            needs_security_access=False,
            safety=SafetyRequirement(
                voltage_min_v=12.0,
                ignition_in=(IgnitionState.KOEO,),
            ),
            steps=(
                ResetStep("تصفير عدّاد الزيت (CBS oil)",
                          StepKind.ROUTINE_START, rid=0xAB01),
                ResetStep("قراءة العدّاد بعد التصفير للتأكيد",
                          StepKind.READ_DID, did=0xDB01),
            ),
            preflight_ar=("حط العربية على P وشغّل الكونتاكت من غير ما تدوّر "
                          "الموتور (KOEO)."),
            success_message_ar=("تم تصفير صيانة الزيت — المؤشر رجع 100% "
                                "والـ CBS اتسجّل من جديد."),
        ),
        # ── EPB pad-change service mode ────────────────────────────────
        ServiceProcedure(
            code="epb_service",
            name_ar="وضع صيانة فرامل اليد الكهربائية (EPB)",
            name_en="Electric Parking Brake — Service Mode",
            feature_code=SERVICE_RESETS_FEATURE,
            target_module="epb",
            needs_security_access=True,
            safety=SafetyRequirement(
                voltage_min_v=12.5,        # motors draw current — need headroom
                gear_in=(GearPosition.P,),
                ignition_in=(IgnitionState.KOEO,),
            ),
            steps=(
                ResetStep("فتح كاليبر الفرامل لوضع الصيانة (retract)",
                          StepKind.ROUTINE_START, rid=0xBC10),
                ResetStep("تأكيد إن الكاليبر اتفتح",
                          StepKind.ROUTINE_RESULT, rid=0xBC10),
                ResetStep("إعادة ضبط الكاليبر بعد تغيير التيل (apply)",
                          StepKind.ROUTINE_START, rid=0xBC11),
            ),
            preflight_ar=("العربية على P، الكونتاكت ON والموتور مطفي. "
                          "متغيّرش التيل غير لما الكاليبر يفتح."),
            success_message_ar=("اكتمل وضع صيانة الـ EPB — الكاليبر اتظبط "
                                "بعد تغيير التيل. اعمل دورة فرملة قبل التسليم."),
        ),
        # ── Steering angle sensor calibration ──────────────────────────
        ServiceProcedure(
            code="sas_calibration",
            name_ar="معايرة حساس زاوية الاستيرنج (SAS)",
            name_en="Steering Angle Sensor Calibration",
            feature_code=SERVICE_RESETS_FEATURE,
            target_module="dsc",
            needs_security_access=True,
            safety=SafetyRequirement(
                voltage_min_v=12.0,
                ignition_in=(IgnitionState.KOEO,),
                forbidden_dtcs=("C1500", "C1510"),
            ),
            steps=(
                ResetStep("صفّر زاوية الاستيرنج (الاستيرنج مستقيم)",
                          StepKind.ROUTINE_START, rid=0xCD20),
                ResetStep("قراءة الزاوية بعد المعايرة (≈0°)",
                          StepKind.READ_DID, did=0xDC20),
            ),
            preflight_ar=("ظبّط العجل مستقيم تماماً قبل التصفير، "
                          "والعربية واقفة على أرض مستوية."),
            success_message_ar=("تمت معايرة الـ SAS — زاوية الاستيرنج بقت 0° "
                                "ولمبة الـ DSC/التحكم بالثبات اتطفت."),
        ),
        # ── DPF forced regeneration ────────────────────────────────────
        ServiceProcedure(
            code="dpf_regen",
            name_ar="حرق فلتر الديزل القسري (DPF Regen)",
            name_en="Diesel Particulate Filter — Forced Regeneration",
            feature_code=SERVICE_RESETS_FEATURE,
            target_module="dme",
            needs_security_access=True,
            safety=SafetyRequirement(
                voltage_min_v=13.0,        # alternator must be charging
                gear_in=(GearPosition.P, GearPosition.N),
                ignition_in=(IgnitionState.KOER,),   # engine MUST be running
                forbidden_dtcs=("P2002", "P242F"),   # DPF physically blocked
            ),
            steps=(
                ResetStep("بدء الحرق القسري (الموتور شغّال على رمبات)",
                          StepKind.ROUTINE_START, rid=0xDE30),
                ResetStep("متابعة حرارة العادم وكتلة السخام",
                          StepKind.ROUTINE_RESULT, rid=0xDE30),
                ResetStep("قراءة كتلة السخام بعد الحرق",
                          StepKind.READ_DID, did=0xDD30),
            ),
            preflight_ar=("الموتور لازم يكون شغّال وسخن، العربية في مكان "
                          "مفتوح كويس التهوية، خزان وقود فيه بنزين/سولار كفاية."),
            success_message_ar=("اكتمل حرق الـ DPF — كتلة السخام نزلت للمعدل "
                                "الطبيعي ولمبة الفلتر اتطفت."),
        ),
        # ── Throttle-body adaptation ───────────────────────────────────
        ServiceProcedure(
            code="throttle_adaptation",
            name_ar="تأقلم بوابة الهواء (Throttle Adaptation)",
            name_en="Throttle Body Adaptation",
            feature_code=SERVICE_RESETS_FEATURE,
            target_module="dme",
            needs_security_access=False,
            safety=SafetyRequirement(
                voltage_min_v=12.0,
                gear_in=(GearPosition.P, GearPosition.N),
                ignition_in=(IgnitionState.KOEO,),
            ),
            steps=(
                ResetStep("مسح قيم التأقلم القديمة (reset adaptation)",
                          StepKind.ROUTINE_START, rid=0xEF40),
                ResetStep("تشغيل دورة تعلّم البوابة",
                          StepKind.ROUTINE_START, rid=0xEF41),
                ResetStep("تأكيد حفظ القيم الجديدة",
                          StepKind.ROUTINE_RESULT, rid=0xEF41),
            ),
            preflight_ar=("نظّف بوابة الهواء كويس قبل التأقلم، الكونتاكت ON "
                          "والموتور مطفي وكل الأحمال الكهربائية مقفولة."),
            success_message_ar=("تم تأقلم البوابة — وضع السلّنتي اتظبط من جديد "
                                "والـ DME حفظ قيم التعلّم."),
        ),
    ]
}


def get_procedure(code: str) -> ServiceProcedure | None:
    return PROCEDURE_CATALOG.get(code)


def all_procedures() -> tuple[ServiceProcedure, ...]:
    return tuple(PROCEDURE_CATALOG.values())
