"""
💬 Conversational Design Builder — Intent Classifier (Phase N.2)
=====================================================================
يستقبل رسالة المستخدم في محادثة تصميم → يحدد نيتها (chat | generate |
refine) + يستخرج التغييرات المطلوبة. نتيجة الـ classifier بتـ route
الـ orchestrator (N.3) لإحدى مسارات:

  • chat      → ردّ نصي، بدون توليد
  • generate  → compose_mega_prompt → smart router → FLUX/Ideogram
  • refine    → FLUX-Kontext edit للصورة الحالية

Confidence < CONFIDENCE_THRESHOLD → fallback for 'chat' (never burn FLUX
على ambiguity). 'refine' بدون current_design يـ downgrade لـ 'generate'.

Cost: ~$0.0001 per classification call (Llama-3-8B serverless).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .design_engine import _call_together_llm

logger = logging.getLogger('mouss_tec_core')


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Configuration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONFIDENCE_THRESHOLD = 0.70
"""Below this → return 'chat' regardless of classifier output. Calibrated
to avoid mis-routing FLUX generations on ambiguous Arabic phrasing."""

VALID_INTENTS = ('chat', 'generate', 'refine')

# Per-turn LLM cost estimate (Llama-3-8B serverless on Together).
# ~400 input tokens + ~100 output tokens @ $0.18/1M = $0.00009/call.
# Stored on DesignConversationTurn.token_cost_credits for analytics.
INTENT_CLASSIFIER_COST_USD = 0.0001


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# System prompt — Arabic-first, explicit intent definitions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_INTENT_SYSTEM = """\
أنت مصنّف نوايا (intent classifier) لـ chatbot تصميم AI. مهمتك: تقرأ
رسالة العميل وتقرر إيه نية الرسالة من بين 3 خيارات بس، وترجع JSON فقط.

النوايا الثلاثة:

1. "generate" — العميل عايز يولّد تصميم جديد من الصفر.
   أمثلة:
     • "اعمل لي تيشرت رياضي"
     • "ابدأ بتصميم بوستر للمطعم"
     • "ولّد لوجو لشركة تكنولوجيا"
     • "Generate a flyer for the restaurant"
     • "اعمل صورة لمنتج جديد"
   إشارات: أفعال البداية (اعمل/ابدأ/ولّد/صمم/generate/create/make).

2. "refine" — العميل عايز يعدّل التصميم الحالي (موجود بالفعل).
   أمثلة:
     • "غيّر اللون للأزرق"
     • "خليه أصغر شوية"
     • "اشيل النص"
     • "حرّك اللوجو لليمين"
     • "change the color to navy"
     • "make it more minimal"
   إشارات: أفعال التعديل على شيء موجود (غيّر/خليه/اشيل/حرّك/change/move).

3. "chat" — أي حاجة تانية: سؤال، تعليق، استشارة، طلب اقتراحات،
   كلام عام مش طلب توليد ولا تعديل.
   أمثلة:
     • "إيه أحسن لون لمنتج فاخر؟"
     • "اعرض لي أمثلة"
     • "what fonts work best for restaurants?"
     • "أنا مش متأكد من اللون"
     • "كم تكلفة التصميم؟"

قواعد مهمة:
─────────────
• لو الرسالة فيها لبس أو غموض → ارجع "chat" وحط confidence < 0.7.
• "refine" بس لو واضح إن فيه تصميم سابق المفروض يتعدّل.
• استخرج أي تفاصيل واضحة (لون/مقاس/موضع/نص) في extracted_changes.
• confidence لازم يعكس فعلاً مدى الوضوح (مش دايماً 1.0).

