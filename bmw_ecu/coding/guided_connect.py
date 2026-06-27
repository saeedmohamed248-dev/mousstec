"""Real-world guided connect → read → code assessment.

This is the brain behind the Coding Room's "اتصال وقراءة / Connect & Read"
button. A technician with a real ENET (or K+D-CAN) cable clicks Connect;
the backend reads the car and then tells them — in plain bilingual steps —
exactly what to do next:

  • If the target module is OPEN (low protection + known software path):
    "الكنترول مفتوح — كوّد مباشرة بكابل الاي نت."
    → they go straight to Load features / Apply.

  • If the module is LOCKED (HIGH/CRITICAL protection, or bench-only):
    we hand back a full step-by-step removal + bench-wiring procedure WITH
    the pinout diagram and coloured pin callouts, e.g.
        1. اطفي الكونتاكت وافصل البطارية
        2. فك الكنترول
        3. وصّل 12V على بِن X و GND على بِن Y
        4. وصّل D-CAN: CAN-H بِن .., CAN-L بِن ..
        5. نزّل بِن البوت N على GND لدخول الـ BSL
        6. صوّر الـ PCB من جوه وارفعها للمراجعة قبل أي كتابة

The procedure pins are derived from the live PinoutRepository so it always
matches the real connector for that ECU family.

Pure-async, hardware-free: it only reads from the profile + pinout repo
(plus an optional VIN read that degrades gracefully). Fully unit-tested.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from ..execution.ecu_profiles import EcuProfile, ProtectionLevel
from ..execution.interactive_guided.pinout_repository import (
    PinoutDiagram, PinoutRepository,
)


@dataclass
class GuidedStep:
    n: int
    ar: str
    en: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConnectAssessment:
    """The verdict returned to the frontend after Connect & Read."""
    vin: str
    ecu_name: str
    chassis: str
    engine: str
    protection: str                       # "OPEN".."CRITICAL"
    locked: bool
    cable: str                            # "enet" | "dcan_bench"
    headline_ar: str
    headline_en: str
    pinout_diagram_url: Optional[str] = None
    pinout_callouts: list[dict] = field(default_factory=list)
    steps: list[GuidedStep] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["steps"] = [s.to_json() for s in self.steps]
        return d


# --- pin extraction ---------------------------------------------------------
def _find_pin(callouts: list[dict], *keywords: str) -> Optional[int]:
    """First callout whose label contains ANY keyword (case-insensitive)."""
    for c in callouts:
        label = str(c.get("label", "")).lower()
        if any(k.lower() in label for k in keywords):
            pin = c.get("pin")
            if isinstance(pin, int):
                return pin
            m = re.search(r"\d+", str(pin or ""))
            if m:
                return int(m.group())
    return None


def _pin_or(callouts: list[dict], fallback: str, *keywords: str) -> str:
    pin = _find_pin(callouts, *keywords)
    return str(pin) if pin is not None else fallback


# --- step builders ----------------------------------------------------------
def _open_steps() -> list[GuidedStep]:
    return [
        GuidedStep(
            1,
            "الكنترول مفتوح ✅ — سيب كابل الاي نت (ENET) متوصّل في فيشة OBD "
            "والكونتاكت ON.",
            "Module is OPEN ✅ — keep the ENET cable in the OBD port with "
            "ignition ON.",
        ),
        GuidedStep(
            2,
            "اضغط «📋 Load features» علشان السيستم يقرا الميزات المتاحة.",
            "Press “📋 Load features” to read the available features.",
        ),
        GuidedStep(
            3,
            "اختار الميزة اللي عايز تفعّلها أو توقفها واضغط «🚀 Apply». "
            "بعد كده دوّر المفتاح OFF/ON.",
            "Pick the feature(s) to enable/disable and press “🚀 Apply”, "
            "then cycle the ignition.",
        ),
    ]


def _locked_steps(profile: EcuProfile,
                  diagram: Optional[PinoutDiagram]) -> list[GuidedStep]:
    callouts = list(diagram.callouts) if diagram else []
    ecu = profile.name
    kl30 = _pin_or(callouts, "KL30", "kl30", "12v")
    gnd = _pin_or(callouts, "KL31", "gnd", "ground")
    canh = _pin_or(callouts, "CAN-H", "can high", "can-h", "can h")
    canl = _pin_or(callouts, "CAN-L", "can low", "can-l", "can l")
    boot = (str(profile.boot_pin) if profile.boot_pin is not None
            else _pin_or(callouts, "BOOT", "boot", "bsl"))

    return [
        GuidedStep(
            1,
            "🔒 الكنترول مقفول (حماية عالية) — مش هينفع نكتب عليه وهو راكب. "
            "اطفي الكونتاكت (Ignition OFF) وافصل طرف البطارية الموجب 5 دقايق.",
            f"🔒 The {ecu} is LOCKED (high protection) — it can't be written "
            "in-car. Switch ignition OFF and disconnect the battery positive "
            "for 5 minutes.",
        ),
        GuidedStep(
            2,
            f"فك الكنترول {ecu} من مكانه وافصل كل الفيش عنه، وحطّه على "
            "طاولة الشغل (bench).",
            f"Remove the {ecu} module, unplug all its connectors, and place "
            "it on the bench.",
        ),
        GuidedStep(
            3,
            f"وصّل تغذية 12V: الموجب على بِن {kl30} (KL30) والأرضي (GND) "
            f"على بِن {gnd}.",
            f"Feed 12V power: positive to pin {kl30} (KL30), ground (GND) "
            f"to pin {gnd}.",
        ),
        GuidedStep(
            4,
            f"وصّل كابل D-CAN: سلك CAN-High على بِن {canh} وسلك CAN-Low "
            f"على بِن {canl}.",
            f"Connect the D-CAN cable: CAN-High to pin {canh}, CAN-Low to "
            f"pin {canl}.",
        ),
        GuidedStep(
            5,
            f"نزّل بِن البوت رقم {boot} على GND لحظة واحدة وانت بتدّي باور "
            "علشان الكنترول يدخل وضع الـ bootloader (BSL).",
            f"Momentarily ground BOOT pin {boot} while powering up so the "
            "module enters bootloader (BSL) mode.",
        ),
        GuidedStep(
            6,
            "📸 صوّر بورده الكنترول من جوه (PCB) بوضوح وارفع الصورة هنا — "
            "هنراجع نقاط اللحام والبِن قبل أي كتابة علشان ما نبوّظش الكنترول.",
            "📸 Photograph the module's PCB clearly and upload it here — we "
            "verify the solder points and pin before any write, so the "
            "module is never bricked.",
        ),
    ]


# --- public API -------------------------------------------------------------
async def assess_connection(
    *,
    profile: EcuProfile,
    vin: str,
    chassis: str = "",
    pinout_repo: Optional[PinoutRepository] = None,
) -> ConnectAssessment:
    """Decide OPEN vs LOCKED and build the matching guided procedure."""
    repo = pinout_repo or PinoutRepository()
    diagram = await repo.get(profile.name)

    open_module = profile.supports_software_only()
    if open_module:
        steps = _open_steps()
        cable = "enet"
        headline_ar = (
            f"تمام ✅ قريت السيارة. الكنترول {profile.name} مفتوح وتقدر "
            "تكوّد مباشرة بكابل الاي نت."
        )
        headline_en = (
            f"Read OK ✅ — {profile.name} is OPEN; you can code directly "
            "over the ENET cable."
        )
    else:
        steps = _locked_steps(profile, diagram)
        cable = "dcan_bench"
        headline_ar = (
            f"قريت السيارة 🔒 الكنترول {profile.name} محمي ومحتاج bench + "
            "D-CAN. اتبع الخطوات بالترتيب."
        )
        headline_en = (
            f"Read the car 🔒 — {profile.name} is protected and needs "
            "bench + D-CAN. Follow the steps in order."
        )

    return ConnectAssessment(
        vin=vin,
        ecu_name=profile.name,
        chassis=chassis or (profile.chassis[0] if profile.chassis else ""),
        engine=profile.engine,
        protection=ProtectionLevel(profile.protection).name,
        locked=not open_module,
        cable=cable,
        headline_ar=headline_ar,
        headline_en=headline_en,
        pinout_diagram_url=diagram.image_url if diagram else None,
        pinout_callouts=list(diagram.callouts) if diagram else [],
        steps=steps,
    )
