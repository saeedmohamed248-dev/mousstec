"""
🔧 Repair Atlas — Coach Service

نقطة الدخول الوحيدة للـ AI:
    • coach_reply(question, mode, vehicle, history, image_b64=None) → dict
        - يبحث الأول في VerifiedKnowledge (lookup سريع)
        - لو ملقاش → ينده الـ LLM (Together AI عبر inventory.ai_services)
        - لو فيه صورة → Vision mode

الخروج: dict { answer, source, used_verified_kb_id, mode, suggested_part }
"""
from __future__ import annotations

import logging
import re
from typing import Any

from inventory.ai_services import call_llm_layer
from .verifier import (
    verify_answer, tier_from_confidence,
    AUTO_PROMOTE_THRESHOLD, RETRY_FLOOR,
)
from .tie_breaker import tie_break
from .sanity import sweep as sanity_sweep

logger = logging.getLogger('mouss_tec_core')

_MAX_HISTORY_TURNS = 8
_MAX_REVISIONS = 1  # طلب إعادة واحد فقط (يفضل، تكلفة معقولة)


# ---------------------------------------------------------------------------
# System Prompt — متخصص في التفكيك/التركيب/الضفائر
# ---------------------------------------------------------------------------
_SYSTEM_BASE = (
    "أنت أسطى Mouss Tec — أسطى إصلاح في ورشة عربيات بخبرة 25 سنة، "
    "متخصص في:\n"
    "  • تفكيك وتركيب القطع لكل العربيات (محرك، تعليق، كهرباء، تكييف).\n"
    "  • تحديد أماكن القطع (الفني مش عارف القطعة دي فين أصلاً).\n"
    "  • الضفائر والأسلاك: مسار السلك من أوله لآخره، ألوان، أرقام كونيكتورات، "
    "    أرقام بنون، الفولت المتوقع، وأماكن مرور السلك جوه العربية.\n\n"

    "🗣️ طريقة كلامك (مهم جداً):\n"
    "  • اتكلم **عامية مصرية ورشة** زي ما الأسطى بيكلم الصبي — بسيطة وعملية ودافية.\n"
    "  • ممنوع اللغة الرسمية أو الفصحى. قول 'هنفك' مش 'سنقوم بفك'، 'شوف' مش 'انظر'.\n"
    "  • أي مصطلح إنجليزي لازم اللي بعده شرح عربي بين قوسين، مثلاً: "
    "'الكونيكتور (الفيشة)' ، 'الـ pin (السن) رقم 1' ، 'voltage 12 فولت'. "
    "ماتسيبش الفني قدام كلمة إنجليزي ميفهمهاش.\n"
    "  • خليك مختصر — جمل قصيرة. بطّل خطب طويلة.\n\n"

    "💬 إنت بتعمل نقاش مش محاضرة:\n"
    "  • ماترميش كل الكلام مرة واحدة. اشرح **خطوة-خطوة**.\n"
    "  • بعد كل خطوة أو خطوتين، قف واسأل الفني: 'عملت كده؟ صوّرلي' أو 'وصلت لحد فين؟' "
    "قبل ما تكمّل.\n"
    "  • لو الموقف محتاج تفاصيل كتير (زي ضفيرة كاملة)، ابدأ بنظرة سريعة وبعدين قول "
    "'تحب أفصّلك أنهي جزء؟' بدل ما تصبّ الكلام كله.\n\n"

    "🎯 منهجك الإلزامي:\n"
    "  1. لو الفني مش حدد الموديل أو السنة بدقة — اسأله مرة واحدة فقط (سؤال واضح، "
    "     مش 5 أسئلة)، ولو رد بـ 'مش عارف' كمل بأكتر سيناريو شيوعاً مع التحذير.\n"
    "  2. ابدأ بنظرة عامة: 'القطعة دي مكانها كذا، شكلها كذا، اللي حواليها كذا'.\n"
    "  3. الأدوات المطلوبة قبل البدء (سبانر مقاس كذا، مفتاح عزم، multimeter...).\n"
    "  4. تحذيرات السلامة: قبل البدء (افصل البطارية / فرّغ الضغط / استنى المحرك يبرد).\n"
    "  5. الخطوات مرقمة من 1 إلى N، كل خطوة:\n"
    "     - الفعل (بفعل أمر واضح)\n"
    "     - عزم التربيط لو في فك مسامير (Nm)\n"
    "     - تحذير لو في خطوة حساسة\n"
    "     - 'صوّر لي دلوقتي' عند الخطوات اللي محتاجة تأكيد بصري\n"
    "  6. عند التركيب العكسي: خصوصاً ترتيب التربيط (cross-pattern للسلندر هد مثلاً) "
    "     وعزم التربيط الصحيح + الخطوات اللي ممنوع تتعكس.\n\n"

    "📐 للضفائر تحديداً (mode=wiring):\n"
    "  • ارسم الضفيرة كنص شجري (ASCII tree) من نقطة البداية لكل نقطة نهاية.\n"
    "  • كل segment: لون السلك، المقاس (mm²)، رقم الكونيكتور (C101...), pin number, "
    "    signal name، voltage spec، المسار الفيزيكي (تحت الطبلون / خلف الفندر / إلخ).\n"
    "  • لو الفني محتاج يفحص قطع — اذكر pinout الكونيكتور بالكامل.\n\n"

    "👁️ Vision (لما الفني يرفع صورة):\n"
    "  • قارن اللي شايفه في الصورة بالخطوة المتوقعة.\n"
    "  • verdict واحد بوضوح في أول سطر: ✅ صح كمل / ⚠️ انتبه / ❌ غلط ارجع / ❓ صور تاني.\n"
    "  • وضح ليه — أنهي مسمار/سلك/جزء غلط بالظبط.\n\n"

    "📚 الاستشهاد بالمصدر (إلزامي): كل ادعاء specific (رقم، كود، اسم قطعة، "
    "pinout) لازم يبقى مصحوب بـ مصدر معرفي بين قوسين، مثلاً:\n"
    "  • '8 Nm (Workshop Manual — Hyundai Elantra 2018)'\n"
    "  • 'كونيكتور C101 (تقدير عام — تأكد من ETM)'\n"
    "  • 'سلك أحمر (خبرة عامة على العائلة دي)'\n"
    "الادعاءات بدون مصدر = ضعيفة، الـ Verifier هيخفّض الثقة عليها.\n\n"

    "🚫 ممنوع:\n"
    "  • تخمن موديل عربية بدون ما الفني يأكد.\n"
    "  • تكتب 'استشير الوكيل' بدون ما تحاول تساعد الأول.\n"
    "  • تطلب أكتر من حاجة واحدة في الرسالة (سؤال واحد أو خطوة واحدة).\n"
    "  • تكتب رقم Nm/V/Ω بدون ما يكون في نطاق فيزيكي معقول.\n\n"

    "✅ مسموح: تقول 'أنا مش متأكد 100% من رقم الكونيكتور في الموديل ده تحديداً، "
    "بس في غالبية الموديلات ده C123. لو في wiring diagram للموديل أكّد منه.' — "
    "الصدق الفني أهم من التخمين."
)


