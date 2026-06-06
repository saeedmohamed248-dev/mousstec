"""
🤖 AI Diagnostics Room — multi-turn co-pilot for the live workshop chat.

Pipeline:
    1. Snapshot the JS-supplied live telemetry + DTC list into a compact
       Arabic context block.
    2. Build a chat history capped at the last 8 turns (cost + relevance).
    3. Inject our expert system prompt — same expertise as
       `auto_diagnostic.diagnose()` but tuned for back-and-forth coaching.
    4. Call Together via `inventory.ai_services.call_llm_layer`.
    5. Return { answer, used_dtcs, context_summary } to the front-end.

We deliberately don't run the two-stage refiner here — by the time the tech
is talking to us they already have live data + DTCs from the car. The
context is hard-grounded, not free-text.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("mouss_tec_core")

_MAX_HISTORY_TURNS = 8
_MAX_LIVE_FIELDS = 12


def _system_prompt() -> str:
    return (
        "أنت Master Diagnostic Technician — كبير مهندسي التشخيص في ورشة Mouss Tec، "
        "بخبرة 25 سنة على BMW/Mercedes/Toyota/Hyundai/Kia/Nissan + خبرة عميقة "
        "في الـ Wiring Diagrams و Pinouts و Bus Networks (CAN / LIN / FlexRay). "
        "بتقود فني واقف عند السيارة بـ Multimeter و Scope في إيده، والـ OBD-II موصول. "
        "الـ Live Data + DTCs + الـ VIN بتجيلك مع كل رسالة.\n\n"

        "🎯 منهجك الإلزامي (لا تخرج عنه):\n"
        "  1. اقرأ الـ VIN: حدد الـ Make/Model/Year/Engine code من رقم الشاسيه:\n"
        "     - Position 1-3 (WMI) = الصانع (مثلاً WBA=BMW Sedan, JTD=Toyota, KMH=Hyundai).\n"
        "     - Position 10 = سنة الموديل (A=2010, B=2011, ... Y=2030).\n"
        "     - استخدم الـ VDS positions 4-8 للموديل والمحرك.\n"
        "     قول للفني صراحة: 'السيارة دي [الموديل] [السنة] محرك [الكود]'.\n"
        "  2. ربط الأكواد بالـ Architecture: لو الموديل معروف باعطال مزمنة في "
        "     منطقة معينة (مثلاً N20 = سلسلة تايمنج، EA888 = استهلاك زيت، 2GR-FE = "
        "     OCV solenoids) — نبّه الفني من أول رد.\n"
        "  3. Pin-Level Testing (إلزامي): متقولش 'غيّر الحساس'. قول للفني بالضبط:\n"
        "     - 'افصل الكونكتر الفلاني'\n"
        "     - 'حدد البِنّات بالاسم (Pin 1 = VBatt, Pin 2 = GND, Pin 3 = 5V Ref, Pin 4 = Signal)'\n"
        "     - 'القراءة المتوقعة بالـ Multimeter: V أو Ω محدد'\n"
        "     - 'لو القراءة [كذا] → التشخيص [كذا]'\n"
        "     - 'لو القراءة [كذا التانية] → ارجع للـ ECU/الفيوز/الريلاي (اذكر الفيوز رقمه إن كنت متأكد)'\n"
        "  4. ترتيب الفحوصات Cheap → Expensive: Visual → Voltage → Resistance → "
        "     Wave-form (Scope) → ECU adaptations → Component replacement.\n"
        "  5. اربط الأكواد المرتبطة (مثلاً P0171+P0300 = lean misfire، شوف vacuum leak "
        "     قبل ما تتهم الـ injectors).\n"
        "  6. لو الـ Live Data ناقصة لقرار التشخيص — اطلب من الفني يقرأها (مثلاً 'محتاج "
        "     STFT Bank1 و LTFT Bank1 من الـ scanner').\n\n"

        "📐 صيغة الرد المطلوبة لما يجي DTC:\n"
        "  [التحليل الأولي] جملة واحدة عن معنى الكود في سياق السيارة دي.\n"
        "  [الفحص 1 — Visual / Cheap] خطوات.\n"
        "  [الفحص 2 — Electrical / Pin-Level] بالـ pinout الكامل والـ voltages المتوقعة.\n"
        "  [الفحص 3 — Component / Advanced] لو 1 و 2 سليمين.\n"
        "  [قرار] لو طلع كذا → اعمل كذا.\n\n"

        "🚫 ممنوع: تكتب كود برمجة، تذكر أسعار قطع، تقول 'غيّر الجزء' بدون pin-test، "
        "تخمن VIN غير موجود، تتجاهل القراءات الحية لو متاحة.\n"
        "✅ مسموح: تقول 'مش متأكد من الـ pinout الدقيق لهذا الموديل تحديداً، استخدم "
        "wiring diagram السيارة' لو فعلاً الموديل غامض — الصدق الفني أهم من التخمين."
    )


# ── VIN decoder (lightweight, no external API) ──────────────────────
_WMI_MAP = {
    # World Manufacturer Identifier — first 3 chars of VIN
    'WBA': 'BMW (Sedan)',    'WBS': 'BMW M',          'WBY': 'BMW i',
    'WDB': 'Mercedes-Benz',  'WDC': 'Mercedes-Benz SUV',
    'WDD': 'Mercedes-Benz',  'WAU': 'Audi',           'WVW': 'Volkswagen',
    'WV1': 'VW Commercial',  'WP0': 'Porsche',        'WP1': 'Porsche SUV',
    'JTD': 'Toyota',         'JTM': 'Toyota',         'JTE': 'Toyota',
    'JHM': 'Honda',          'JHL': 'Honda',          'JN1': 'Nissan',
    'JN6': 'Nissan',         'JN8': 'Nissan',         'JF1': 'Subaru',
    'KMH': 'Hyundai',        'KNA': 'Kia',            'KND': 'Kia',
    'KNH': 'Kia',            '1G1': 'Chevrolet',      '1FA': 'Ford',
    '1FT': 'Ford Truck',     '1HG': 'Honda US',       '1J4': 'Jeep',
    '2HG': 'Honda Canada',   '3VW': 'VW Mexico',      '5NP': 'Hyundai US',
    'SAJ': 'Jaguar',         'SAL': 'Land Rover',     'VF1': 'Renault',
    'VF3': 'Peugeot',        'VF7': 'Citroën',        'ZFA': 'Fiat',
    'ZAR': 'Alfa Romeo',     'ZAM': 'Maserati',       'YV1': 'Volvo',
    'TMB': 'Skoda',          'TRU': 'Audi Hungary',
}

_YEAR_MAP = {
    # VIN position 10 — model year (10th char)
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026, 'V': 2027, 'W': 2028, 'X': 2029,
    'Y': 2030, '1': 2031, '2': 2032, '3': 2033,
}


def _decode_vin(vin: str | None) -> dict[str, Any]:
    """Lightweight VIN decode — no external API call. Pulls WMI + model year
    so the prompt can hand the LLM a concrete starting point (the LLM still
    does the heavy lifting on engine code / trim from VDS positions 4-8)."""
    if not vin or len(vin) != 17:
        return {}
    vin = vin.upper()
    return {
        "vin": vin,
        "wmi": vin[:3],
        "make": _WMI_MAP.get(vin[:3], "غير معروف"),
        "vds": vin[3:9],
        "model_year": _YEAR_MAP.get(vin[9]),
        "plant_code": vin[10],
        "serial": vin[11:],
    }


def _format_snapshot(snapshot: dict[str, Any]) -> str:
    if not snapshot:
        return "لا يوجد بث حي حالياً."
    rows = []
    units = {
        "rpm": "rpm", "speed_kph": "km/h",
        "coolant_temp_c": "°C", "intake_temp_c": "°C", "oil_temp_c": "°C",
        "engine_load": "%", "throttle_pct": "%", "fuel_level_pct": "%",
        "maf_gs": "g/s", "control_voltage": "V",
    }
    for k, v in list(snapshot.items())[:_MAX_LIVE_FIELDS]:
        if k.startswith("_"):
            continue
        unit = units.get(k, "")
        try:
            rows.append(f"  • {k} = {float(v):.2f} {unit}".rstrip())
        except (TypeError, ValueError):
            rows.append(f"  • {k} = {v}")
    return "\n".join(rows) if rows else "لا يوجد بث حي حالياً."


def _format_dtcs(dtcs: list[str]) -> str:
    if not dtcs:
        return "لا توجد أكواد أعطال حالياً (Mode 03 رجع فاضي)."
    return ", ".join(dict.fromkeys(c.strip().upper() for c in dtcs if c))


def _format_vehicle(hint: dict[str, Any], vin_info: dict[str, Any]) -> str:
    # VIN decode is the source of truth when present; hint is a soft override.
    if vin_info:
        parts = [
            f"VIN={vin_info['vin']}",
            f"Make={vin_info['make']} (WMI={vin_info['wmi']})",
            f"Model Year={vin_info.get('model_year') or 'غير محدد'}",
            f"VDS={vin_info['vds']} (يستخدم لتحديد الموديل/المحرك)",
        ]
        if hint.get("model"):
            parts.append(f"override.model={hint['model']}")
        if hint.get("engine"):
            parts.append(f"override.engine={hint['engine']}")
        return "\n  ".join(parts)
    if not hint:
        return "VIN غير متاح (سيارة قديمة قبل 2008 أو لم يتم القراءة) — اطلب من "\
               "الفني يأكدلك الموديل والسنة والمحرك يدوياً."
    parts = [hint.get("model"), hint.get("engine"), hint.get("year")]
    return " / ".join(str(p) for p in parts if p) or "غير محدد"


def _build_context_block(*, snapshot, dtcs, vehicle_hint, vin) -> str:
    vin_info = _decode_vin(vin)
    return (
        "═══ سياق السيارة الحالي (مبثوث من الـ ELM327) ═══\n"
        f"السيارة:\n  {_format_vehicle(vehicle_hint, vin_info)}\n\n"
        f"DTCs:\n  {_format_dtcs(dtcs)}\n\n"
        f"Live Data:\n{_format_snapshot(snapshot)}\n"
        "═════════════════════════════════════════════════"
    )


def _opening_turn(*, snapshot, dtcs) -> str:
    """First message when the tech hasn't typed anything yet."""
    if dtcs:
        return (
            "اتفضل أول قراءة من السيارة. ابدأ بتحليل الأكواد الموجودة "
            "واقترح خطة فحص عملية للفني، بترتيب الأرخص فالأغلى."
        )
    return (
        "السيارة موصولة والبث الحي شغال — مفيش DTCs مخزنة. "
        "لو الفني عنده شكوى تشغيلية معينة، طمنه على القراءات الحية "
        "اللي قدامك واقترح فحوصات تأكيدية."
    )


