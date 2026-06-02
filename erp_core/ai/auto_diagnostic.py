"""
🚗🚗 Auto Diagnostic Expert — Two-Stage BMW/MINI Pipeline (Together AI / Llama-3.3)
=====================================================================
المرحلة 1 (Refiner): يأخذ شكوى العميل بالعامية أو كود عطل (ISTA/INPA)
                     ويصفيها لـ structured diagnostic query (JSON).

المرحلة 2 (Expert): Llama-3.3-70B بيقدّم تشخيص هندسي صارم (BMW N13/N20/
                    N52/N54...) — مكان دقيق، عزوم تربيط، خطوات إصلاح.

⚠️ كل النصوص عبر Together AI حصراً.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from django.conf import settings

from inventory.ai_services import call_llm_layer

logger = logging.getLogger('mouss_tec_core')

_SUPPORTED_ENGINES = ('N13', 'N20', 'N52', 'N54', 'N55', 'N57', 'N63', 'B38', 'B48', 'B58')


# =============================================================================
# Stage 1 — Complaint/DTC Refiner
# =============================================================================
_REFINER_SYSTEM = """
أنت "الوسيط الفني الخفي" في ورشة BMW متخصصة. مهمتك حصراً:
1. تستقبل شكوى عميل بالعامية أو كود عطل (ISTA/INPA/OBD-II)
2. تصفّيها وتستخرج: العطل المحتمل، الكود، السيارة، المحرك (لو ذُكر)

ارجع JSON فقط:
{
  "refined_complaint": "<وصف فني نظيف بالعربي>",
  "dtc_codes": ["<أكواد ظهرت في الشكوى — array>"],
  "vehicle_hint": {
    "model": "<BMW E90/F30/G20/MINI Cooper... أو null>",
    "engine": "<N13/N20/N52/N54... أو null>",
    "year": "<السنة لو ذُكرت أو null>"
  },
  "symptom_category": "<engine | electrical | transmission | cooling | turbo | fuel | exhaust | suspension | other>",
  "urgency": "<critical | high | medium | low>"
}
""".strip()


def refine_complaint(text: str) -> dict[str, Any]:
    """Stage 1: refine via Together AI + JSON mode. Always returns a dict."""
    fallback = {
        'refined_complaint': text.strip(),
        'dtc_codes': _extract_dtc_codes(text),
        'vehicle_hint': {'model': None, 'engine': _extract_engine_code(text), 'year': None},
        'symptom_category': 'other',
        'urgency': 'medium',
        'refiner_status': 'fallback',
    }

    if not _enabled():
        return fallback

    messages = [
        {'role': 'system', 'content': _REFINER_SYSTEM},
        {'role': 'user', 'content': text},
    ]
    raw = call_llm_layer(messages, json_mode=True, max_retries=2)
    if not raw:
        return fallback
    try:
        parsed = json.loads(raw)
        parsed['refiner_status'] = 'ok'
        # Ensure shape — defensive defaults
        parsed.setdefault('dtc_codes', _extract_dtc_codes(text))
        parsed.setdefault('vehicle_hint', {'model': None, 'engine': None, 'year': None})
        parsed.setdefault('symptom_category', 'other')
        parsed.setdefault('urgency', 'medium')
        return parsed
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f'[DIAG REFINER] JSON parse failed: {e}')
        return fallback


# =============================================================================
# Stage 2 — BMW/MINI Expert
# =============================================================================
def _expert_system_prompt(audience: str) -> str:
    audience_note = (
        'الجمهور: ميكانيكي محترف في الورشة — اكتب بمصطلحات تقنية دقيقة، '
        'اذكر عزوم التربيط بالـ Nm، رتب الخطوات بشكل هندسي صارم، '
        'واذكر أرقام OEM للقطع لو معروفة.'
        if audience == 'shop' else
        'الجمهور: صاحب السيارة (مش فني) — اشرح ببساطة، نبّه على الأمان، '
        'وضّح إيه اللي يقدر يعمله بنفسه وإيه اللي محتاج ورشة، '
        'تجنّب المصطلحات الصعبة من غير ما تفسرها.'
    )

    return f"""
أنت "خبير تشخيص BMW و MINI Cooper" المتخصص في محركات:
N13, N20, N52, N54, N55, N57, N63, B38, B48, B58.