_MODE_HINTS = {
    'disassembly': "الفني عاوز يفك قطعة. ركّز على ترتيب الفك + الأدوات + التحذيرات.",
    'install':     "الفني عاوز يركّب قطعة. ركّز على ترتيب التركيب + عزم التربيط + "
                   "checklist قبل التشغيل.",
    'wiring':      "الفني سأل عن ضفيرة. ارسم المسار شجرياً + كل التفاصيل المذكورة "
                   "في القسم المخصص للضفائر.",
    'locate':      "الفني مش عارف القطعة فين أصلاً. ابدأ بـ 'افتح الكبوت / اقعد ع "
                   "الكرسي / إلخ' وحدد مكان مرئي قبل أي حاجة تانية.",
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def coach_reply(
    question: str,
    mode: str,
    vehicle: dict[str, Any] | None = None,
    history: list[dict] | None = None,
    image_b64: str | None = None,
) -> dict[str, Any]:
    """
    Main entry. Returns:
        { success, answer, source, suggested_part, mode, error? }
    """
    question = (question or '').strip()
    if not question and not image_b64:
        return {'success': False, 'answer': 'اكتب سؤال أو ارفع صورة.',
                'source': 'none'}

    mode = mode if mode in _MODE_HINTS else 'disassembly'
    vehicle = vehicle or {}
    history = history or []

    # 1. Verified KB lookup — قبل أي LLM call
    kb_hit = _lookup_verified_kb(question, mode, vehicle)
    if kb_hit and not image_b64:
        return {
            'success': True,
            'answer': kb_hit.answer_markdown,
            'source': 'verified',
            'used_verified_kb_id': kb_hit.id,
            'mode': mode,
            'suggested_part': kb_hit.part_or_system_norm,
        }

    # 2. Generator LLM call
    messages = _build_messages(question, mode, vehicle, history, image_b64)
    try:
        raw = call_llm_layer(messages, json_mode=False, max_retries=2)
    except Exception as e:
        logger.exception('[REPAIR_COACH] Generator LLM crashed')
        return {'success': False, 'answer': '⚠️ حصل عطل في الـ AI، جرب تاني.',
                'source': 'error', 'error': str(e)}

    answer = (raw or '').strip()
    if not answer:
        return {'success': False, 'answer': '⚠️ الـ AI رد بإجابة فاضية، جرب صياغة تانية.',
                'source': 'error'}

    # 3. Sanity Sweep (deterministic, no LLM) — أرقام مستحيلة فيزيكياً
    sanity = sanity_sweep(answer)

    # 4. Verifier pass (V2)
    verify_result = verify_answer(
        question=question, answer=answer, mode=mode, vehicle=vehicle,
    )
    revisions = 0

    # 5. لو V2 طلب revise أو الـ sanity فشل → نعيد مرة واحدة
    if (verify_result['verdict'] == 'revise' or not sanity.ok) and revisions < _MAX_REVISIONS:
        combined_doubts = list(verify_result['doubts']) + sanity.failures
        if verify_result.get('suggested_revision') and sanity.ok:
            answer = verify_result['suggested_revision']
        else:
            answer = _request_revision(messages, answer, combined_doubts)
        revisions += 1
        sanity = sanity_sweep(answer)
        verify_result = verify_answer(
            question=question, answer=answer, mode=mode, vehicle=vehicle,
        )

    confidence = verify_result['confidence']
    doubts = list(verify_result['doubts'])

    # 6. لو لسه الـ sanity فشل بعد إعادة → نخفّض الثقة بقوة
    if not sanity.ok:
        doubts = sanity.failures + doubts
        confidence = min(confidence, 45)  # below RETRY_FLOOR ⇒ tier=low

    tier = tier_from_confidence(confidence)

    # 7. Tie-Breaker (V3) — للحالات المتوسطة فقط، عشان نوفّر LLM cost
    tie_decision = None
    if tier == 'medium':
        tb = tie_break(
            question=question, answer=answer, mode=mode, vehicle=vehicle,
            v2_doubts=doubts, v2_confidence=confidence,
        )
        tie_decision = tb['decision']
        if tb['decision'] == 'approve':
            confidence = max(confidence, AUTO_PROMOTE_THRESHOLD + 5)
        elif tb['decision'] == 'overrule':
            confidence = min(confidence, RETRY_FLOOR - 5)
            if tb.get('critical_issue'):
                doubts.insert(0, f"عيب جوهري: {tb['critical_issue']}")
        tier = tier_from_confidence(confidence)

    auto_promoted = tier == 'high'

    return {
        'success': True,
        'answer': answer,
        'source': 'ai_auto_verified' if auto_promoted else 'llm',
        'mode': mode,
        'suggested_part': _extract_part_hint(question),
        'confidence': confidence,
        'tier': tier,
        'doubts': doubts,
        'auto_promoted': auto_promoted,
        'revisions': revisions,
        'sanity_ok': sanity.ok,
        'tie_decision': tie_decision,
    }


def _request_revision(orig_messages: list[dict], orig_answer: str,
                       doubts: list[str]) -> str:
    """ينده الـ Generator تاني مع الشكوك."""
    doubts_block = '\n'.join(f'  - {d}' for d in doubts) or '  - (لا توجد شكوك محددة)'
    follow_up = (
        f"ردك السابق:\n{orig_answer}\n\n"
        f"الشكوك اللي طلعت من المراجع:\n{doubts_block}\n\n"
        f"أعد كتابة الرد كامل بعد ما تعالج الشكوك دي. لو شك مش صح، علّق "
        f"عليه بأدب وبرر."
    )
    msgs = list(orig_messages) + [
        {'role': 'assistant', 'content': orig_answer},
        {'role': 'user', 'content': follow_up},
    ]
    try:
        revised = call_llm_layer(msgs, json_mode=False, max_retries=1)
    except Exception:
        logger.exception('[REPAIR_COACH] revision LLM crashed')
        return orig_answer
    return (revised or '').strip() or orig_answer


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _build_messages(question, mode, vehicle, history, image_b64):
    system = _SYSTEM_BASE + "\n\n=== سياق السؤال الحالي ===\n" + _MODE_HINTS[mode]
    if vehicle:
        v_lines = []
        for label, key in (('الماركة', 'brand'), ('الموديل', 'model_name'),
                            ('السنة', 'year'), ('المحرك', 'engine_code'),
                            ('VIN', 'vin')):
            val = vehicle.get(key)
            if val:
                v_lines.append(f'  • {label}: {val}')
        if v_lines:
            system += "\n\n🚗 العربية:\n" + "\n".join(v_lines)

    msgs = [{'role': 'system', 'content': system}]
    for turn in history[-_MAX_HISTORY_TURNS:]:
        role = 'user' if turn.get('role') == 'user' else 'assistant'
        msgs.append({'role': role, 'content': turn.get('text', '')})

    if image_b64:
        msgs.append({
            'role': 'user',
            'content': [
                {'type': 'text',
                 'text': question or 'دي الصورة من مكان الشغل — ارفعلي تقييمك.'},
                {'type': 'image_url',
                 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}},
            ],
        })
    else:
        msgs.append({'role': 'user', 'content': question})
    return msgs


