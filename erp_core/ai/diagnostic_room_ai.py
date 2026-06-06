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
        "أنت كبير مهندسي تشخيص السيارات في ورشة Mouss Tec. "
        "بتساعد فني واقف عند السيارة، الـ OBD-II موصول، والبيانات الحية + أكواد الأعطال "
        "بتجيلك مع كل رسالة من المستخدم.\n\n"
        "أسلوبك:\n"
        "  • لغة عربية فنية، مختصرة، بدون حشو.\n"
        "  • لما الفني يسأل سؤال عام — جاوب جواب مباشر.\n"
        "  • لما تيجي بيانات DTC جديدة أو قيم حية شاذة — اقترح أول 3 فحوصات عملية، "
        "    بترتيب الأرخص فالأغلى (Cheap → Expensive).\n"
        "  • قول بصراحة لو محتاج قراءة إضافية (مثلاً Freeze Frame, Mode 06).\n"
        "  • متخمنش — لو الـ Live Data ناقصة، اطلبها من الفني.\n"
        "  • اربط بين الأكواد لما تكون ذات صلة (مثلاً P0171 + P0300 = مشكلة هواء/خليط).\n\n"
        "ممنوع: تكتب كود برمجة، تذكر أسعار، تقدر إصلاح تخميني بدون فحص."
    )


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


def _format_vehicle(hint: dict[str, Any]) -> str:
    if not hint:
        return "غير محدد"
    parts = [hint.get("model"), hint.get("engine"), hint.get("year")]
    return " / ".join(str(p) for p in parts if p) or "غير محدد"


def _build_context_block(*, snapshot, dtcs, vehicle_hint) -> str:
    return (
        "═══ سياق السيارة الحالي (مبثوث من الـ ELM327) ═══\n"
        f"السيارة: {_format_vehicle(vehicle_hint)}\n\n"
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
        snapshot=snapshot, dtcs=dtcs, vehicle_hint=vehicle_hint,
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
