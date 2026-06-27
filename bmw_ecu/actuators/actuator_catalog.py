"""Catalog of bidirectional (actuator) tests.

A bidirectional test commands a component to MOVE so the technician can
physically confirm it works — spin the radiator fan, pulse an injector,
cycle the EGR valve, click the AC clutch, sound the horn. On the bus this
is InputOutputControlByIdentifier (UDS 0x2F): the tester takes control of
an output, drives it, then ALWAYS returns control to the ECU.

Each test is declarative — module, the IO DID, how it's driven
(ACTIVATE / PULSE / CYCLE), its safety pre-conditions, and bilingual copy
including the exact question to ask the technician while the component is
running ("هل المروحة بتلف؟"). ONE generic orchestrator interprets any of
them. All gated behind the single saleable feature 'bidirectional_tests'.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

from ..premium.safety_checks import (
    GearPosition,
    IgnitionState,
    SafetyRequirement,
)


class ControlKind(str, enum.Enum):
    ACTIVATE = "activate"   # energise for a fixed duration (fan, pump, horn)
    PULSE    = "pulse"      # N discrete pulses (injector, solenoid)
    CYCLE    = "cycle"      # open → close → open (EGR, throttle, window)


BIDIRECTIONAL_FEATURE = "bidirectional_tests"


@dataclass(frozen=True)
class ActuatorTest:
    code: str
    name_ar: str
    name_en: str
    target_module: str
    control_kind: ControlKind
    io_did: int                  # IOControlByIdentifier data id
    needs_security: bool
    safety: SafetyRequirement
    default_duration_s: int      # how long ACTIVATE runs / PULSE total
    observe_question_ar: str     # asked while the component is driven
    feature_code: str = BIDIRECTIONAL_FEATURE

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name_ar": self.name_ar,
            "name_en": self.name_en,
            "target_module": self.target_module,
            "control_kind": self.control_kind.value,
            "io_did": f"0x{self.io_did:04X}",
            "needs_security": self.needs_security,
            "default_duration_s": self.default_duration_s,
            "observe_question_ar": self.observe_question_ar,
            "feature_code": self.feature_code,
        }


# Engine-bay tests run KOEO (key on, engine OFF) so a forced output isn't
# fighting the running ECU and nobody's hand is near a belt.
_KOEO = SafetyRequirement(voltage_min_v=12.0,
                          ignition_in=(IgnitionState.KOEO,))


ACTUATOR_CATALOG: dict[str, ActuatorTest] = {
    a.code: a for a in [
        ActuatorTest(
            "radiator_fan", "مروحة التبريد", "Radiator Cooling Fan",
            "dme", ControlKind.ACTIVATE, 0xF010, False, _KOEO, 8,
            "هل المروحة بتلف بسرعة كاملة دلوقتي؟",
        ),
        ActuatorTest(
            "fuel_pump", "طلمبة البنزين", "Fuel Pump",
            "dme", ControlKind.ACTIVATE, 0xF011, False, _KOEO, 5,
            "هل بتسمع صوت الطلمبة شغّالة وضغط الريل بيطلع؟",
        ),
        ActuatorTest(
            "injector_1", "بخاخ السلندر 1", "Injector — Cylinder 1",
            "dme", ControlKind.PULSE, 0xF021,
            needs_security=True, safety=_KOEO, default_duration_s=3,
            observe_question_ar="هل سمعت صوت نقر البخاخ (clicking) 5 مرات؟",
        ),
        ActuatorTest(
            "egr_valve", "صمام الـ EGR", "EGR Valve",
            "dme", ControlKind.CYCLE, 0xF030, True, _KOEO, 6,
            "هل الصمام بيفتح ويقفل بسلاسة (من غير ما يعلّق)؟",
        ),
        ActuatorTest(
            "purge_valve", "صمام تبخير الوقود (Purge)", "EVAP Purge Valve",
            "dme", ControlKind.CYCLE, 0xF031, False, _KOEO, 4,
            "هل بتسمع نقر الصمام وهو بيفتح ويقفل؟",
        ),
        ActuatorTest(
            "ac_compressor", "كلتش التكييف", "A/C Compressor Clutch",
            "ihka", ControlKind.ACTIVATE, 0xF040, False, _KOEO, 5,
            "هل كلتش الكمبروسر اشتبك (صوت تك) واتفصل تاني؟",
        ),
        ActuatorTest(
            "horn", "الكلاكس", "Horn",
            "fem", ControlKind.ACTIVATE, 0xF050, False, _KOEO, 2,
            "هل الكلاكس صوّت؟",
        ),
        ActuatorTest(
            "door_lock", "قفل الأبواب المركزي", "Central Door Lock",
            "fem", ControlKind.CYCLE, 0xF051, False, _KOEO, 4,
            "هل كل الأبواب قفلت وفتحت مع الأمر؟",
        ),
    ]
}


def get_actuator(code: str) -> ActuatorTest | None:
    return ACTUATOR_CATALOG.get(code)


def all_actuators() -> tuple[ActuatorTest, ...]:
    return tuple(ACTUATOR_CATALOG.values())
