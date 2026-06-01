"""
🚗🚗 Auto Diagnostic Expert — Two-Stage BMW/MINI Pipeline
=====================================================================
المرحلة 1 (Refiner): يأخذ شكوى العميل بالعامية أو كود عطل (ISTA/INPA)
                     ويصفيها لـ structured diagnostic query.

المرحلة 2 (Expert): موديل Gemini متخصص في BMW + MINI Cooper مع تركيز
                    خاص على محركات N13 / N20 / N52 / N54، يقدم خطوات
                    إصلاح، تحديد مكان دقيق، وعزوم تربيط هندسية.

كل API calls محمية بـ try/except. الـ errors بترجع رسائل عربية أنيقة.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests
from django.conf import settings

from .advisor_agent import _api_key as _gemini_key, _GEMINI_BASE, _post_with_retry

logger = logging.getLogger('mouss_tec_core')

_TIMEOUT = 30
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

أمثلة:
- "العربية بتتنفض في الـ idle وبتطفي" → category=engine, urgency=high
- "P0301 BMW E90 N52" → dtc_codes=["P0301"], engine="N52", category=engine
- "صوت من التيربو وفقدان عزم F30 N13" → category=turbo, engine="N13", model="F30"
""".strip()


def refine_complaint(text: str) -> dict[str, Any]:
    """Stage 1: تنظيف الشكوى."""
    key = _gemini_key()
    fallback = {
        'refined_complaint': text.strip(),
        'dtc_codes': _extract_dtc_codes(text),
        'vehicle_hint': {'model': None, 'engine': _extract_engine_code(text), 'year': None},
        'symptom_category': 'other',
        'urgency': 'medium',
        'refiner_status': 'fallback',
    }
    if not key:
        return fallback

    model = getattr(settings, 'GEMINI_REFINER_MODEL', 'gemini-2.0-flash')
    url = f'{_GEMINI_BASE}/{model}:generateContent?key={key}'

    payload = {
        'systemInstruction': {'parts': [{'text': _REFINER_SYSTEM}]},
        'contents': [{'role': 'user', 'parts': [{'text': text}]}],
        'generationConfig': {
            'temperature': 0.1,
            'responseMimeType': 'application/json',
        },
    }

    try:
        resp = requests.post(url, json=payload, timeout=_TIMEOUT)
        if resp.status_code != 200:
            logger.warning(f'[DIAG REFINER] HTTP {resp.status_code}: {resp.text[:200]}')
            return fallback
        raw = resp.json()['candidates'][0]['content']['parts'][0]['text']
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())
        parsed = json.loads(raw)
        parsed['refiner_status'] = 'ok'
        return parsed
    except Exception as e:
        logger.warning(f'[DIAG REFINER] failed: {e}')
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

اكتب الرد منظم في secṭṭons واضحة (Diagnosis, Likely Causes, Repair Steps,
Torque Specs, Parts Needed, Safety Notes). استخدم العربي الواضح.
""".strip()


def run_diagnostic_pipeline(
    user_text: str,
    audience: str = 'shop',
    history: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Pipeline كامل: refine → expert diagnosis.

    Args:
        audience: 'shop' (الورشة/الفني) or 'customer' (صاحب السيارة).
    """
    key = _gemini_key()
    if not key:
        return {
            'success': False,
            'answer': '🔧 خدمة التشخيص الذكي لسه مش مفعّلة على السيرفر.',
            'error': 'ai_disabled',
        }

    if audience not in ('shop', 'customer'):
        audience = 'shop'

    # --- Stage 1 ---
    refined = refine_complaint(user_text)

    # --- Stage 2 ---
    model = getattr(settings, 'GEMINI_REASONING_MODEL', 'gemini-2.5-flash')
    url = f'{_GEMINI_BASE}/{model}:generateContent?key={key}'

    contents: list[dict] = []
    for msg in (history or [])[-8:]:
        role = 'user' if msg.get('role') == 'user' else 'model'
        text = str(msg.get('text', '')).strip()
        if text:
            contents.append({'role': role, 'parts': [{'text': text}]})

    enriched = (
        f'الشكوى الأصلية: {user_text}\n\n'
        f'بعد التصفية: {refined.get("refined_complaint")}\n'
        f'الـ DTC Codes المكتشفة: {", ".join(refined.get("dtc_codes") or []) or "لا يوجد"}\n'
        f'السيارة: {refined.get("vehicle_hint", {}).get("model") or "غير محدد"}\n'
        f'المحرك: {refined.get("vehicle_hint", {}).get("engine") or "غير محدد"}\n'
        f'التصنيف: {refined.get("symptom_category")} | الأولوية: {refined.get("urgency")}\n\n'
        f'قدّم تشخيص هندسي كامل.'
    )
    contents.append({'role': 'user', 'parts': [{'text': enriched}]})

    payload = {
        'systemInstruction': {'parts': [{'text': _expert_system_prompt(audience)}]},
        'contents': contents,
        'generationConfig': {'temperature': 0.25},
    }

    try:
        resp = _post_with_retry(url, payload)
    except Exception as e:
        logger.exception('[DIAG EXPERT] network failure')
        return {
            'success': False,
            'answer': '⚠️ مش قادر أوصل لخدمة التشخيص — جرب تاني بعد ثواني.',
            'error': str(e),
            'refined': refined,
        }

    if resp.status_code != 200:
        logger.error(f'[DIAG EXPERT] HTTP {resp.status_code}: {resp.text[:300]}')
        return {
            'success': False,
            'answer': '⚠️ خبير التشخيص مش متاح دلوقتي — جرب تاني خلال ثواني.',
            'error': f'gemini_http_{resp.status_code}',
            'refined': refined,
        }

    try:
        data = resp.json()
        parts = data['candidates'][0]['content'].get('parts', [])
        final_text = '\n'.join(p.get('text', '') for p in parts if 'text' in p).strip()
    except (KeyError, IndexError) as e:
        logger.error(f'[DIAG EXPERT] malformed response: {e}')
        return {
            'success': False,
            'answer': '⚠️ وصلني رد غير مفهوم — جرب تاني.',
            'error': 'malformed_response',
            'refined': refined,
        }

    return {
        'success': True,
        'answer': final_text or 'الموديل رد بفراغ — جرب تصيغ السؤال بشكل تاني.',
        'refined': refined,
        'audience': audience,
    }


# =============================================================================
# Local heuristics (fallback لو الـ refiner فشل)
# =============================================================================
_DTC_PATTERN = re.compile(r'\b([PCBU][0-9]{4}|[0-9A-F]{5})\b', re.IGNORECASE)


def _extract_dtc_codes(text: str) -> list[str]:
    """يستخرج أكواد DTC من النص (P0301, U0100, hex codes...)."""
    matches = _DTC_PATTERN.findall(text.upper())
    # Dedup مع الحفاظ على الترتيب
    seen = set()
    out = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out[:10]


def _extract_engine_code(text: str) -> str | None:
    """يستخرج كود المحرك (N13, N54, ...) من النص."""
    for code in _SUPPORTED_ENGINES:
        if re.search(rf'\b{code}\b', text, re.IGNORECASE):
            return code
    return None
