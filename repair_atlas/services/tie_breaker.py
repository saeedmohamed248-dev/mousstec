"""
⚖️ Tie-Breaker — Verifier ثالث للحالات المتوسطة (Devil's Advocate).

لما V2 (الـ Verifier الأساسي) بيدّي confidence متوسط (60-84)، الرد مش رافض ومش
معتمد. هنا V3 بيقوم بدور "محامي الشيطان" — مهمته إيجاد عيب جوهري.

V3 system prompt مختلف عن V2:
    • V2 = Senior Reviewer هادي ومنظم.
    • V3 = Devil's Advocate — مهمته يلاقي حاجة غلط حتى لو الكل قال صح.

النتيجة بترجع verdict واحد من اتنين:
    • 'approve'  → ارفع الـ confidence لـ 90 وأعتمد (auto-promote).
    • 'sustain'  → سيب الـ tier medium، الفني يشوف 🟡 ويتأكد بنفسه.
    • 'overrule' → خفض الـ tier لـ low، الفني يشوف 🔴.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from inventory.ai_services import call_llm_layer

logger = logging.getLogger('mouss_tec_core')


_TIE_BREAKER_SYSTEM = (
    "أنت Devil's Advocate في ورشة Mouss Tec. وصلك رد فني من AI + ملاحظات "
    "مراجع تاني. مهمتك تكون **متشكك بشكل عدائي**. تفترض إن الرد فيه "
    "غلط حتى لو كل اللي قبلك قالوا حلو. مش هتقول 'حلو' غير لو فعلاً "
    "مفيش أي شك فيزيكي أو هندسي.\n\n"

    "🎯 ركّز على:\n"
    "  1. ادعاءات specific (أرقام، أكواد، أسماء قطع) بدون مصدر: شك.\n"
    "  2. تعميمات ('في غالبية العربيات') على سؤال موديل محدد: شك.\n"
    "  3. تجاهل تحذيرات السلامة (Airbag، HV، Fuel pressure): فادح.\n"
    "  4. ادعاء pinout / wire color / connector code بدون reference: شك.\n"
    "  5. ترتيب خطوات يمكن يؤذي قطعة (مثلاً تشغيل بدون ملء زيت): فادح.\n\n"

    "📐 الإخراج JSON صارم:\n"
    "{\n"
    '  "decision": "approve" | "sustain" | "overrule",\n'
    '  "critical_issue": "<جملة واحدة لو في عيب جوهري، أو null>",\n'
    '  "reasoning": "<سطرين كحد أقصى ليه قررت كده>"\n'
    "}\n\n"

    "Decision rules:\n"
    "  • 'approve'  = فحصت ومفيش شك حقيقي → ارفع الثقة.\n"
    "  • 'sustain'  = الرد محتمل لكن مش متأكد 100% → سيب التقدير وسط.\n"
    "  • 'overrule' = لقيت critical_issue، الرد غلط أو خطر → خفّض الثقة.\n\n"

    "🚫 ممنوع 'approve' لو في تعميم واحد على الأقل أو ادعاء specific بدون reference."
)


_JSON_BLOCK_RE = re.compile(r'\{[\s\S]*\}')


def tie_break(
    *,
    question: str,
    answer: str,
    mode: str,
    vehicle: dict[str, Any] | None,
    v2_doubts: list[str],
    v2_confidence: int,
) -> dict[str, Any]:
    """V3 pass. Always returns dict."""
    vehicle = vehicle or {}
    v_line = ' '.join(str(vehicle.get(k, '')) for k in
                       ('brand', 'model_name', 'year') if vehicle.get(k))
    doubts_block = '\n'.join(f'  - {d}' for d in v2_doubts) or '  - (مفيش)'

    user_block = (
        f"=== سياق ===\n"
        f"الوضع: {mode}\nالعربية: {v_line or 'غير محددة'}\n\n"
        f"=== سؤال الفني ===\n{question}\n\n"
        f"=== رد AI ===\n{answer}\n\n"
        f"=== شكوك Reviewer (V2) — confidence={v2_confidence} ===\n"
        f"{doubts_block}\n\n"
        f"اطلع قرارك JSON."
    )

    messages = [
        {'role': 'system', 'content': _TIE_BREAKER_SYSTEM},
        {'role': 'user', 'content': user_block},
    ]

    try:
        raw = call_llm_layer(messages, json_mode=True, max_retries=1)
    except Exception as e:
        logger.exception('[TIE_BREAKER] crashed')
        return _fallback(reason=f'crash:{e}')

    return _parse(raw)


def _parse(raw: Any) -> dict[str, Any]:
    if not raw:
        return _fallback(reason='empty')

    if isinstance(raw, dict):
        data = raw
    else:
        text = str(raw).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            m = _JSON_BLOCK_RE.search(text)
            if not m:
                return _fallback(reason='non_json')
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return _fallback(reason='invalid_json')

    decision = str(data.get('decision', '')).lower().strip()
    if decision not in {'approve', 'sustain', 'overrule'}:
        decision = 'sustain'

    critical = data.get('critical_issue')
    if critical is not None:
        critical = str(critical).strip() or None

    reasoning = str(data.get('reasoning', '')).strip()

    return {
        'decision': decision,
        'critical_issue': critical,
        'reasoning': reasoning[:600],
    }


def _fallback(reason: str) -> dict[str, Any]:
    logger.warning('[TIE_BREAKER] fallback: %s', reason)
    return {
        'decision': 'sustain',
        'critical_issue': None,
        'reasoning': f'تعذّر التحقق ({reason}) — التقدير سيب وسط.',
    }