def answer_room_turn(
    *,
    history: list[dict],
    user_message: str,
    snapshot: dict[str, Any],
    dtcs: list[str],
    vehicle_hint: dict[str, Any],
    vin: str | None = None,
    tenant=None,
    user=None,
) -> dict[str, Any]:
    """Single turn of the diagnostics-room chat. Returns:
        {
          "success": bool,
          "answer": str,
          "context_summary": str,
          "used_dtcs": [...],
        }
    """
    from inventory.ai_services import call_llm_layer

    context_block = _build_context_block(
        snapshot=snapshot, dtcs=dtcs, vehicle_hint=vehicle_hint, vin=vin,
    )

    messages: list[dict] = [
        {"role": "system", "content": _system_prompt()},
    ]
    for turn in (history or [])[-_MAX_HISTORY_TURNS:]:
        role = "user" if turn.get("role") == "user" else "assistant"
        text = str(turn.get("text", "")).strip()
        if text:
            messages.append({"role": role, "content": text})

    # The latest user turn gets the live context glued on, so the model
    # always sees fresh telemetry — even if the chat history is long.
    user_block = user_message or _opening_turn(snapshot=snapshot, dtcs=dtcs)
    messages.append({
        "role": "user",
        "content": f"{context_block}\n\nسؤال الفني: {user_block}",
    })

    answer = call_llm_layer(messages, json_mode=False, max_retries=2)
    if not answer:
        logger.warning(
            "[Diag Room] LLM returned empty — tenant=%s user=%s dtcs=%s",
            getattr(tenant, "schema_name", None),
            getattr(user, "username", None), dtcs,
        )
        return {
            "success": False,
            "answer": "⚠️ خبير التشخيص مش متاح حالياً — جرب تاني خلال ثواني.",
            "context_summary": context_block,
            "used_dtcs": dtcs,
        }

    return {
        "success": True,
        "answer": answer.strip(),
        "context_summary": context_block,
        "used_dtcs": dtcs,
    }
