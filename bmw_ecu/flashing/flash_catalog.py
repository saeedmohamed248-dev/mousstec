"""Declarative catalog of ECU flash jobs.

The low-level `FlashEngine` (engine.py) drives the raw UDS programming
sequence against real hardware. This catalog is the layer ABOVE it: a
declarative description of the flash *jobs* a workshop actually sells —
DME software update, FEM/BDC update, instrument-cluster update, gearbox
(EGS) update — each one the SAME shape so ONE guided orchestrator
(flash_orchestrator.py) can run any of them, entitlement-gated and
hardware-free in tests.

Flashing is the highest-risk operation in the suite: a half-written ECU
is a brick. So every job carries strict pre-conditions (a CHARGED battery
— flashing draws current for minutes — and the right ignition state) plus
a payload size band the orchestrator validates before it erases anything.
The orchestrator additionally enforces a MANDATORY backup + rollback, so
the catalog only needs to declare intent, not safety mechanics.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..premium.safety_checks import IgnitionState, SafetyRequirement


FLASH_FEATURE = "ecu_flashing"


# Flashing must run on a charger / strong battery — the ECU is mid-write
# for minutes and a voltage sag corrupts it. Hence a HIGH floor (13.0 V)
# and key-on/engine-off so the alternator isn't swinging the rail.
_FLASH_SAFETY = SafetyRequirement(
    voltage_min_v=13.0,
    voltage_max_v=14.8,
    ignition_in=(IgnitionState.KOEO,),
)


@dataclass(frozen=True)
class FlashJob:
    code: str
    name_ar: str
    name_en: str
    target_module: str            # module code (see scan.module_map)
    target_addr: int              # flash region start
    needs_security: bool
    safety: SafetyRequirement
    expected_min_bytes: int       # payload size band — guards wrong-file flashes
    expected_max_bytes: int
    checksum_algo: str
    success_message_ar: str
    preflight_ar: str = ""
    feature_code: str = FLASH_FEATURE

    def size_ok(self, n: int) -> bool:
        return self.expected_min_bytes <= n <= self.expected_max_bytes

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name_ar": self.name_ar,
            "name_en": self.name_en,
            "target_module": self.target_module,
            "target_addr": f"0x{self.target_addr:08X}",
            "needs_security": self.needs_security,
            "expected_min_bytes": self.expected_min_bytes,
            "expected_max_bytes": self.expected_max_bytes,
            "checksum_algo": self.checksum_algo,
            "preflight_ar": self.preflight_ar,
            "feature_code": self.feature_code,
        }


_KIB = 1024


FLASH_CATALOG: dict[str, FlashJob] = {
    j.code: j for j in [
        FlashJob(
            code="dme_sw_update",
            name_ar="تحديث سوفت وير كمبيوتر الموتور (DME)",
            name_en="Engine ECU (DME) Software Update",
            target_module="dme",
            target_addr=0x00080000,
            needs_security=True,
            safety=_FLASH_SAFETY,
            expected_min_bytes=256 * _KIB,
            expected_max_bytes=4096 * _KIB,
            checksum_algo="crc32",
            preflight_ar=("وصّل شاحن بطارية ثابت (13.5–14 فولت)، الكونتاكت ON "
                          "والموتور مطفي، وما تفصلش العربية طول التحديث."),
            success_message_ar=("تم تحديث سوفت وير الـ DME وعدى فحص الاعتماد — "
                                "الوحدة عملت ريسيت وبتشتغل بالنسخة الجديدة."),
        ),
        FlashJob(
            code="fem_bdc_update",
            name_ar="تحديث وحدة FEM/BDC",
            name_en="FEM/BDC Body Controller Update",
            target_module="fem",
            target_addr=0x00100000,
            needs_security=True,
            safety=_FLASH_SAFETY,
            expected_min_bytes=128 * _KIB,
            expected_max_bytes=2048 * _KIB,
            checksum_algo="crc32",
            preflight_ar=("شاحن موصّل، كل الأحمال الكهربائية مقفولة، "
                          "والكونتاكت ON من غير تدوير."),
            success_message_ar=("تم تحديث الـ FEM/BDC والوحدة رجعت أونلاين على "
                                "الباص بالنسخة الجديدة."),
        ),
        FlashJob(
            code="kombi_update",
            name_ar="تحديث عداد التابلوه (KOMBI)",
            name_en="Instrument Cluster (KOMBI) Update",
            target_module="kombi",
            target_addr=0x00040000,
            needs_security=False,
            safety=_FLASH_SAFETY,
            expected_min_bytes=64 * _KIB,
            expected_max_bytes=1024 * _KIB,
            checksum_algo="crc32",
            preflight_ar=("شاحن موصّل والكونتاكت ON. التحديث بيطفّي شاشة "
                          "العداد لحظياً — ده طبيعي."),
            success_message_ar=("تم تحديث الـ KOMBI — الشاشة رجعت اشتغلت "
                                "بالنسخة الجديدة والساعة محتاجة ضبط."),
        ),
        FlashJob(
            code="egs_update",
            name_ar="تحديث كمبيوتر الجير (EGS / 8HP)",
            name_en="Transmission ECU (EGS) Update",
            target_module="egs",
            target_addr=0x000C0000,
            needs_security=True,
            safety=_FLASH_SAFETY,
            expected_min_bytes=128 * _KIB,
            expected_max_bytes=2048 * _KIB,
            checksum_algo="crc32",
            preflight_ar=("العربية على P، شاحن موصّل، والكونتاكت ON. بعد "
                          "التحديث محتاج تأقلم نقلات (adaptation) قبل التسليم."),
            success_message_ar=("تم تحديث الـ EGS — اعمل دورة تأقلم نقلات قبل "
                                "ما تسلّم العربية."),
        ),
    ]
}


def get_flash_job(code: str) -> FlashJob | None:
    return FLASH_CATALOG.get(code)


def all_flash_jobs() -> tuple[FlashJob, ...]:
    return tuple(FLASH_CATALOG.values())
