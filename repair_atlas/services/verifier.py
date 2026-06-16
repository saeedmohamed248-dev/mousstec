"""
🔎 Verifier — البوت يراجع نفسه.

بدل ما SuperAdmin يراجع كل رد، LLM تاني يقوم بدور الـ Senior Reviewer:
  • يفحص رد Generator adversarially (شك في كل حاجة).
  • يطلع confidence score (0-100) + قائمة شكوك + اقتراح تحسين.
  • النتيجة JSON صارمة — عشان نقدر نتفرع منها برمجياً.

نتيجة الاستدعاء:
    {
      'confidence': 0..100,
      'verdict': 'pass' | 'revise' | 'reject',
      'doubts': [str, ...],
      'suggested_revision': str | None,
    }

Thresholds (مستخدمين في coach_reply):
    ≥85 → AUTO_PROMOTE (الإجابة تتسحب من DB في المستقبل بدون LLM)
    60-84 → REVISE (نطلب من Generator يعيد ويعالج الشكوك)
    <60  → FLAG (الفني يشوف badge أحمر "غير مؤكد")
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from inventory.ai_services import call_llm_layer

logger = logging.getLogger('mouss_tec_core')


_VERIFIER_SYSTEM = (
    "أنت Senior Quality Reviewer في ورشة Mouss Tec بخبرة 30 سنة على كل ماركات "
    "السيارات + خلفية في الـ Wiring Diagrams و workshop manuals الرسمية. "
    "مهمتك تفحص رد فني صادر من AI مساعد، **adversarially** — يعني تفترض "
    "إنه ممكن يكون فيه غلط حتى لو شكله سليم.\n\n"

    "🎯 الفحوصات الإلزامية على كل رد:\n"
    "  1. Specifics check: لو فيه أرقام (عزم تربيط Nm، voltage V، resistance Ω، "
    "     مقاسات سبانر)، هل هي معقولة وحقيقية لهذا الموديل؟ أرقام مجنونة "
    "     (مثلاً 500 Nm لمسمار حساس) = شك فوري.\n"
    "  2. Steps order: هل ترتيب الخطوات منطقي؟ (مثلاً فصل البطارية قبل أي شغل "
    "     كهرباء، تفريغ الفريون قبل فك التكييف).\n"
    "  3. Part location/naming: هل اسم القطعة ومكانها صح لهذا الموديل؟ شك "
    "     بشكل خاص لو الرد عمّم ('في غالبية العربيات') بدون ما يحدد الموديل.\n"
    "  4. Safety: هل التحذيرات كافية؟ (Airbag، High-voltage hybrid، fuel pressure).\n"
    "  5. Connectors/pinouts: لو ذكر pin numbers أو ألوان أسلاك، هل هي محددة "
    "     للموديل ولا تخمين؟ التخمين في الـ pinout خطر جداً.\n"
    "  6. Hallucination check: هل في حاجة 'مخترعة' (موديل غير موجود، كود "
    "     ECU وهمي، اسم قطعة مش متعارف عليه)؟\n\n"

    "📐 الإخراج JSON صارم — مفيش نص قبل أو بعد:\n"
    "{\n"
    '  "confidence": <0-100 رقم صحيح يعكس مدى ثقتك إن الرد صح>,\n'
    '  "verdict": "pass" | "revise" | "reject",\n'
    '  "doubts": [\n'
    '    "نقطة شك محددة بالظبط — مش عمومية",\n'
    '    "..."\n'
    "  ],\n"
    '  "suggested_revision": "<لو verdict=revise، اكتب الرد المحسّن كامل عربي. غير كده null>"\n'
    "}\n\n"

    "Verdict rules:\n"
    "  • confidence ≥ 85 + مفيش شكوك خطيرة → 'pass'\n"
    "  • فيه شكوك يمكن إصلاحها → 'revise' مع suggested_revision كامل\n"
    "  • فيه أخطاء جوهرية يصعب إصلاحها أو الرد بعيد عن السؤال → 'reject'\n\n"

    "🚫 ممنوع: تطلع 'حلو' أو 'كويس' بدون تحقق. لو الرد عام ومش specific للموديل، "
    "ده شك يستحق خفض الـ confidence."
)


def verify_answer(
    *,
    question: str,
    answer: str,
    mode: str,
    vehicle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the Verifier LLM pass. Always returns a dict (with fallback on error)."""
    vehicle = vehicle or {}
    vehicle_line = _vehicle_line(vehicle)

    user_block = (
        f"=== سياق السؤال ===\n"
        f"الوضع: {mode}\n"
        f"العربية: {vehicle_line or 'مش محددة'}\n\n"
        f"=== سؤال الفني ===\n{question}\n\n"
        f"=== رد المساعد المطلوب فحصه ===\n{answer}\n\n"
        f"راجع الرد ده وأطلع JSON بالـ schema المذكور."
    )

    messages = [
        {'role': 'system', 'content': _VERIFIER_SYSTEM},
        {'role': 'user', 'content': user_block},
    ]

    try:
        raw = call_llm_layer(messages, json_mode=True, max_retries=2)
    except Exception as e:
        logger.exception('[VERIFIER] LLM crashed')
        return _fallback(reason=f'verifier_error:{e}')

    return _parse(raw)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _vehicle_line(v: dict) -> str:
    parts = []
    for key in ('brand', 'model_name', 'year', 'engine_code'):
        val = v.get(key)
        if val:
            parts.append(str(val))
    return ' '.join(parts)


_JSON_BLOCK_RE = re.compile(r'\{[\s\S]*\}')


def _parse(raw: Any) -> dict[str, Any]:
    if not raw:
        return _fallback(reason='empty_verifier_output')

    if isinstance(raw, dict):
        data = raw
    else:
        text = str(raw).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            m = _JSON_BLOCK_RE.search(text)
            if not m:
                return _fallback(reason='non_json_verifier_output')
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return _fallback(reason='invalid_json_verifier_output')

    try:
        confidence = int(data.get('confidence', 0))
    except (TypeError, ValueError):
        confidence = 0
    confidence = max(0, min(100, confidence))

    verdict = str(data.get('verdict', '')).lower().strip()
    if verdict not in {'pass', 'revise', 'reject'}:
        # Infer from confidence
        verdict = 'pass' if confidence >= 85 else 'revise' if confidence >= 60 else 'reject'

    doubts = data.get('doubts') or []
    if not isinstance(doubts, list):
        doubts = [str(doubts)]
    doubts = [str(d).strip() for d in doubts if str(d).strip()][:10]

    revision = data.get('suggested_revision')
    if revision is not None:
        revision = str(revision).strip() or None

    return {
        'confidence': confidence,
        'verdict': verdict,
        'doubts': doubts,
        'suggested_revision': revision,
    }


def _fallback(reason: str) -> dict[str, Any]:
    """Verifier crashed — be conservative: medium confidence, flag for caution."""
    logger.warning('[VERIFIER] fallback path: %s', reason)
    return {
        'confidence': 50,
        'verdict': 'revise',
        'doubts': [f'فشل التحقق التلقائي ({reason}) — اعتبر الرد محتمل'],
        'suggested_revision': None,
    }


# ---------------------------------------------------------------------------
# Thresholds — public constants
# ---------------------------------------------------------------------------
AUTO_PROMOTE_THRESHOLD = 85
RETRY_FLOOR = 60


def tier_from_confidence(score: int) -> str:
    """🟢 / 🟡 / 🔴 — تستخدم في الـ UI."""
    if score >= AUTO_PROMOTE_THRESHOLD:
        return 'high'
    if score >= RETRY_FLOOR:
        return 'medium'
    return 'low'