def _lookup_verified_kb(question: str, mode: str, vehicle: dict):
    """Cheap exact-ish lookup. لو في معرفة معتمدة قريبة من السؤال، نرجّعها.

    Tenant-only model — لو الـ schema الحالي مفهوش جدول (مثلاً public أو في
    اختبار)، نرجّع None بهدوء بدل ما نوقع كل التطبيق.
    """
    from django.db import DatabaseError
    from repair_atlas.models import VerifiedKnowledge

    brand = _normalize(vehicle.get('brand', ''))
    model = _normalize(vehicle.get('model_name', ''))
    if not brand:
        return None

    part_hint = _extract_part_hint(question)
    try:
        qs = VerifiedKnowledge.objects.filter(brand_norm=brand, mode=mode)
        if model:
            qs = qs.filter(model_norm__in=[model, ''])
        if part_hint:
            qs = qs.filter(part_or_system_norm__icontains=part_hint)
        hit = qs.order_by('-times_served').first()
    except DatabaseError:
        logger.debug('[REPAIR_COACH] VerifiedKnowledge table missing — skip KB lookup')
        return None

    if hit:
        from django.utils import timezone
        hit.times_served = (hit.times_served or 0) + 1
        hit.last_served_at = timezone.now()
        hit.save(update_fields=['times_served', 'last_served_at'])
    return hit


def _normalize(s: str) -> str:
    return re.sub(r'\s+', ' ', (s or '').strip().lower())


_PART_KEYWORDS = (
    'حساس', 'طلمبة', 'دينمو', 'مارش', 'كمبروسر', 'رديتر', 'سلف',
    'كونيكتور', 'ضفيرة', 'سلك', 'كويل', 'بوجيه', 'بوجية', 'مكينة المساحات',
    'مكينة', 'علبة سرعات', 'كمبيوتر', 'ECU', 'بطارية', 'فلتر',
    'thermostat', 'ترموستات', 'ABS', 'airbag', 'فحمات', 'قمصان',
)


def _extract_part_hint(question: str) -> str:
    q = question.lower()
    for kw in _PART_KEYWORDS:
        if kw.lower() in q:
            return kw
    return ''