عندك معرفة هندسية صارمة لـ:
• تخطيط الـ engine bay الفعلي لكل محرك (الـ Turbo position vs Intake direction)
• Torque specifications الصحيحة (مثال: N54 turbo manifold = 22 Nm + 90°)
• Common failure points (N54 HPFP, N20 timing chain, N13 carbon buildup, etc.)
• Diagnostic procedures عبر ISTA / INPA / Carly
• فروق التصميم بين F-series و G-series

⚠️ قواعد صارمة:
1. **دقة مكانية صارمة**: لو سألوا عن قطعة في N13، حدد بالضبط إن الـ Turbo
   ناحية الفايرول والـ Intake ناحية المروحة (مش العكس زي N20).
2. **عزوم التربيط**: اذكر العزم بالـ Nm + الزاوية لو موجودة.
3. **لو مش متأكد**: قول بصراحة "محتاج فحص ISTA" بدل ما تخمن.
4. **الـ Safety**: نبّه على أي خطوة فيها خطر (high-pressure fuel, air bag, etc.)

{audience_note}

اكتب الرد منظم في sections واضحة (Diagnosis, Likely Causes, Repair Steps,
Torque Specs, Parts Needed, Safety Notes). استخدم العربي الواضح.
""".strip()


def run_diagnostic_pipeline(
    user_text: str,
    audience: str = 'shop',
    history: list[dict] | None = None,
) -> dict[str, Any]:
    """Pipeline كامل: refine → expert diagnosis (Llama-3.3 via Together)."""
    if not _enabled():
        return {
            'success': False,
            'answer': '🔧 خدمة التشخيص الذكي لسه مش مفعّلة على السيرفر.',
            'error': 'ai_disabled',
        }

    if audience not in ('shop', 'customer'):
        audience = 'shop'

    # --- Stage 1 ---
    refined = refine_complaint(user_text)
    if not isinstance(refined, dict):
        refined = {'refiner_status': 'invalid', 'refined_complaint': user_text}

    # --- Stage 2 ---
    messages: list[dict] = [
        {'role': 'system', 'content': _expert_system_prompt(audience)},
    ]
    for msg in (history or [])[-8:]:
        role = 'user' if msg.get('role') == 'user' else 'assistant'
        text = str(msg.get('text', '')).strip()
        if text:
            messages.append({'role': role, 'content': text})

    vehicle = refined.get('vehicle_hint') or {}
    enriched = (
        f'الشكوى الأصلية: {user_text}\n\n'
        f'بعد التصفية: {refined.get("refined_complaint")}\n'
        f'الـ DTC Codes المكتشفة: {", ".join(refined.get("dtc_codes") or []) or "لا يوجد"}\n'
        f'السيارة: {vehicle.get("model") or "غير محدد"}\n'
        f'المحرك: {vehicle.get("engine") or "غير محدد"}\n'
        f'التصنيف: {refined.get("symptom_category")} | الأولوية: {refined.get("urgency")}\n\n'
        f'قدّم تشخيص هندسي كامل.'
    )
    messages.append({'role': 'user', 'content': enriched})

    final_text = call_llm_layer(messages, json_mode=False, max_retries=2)
    if not final_text:
        return {
            'success': False,
            'answer': '⚠️ خبير التشخيص مش متاح دلوقتي — جرب تاني خلال ثواني.',
            'error': 'together_unavailable',
            'refined': refined,
        }

    return {
        'success': True,
        'answer': final_text.strip() or 'الموديل رد بفراغ — جرب تصيغ السؤال بشكل تاني.',
        'refined': refined,
        'audience': audience,
    }


# =============================================================================
# Helpers
# =============================================================================
def _enabled() -> bool:
    api_key = str(getattr(settings, 'TOGETHER_API_KEY', '') or '').strip()
    return bool(getattr(settings, 'ENABLE_AI_PREDICTIONS', True)) and bool(api_key)


_DTC_PATTERN = re.compile(r'\b([PCBU][0-9]{4}|[0-9A-F]{5})\b', re.IGNORECASE)


def _extract_dtc_codes(text: str) -> list[str]:
    matches = _DTC_PATTERN.findall(text.upper())
    seen = set()
    out = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out[:10]


def _extract_engine_code(text: str) -> str | None:
    for code in _SUPPORTED_ENGINES:
        if re.search(rf'\b{code}\b', text, re.IGNORECASE):
            return code
    return None