شكل الـ JSON اللي ترجعه (واحد فقط، بدون شرح):
{
  "intent": "chat | generate | refine",
  "confidence": 0.0 إلى 1.0,
  "extracted_changes": {
    "color": "navy" أو null,
    "position_change": "top-left" أو null,
    "remove_elements": ["text"] أو [],
    "add_elements": [] أو [],
    "style_change": "minimal" أو null,
    "size_change": "larger" أو null,
    "other": "أي تفصيل مفيد" أو null
  },
  "reasoning_brief": "سطر واحد بالعربي يشرح اختيارك"
}
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def classify_chat_intent(
    message: str,
    *,
    has_current_design: bool = False,
    recent_turns: Optional[list[dict[str, str]]] = None,
) -> dict[str, Any]:
    """يصنّف رسالة المستخدم في محادثة تصميم.

    Args:
        message: نص الرسالة من العميل.
        has_current_design: True لو فيه CustomerDesign live في المحادثة.
                           لو False وكان الـ intent="refine" → downgrade
                           لـ "generate" (مش منطقي تعدّل حاجة مش موجودة).
        recent_turns: آخر 1-3 turns كـ context (optional). شكل:
                     [{"role": "user", "content": "..."}, ...]

    Returns:
        {
          'intent':              'chat' | 'generate' | 'refine',
          'confidence':          float [0.0 - 1.0],
          'extracted_changes':   dict — color/position/elements/style/...
          'reasoning_brief':     str,
          'raw_intent':          str — pre-downgrade intent (للـ analytics),
          'downgraded':          bool — True لو حصل refine→generate downgrade,
          'fallback_reason':     str|None — لو رجع 'chat' بسبب threshold/error,
          'cost_usd':            float,
          'success':             bool,
        }

    On classifier failure (LLM down, parse error, etc.) — returns safe
    default: intent='chat', confidence=0.0, fallback_reason='classifier_error'.
    Never raises.
    """
    safe_msg = (message or '').strip()
    if not safe_msg:
        return _fallback_chat('empty_message', cost=0.0)

    # ── Build user message with optional recent-turn context ────────
    user_msg = f'رسالة العميل: "{safe_msg}"'
    if recent_turns:
        # Cap at last 3 turns to avoid token bloat — the classifier only
        # needs immediate context to disambiguate "and now make it red"
        # vs "and now generate a new one".
        ctx_lines = []
        for t in recent_turns[-3:]:
            role = t.get('role', 'user')
            content = (t.get('content') or '')[:200]
            ctx_lines.append(f'  [{role}] {content}')
        user_msg = (
            'سياق المحادثة (آخر تيرنات):\n'
            + '\n'.join(ctx_lines)
            + f'\n\nالرسالة الحالية: "{safe_msg}"'
        )
    user_msg += (
        f'\n\nحالة الجلسة: '
        f'{"فيه تصميم حالي موجود (يقبل refine)" if has_current_design else "مفيش تصميم لسه (refine مش متاح، استخدم generate)"}'
    )

    # ── Call Together (Llama-3-8B-first fallback chain) ─────────────
    result = _call_together_llm(_INTENT_SYSTEM, user_msg, temperature=0.1)
    if not result.get('success'):
        logger.warning(
            f'[CHAT INTENT] classifier call failed: '
            f'{result.get("error")} — falling back to chat'
        )
        return _fallback_chat('classifier_error', cost=0.0)

    data = result.get('data') or {}
    raw_intent = str(data.get('intent') or '').strip().lower()
    try:
        confidence = float(data.get('confidence', 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))  # clamp

    extracted = data.get('extracted_changes') or {}
    if not isinstance(extracted, dict):
        extracted = {}
    reasoning = str(data.get('reasoning_brief') or '')[:300]

    # ── Validate intent ─────────────────────────────────────────────
    if raw_intent not in VALID_INTENTS:
        logger.warning(
            f'[CHAT INTENT] invalid intent "{raw_intent}" — falling back to chat'
        )
        return _fallback_chat(
            'invalid_intent', cost=INTENT_CLASSIFIER_COST_USD,
            extracted=extracted, raw_intent=raw_intent,
        )

    # ── Confidence threshold ────────────────────────────────────────
    if confidence < CONFIDENCE_THRESHOLD:
        logger.info(
            f'[CHAT INTENT] confidence {confidence:.2f} < {CONFIDENCE_THRESHOLD} '
            f'(intent={raw_intent}) — falling back to chat'
        )
        return {
            'intent': 'chat',
            'confidence': confidence,
            'extracted_changes': extracted,
            'reasoning_brief': reasoning,
            'raw_intent': raw_intent,
            'downgraded': False,
            'fallback_reason': 'low_confidence',
            'cost_usd': INTENT_CLASSIFIER_COST_USD,
            'success': True,
        }

    # ── Refine without a current design → downgrade to generate ─────
    final_intent = raw_intent
    downgraded = False
    if raw_intent == 'refine' and not has_current_design:
        final_intent = 'generate'
        downgraded = True
        logger.info(
            '[CHAT INTENT] downgrading refine→generate (no current_design)'
        )

    return {
        'intent': final_intent,
        'confidence': confidence,
        'extracted_changes': extracted,
        'reasoning_brief': reasoning,
        'raw_intent': raw_intent,
        'downgraded': downgraded,
        'fallback_reason': None,
        'cost_usd': INTENT_CLASSIFIER_COST_USD,
        'success': True,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Context patching — apply extracted_changes to accumulated_context
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def apply_context_patch(
    accumulated_context: dict,
    extracted_changes: dict,
    turn_index: int,
) -> tuple[dict, dict]:
    """يدمج extracted_changes في accumulated_context.selections.

    Returns: (new_context, applied_patch)
        new_context:   الـ context بعد التعديل (immutable — returns a copy)
        applied_patch: الـ patch اللي اتطبق فعلاً (للحفظ في turn.context_patch)

    Rules:
      • أي قيمة جديدة في selections بتـ override القديمة (explicit wins).
      • remove_elements / add_elements بتـ accumulate في lists.
      • history list بيتـ append بدون فقدان السجل.
      • القيم null/empty بتـ skip (مش بتمسح القديم).
    """
    ctx = json.loads(json.dumps(accumulated_context or {}))  # deep copy
    ctx.setdefault('selections', {})
    ctx.setdefault('history', [])

    patch_applied: dict[str, Any] = {}

    # Direct selection overrides
    for src_key, dst_key in (
        ('color', 'color_primary'),
        ('style_change', 'style'),
        ('size_change', 'size'),
        ('position_change', 'position'),
    ):
        v = extracted_changes.get(src_key)
        if v and isinstance(v, str) and v.strip():
            ctx['selections'][dst_key] = v.strip()
            patch_applied[f'selections.{dst_key}'] = v.strip()

    # List accumulators
    for list_key in ('remove_elements', 'add_elements'):
        items = extracted_changes.get(list_key) or []
        if isinstance(items, list) and items:
            current = ctx['selections'].setdefault(list_key, [])
            new_items = [i for i in items if i and i not in current]
            if new_items:
                current.extend(new_items)
                patch_applied[f'selections.{list_key}'] = new_items

    # "other" free-text — useful as a style note
    other = extracted_changes.get('other')
    if other and isinstance(other, str) and other.strip():
        notes = ctx['selections'].setdefault('extra_notes', [])
        if other.strip() not in notes:
            notes.append(other.strip())
            patch_applied['selections.extra_notes'] = [other.strip()]

    # Append to history regardless of whether anything was extracted —
    # gives us a complete replay log for undo / debugging.
    ctx['history'].append({
        'turn': turn_index,
        'patch': patch_applied,
    })

    return ctx, patch_applied


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Internal helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Chat reply — for the 'chat' intent path (no image, just LLM response)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_CHAT_REPLY_SYSTEM = """\
أنت مصمم AI صديقي خبير في تصميم العلامات التجارية، الطباعة، والمنتجات.
بترد على عملاء عرب وأحياناً انجليزي (code-switching) في محادثة لبناء
تصميم. الردود لازم تكون:

  • قصيرة (1-3 جمل في العادة، 5 كحد أقصى)
  • مفيدة وعملية، مش filler
  • لو العميل سأل سؤال → اجاوب باختصار وحدد خيارين/تلاتة
  • لو العميل بيستكشف → اقترح خطوة عملية (لون/نمط/قطاع)
  • مش بتولّد تصميم لوحدك — لو ده اللي العميل عاوزه قول له "اضغط Generate"
  • مرّر العميل لصياغة الـ brief بدل ما يتوهان

شكل الـ JSON اللي ترجعه:
{
  "reply": "ردّك المختصر بالعربي",
  "suggested_next": "اقتراح خطوة جاية (optional)" أو null
}
"""

# Per-chat-reply cost estimate (same Llama-3-8B serverless tier).
CHAT_REPLY_COST_USD = 0.0002


def generate_chat_reply(
    user_message: str,
    *,
    accumulated_context: Optional[dict] = None,
    recent_turns: Optional[list[dict[str, str]]] = None,
) -> dict[str, Any]:
    """يولّد ردّ نصي للـ chat intent — مفيش توليد صور.

    Returns:
      {'success': bool, 'reply': str, 'suggested_next': str|None,
       'cost_usd': float}
    """
    safe_msg = (user_message or '').strip()
    if not safe_msg:
        return {
            'success': True, 'reply': 'اكتب رسالتك أو وصف فكرتك.',
            'suggested_next': None, 'cost_usd': 0.0,
        }

    parts = []
    if accumulated_context:
        brief_ctx = {
            'raw_idea': accumulated_context.get('raw_idea', ''),
            'selections': accumulated_context.get('selections', {}),
        }
        parts.append(f'سياق التصميم اللي بنبنيه:\n{json.dumps(brief_ctx, ensure_ascii=False)[:500]}')
    if recent_turns:
        ctx_lines = []
        for t in recent_turns[-3:]:
            role = t.get('role', 'user')
            content = (t.get('content') or '')[:200]
            ctx_lines.append(f'  [{role}] {content}')
        parts.append('آخر تيرنات المحادثة:\n' + '\n'.join(ctx_lines))
    parts.append(f'الرسالة الحالية: "{safe_msg}"')
    user_msg = '\n\n'.join(parts)

    result = _call_together_llm(_CHAT_REPLY_SYSTEM, user_msg, temperature=0.6)
    if not result.get('success'):
        return {
            'success': False,
            'reply': 'حصل خطأ مؤقت. حاول تاني بعد ثانية.',
            'suggested_next': None,
            'cost_usd': 0.0,
            'error': result.get('error', 'llm_error'),
        }

    data = result.get('data') or {}
    reply = str(data.get('reply') or '').strip()[:1000]
    suggested = data.get('suggested_next')
    if suggested and isinstance(suggested, str):
        suggested = suggested.strip()[:300] or None
    else:
        suggested = None

    if not reply:
        reply = 'لو وصفت أكتر إيه اللي بتدور عليه، أقدر أساعدك بشكل أدق.'

    return {
        'success': True, 'reply': reply,
        'suggested_next': suggested,
        'cost_usd': CHAT_REPLY_COST_USD,
    }


def _fallback_chat(
    reason: str,
    *,
    cost: float,
    extracted: Optional[dict] = None,
    raw_intent: str = '',
) -> dict[str, Any]:
    """Safe-default response when the classifier fails or input is bad."""
    return {
        'intent': 'chat',
        'confidence': 0.0,
        'extracted_changes': extracted or {},
        'reasoning_brief': '',
        'raw_intent': raw_intent,
        'downgraded': False,
        'fallback_reason': reason,
        'cost_usd': cost,
        'success': reason != 'classifier_error',
    }
