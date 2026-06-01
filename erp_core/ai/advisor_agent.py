"""
🧠🧠 Two-Stage Cognitive Advisor Pipeline
=====================================================================
المرحلة 1 (Refiner): بوت وسيط خفي بيصفي طلب المستخدم بالعامية ويحوله
                     لسؤال تقني نظيف ومرتب فيه السياق + النية.

المرحلة 2 (Reasoning + Tools): بوت Gemini أقوى بيشوف الطلب المصفى،
                               يستدعي الـ Tools المناسبة من advisor_tools،
                               ويصيغ الرد النهائي للمستخدم.

⚠️ كل الـ API calls محمية بـ Try/Except، وأي فشل بيرجع رسالة أنيقة للـ UI
   مش 500.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import requests
from django.conf import settings

from .advisor_tools import GEMINI_FUNCTION_DECLARATIONS, TOOL_REGISTRY

logger = logging.getLogger('mouss_tec_core')

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
_GEMINI_BASE = 'https://generativelanguage.googleapis.com/v1beta/models'
_TIMEOUT = 30
_MAX_RETRIES = 2
# الموديل بيقدر يطلب tool calls متتالية. بنحط cap عشان متبقاش حلقة لا نهائية.
_MAX_TOOL_HOPS = 4


def _api_key() -> str:
    """يطهّر مفتاح Gemini من أي whitespace في الـ .env."""
    return str(getattr(settings, 'GEMINI_API_KEY', '') or '').strip()


def _is_enabled() -> bool:
    return bool(getattr(settings, 'ENABLE_AI_PREDICTIONS', True)) and bool(_api_key())


# =============================================================================
# Stage 1 — Refiner (Hidden Intermediary Bot)
# =============================================================================
_REFINER_SYSTEM = """
أنت "البوت الوسيط الخفي" داخل منصة Mousstec ERP. مهمتك حصراً:
1. تستقبل سؤال المستخدم بالعامية أو بأي صياغة، وترجعه كـ JSON منظم.
2. تستخرج النية الحقيقية، والمعاملات الرقمية، والسياق.
3. ميصبحش جواب على السؤال — بس تصفية ومعالجة.

ارجع JSON فقط بالصيغة دي بالظبط:
{
  "refined_question": "<السؤال بصيغة تقنية نظيفة بالعربي الفصيح>",
  "intent": "<cash_flow | dead_stock | inventory_simulation | report_link | general>",
  "parameters": { "<اسم_البارامتر>": "<القيمة>" },
  "language_hint": "ar"
}

أمثلة:
- "لو لميت اللي على الناس هبقي معايا كاش كام؟" → intent=cash_flow, parameters={}
- "أبيع 30% من الراكد هكسب كام؟" → intent=inventory_simulation, parameters={"percentage": 30}
- "اعرضلي اللي مش بيتباع" → intent=dead_stock, parameters={}
- "ودّيني لصفحة العميل رقم 14" → intent=report_link, parameters={"report_type":"customer_detail","customer_id":14}
""".strip()


def refine_query(user_query: str, sector: str = 'printing') -> dict[str, Any]:
    """Stage 1: بوت وسيط خفي بيصفي السؤال. لو فشل بيرجع fallback آمن."""
    fallback = {
        'refined_question': user_query.strip(),
        'intent': 'general',
        'parameters': {},
        'language_hint': 'ar',
        'refiner_status': 'fallback',
    }

    if not _is_enabled():
        return fallback

    model = getattr(settings, 'GEMINI_REFINER_MODEL', 'gemini-2.0-flash')
    url = f'{_GEMINI_BASE}/{model}:generateContent?key={_api_key()}'

    payload = {
        'systemInstruction': {'parts': [{'text': f'{_REFINER_SYSTEM}\n\nالقطاع الحالي: {sector}'}]},
        'contents': [{'role': 'user', 'parts': [{'text': user_query}]}],
        'generationConfig': {
            'temperature': 0.1,
            'responseMimeType': 'application/json',
        },
    }

    try:
        resp = requests.post(url, json=payload, timeout=_TIMEOUT)
        if resp.status_code != 200:
            logger.warning(f'[ADVISOR REFINER] HTTP {resp.status_code}: {resp.text[:200]}')
            return fallback

        data = resp.json()
        raw = data['candidates'][0]['content']['parts'][0]['text']
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())
        parsed = json.loads(raw)
        parsed['refiner_status'] = 'ok'
        return parsed
    except Exception as e:
        logger.warning(f'[ADVISOR REFINER] failed: {e}')
        return fallback


# =============================================================================
# Stage 2 — Reasoning + Function Calling
# =============================================================================
def _reasoning_system_prompt(sector: str) -> str:
    sector_label = 'التصاميم والمطابع' if sector == 'printing' else 'السيارات وقطع الغيار'
    return f"""
أنت "المستشار الذكي" داخل منصة Mousstec ERP — قطاع: {sector_label}.

دورك:
• ترد على أسئلة صاحب البزنس بالعامية المصرية الواضحة (مع أرقام دقيقة من قاعدة بياناته).
• تستدعي الـ Tools المتاحة لما تحتاج بيانات حقيقية. متخمنش أرقام أبداً.
• لو السؤال محتاج لينك صفحة (مثلاً عميل أو تقرير) استدعي generate_report_link واعرض الـ HTML اللي بيرجعه كما هو في ردك (مفيش escape).
• خلي ردك مختصر ومرتب: عنوان قصير، أرقام highlight، توصية عملية.

قواعد صارمة:
1. متخترعش أرقام. لو الـ tool فشل قول للمستخدم "البيانات مش متاحة دلوقتي".
2. كل ردودك بالعربي الـ Egyptian Arabic، نبرة محترفة لكن ودودة.
3. لو المستخدم سأل سؤال مش متعلق بالشغل، اعتذر برقي وارجعه لمواضيع الـ ERP.
""".strip()


def run_advisor_pipeline(
    user_query: str,
    sector: str,
    history: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Pipeline كاملة: Refine → Reasoning → Tool Loop → Final Answer.

    Args:
        user_query: السؤال زي ما المستخدم كتبه.
        sector: 'printing' or 'automotive'.
        history: list من dicts {role, text} للسياق التاريخي للمحادثة.

    Returns:
        dict: {success, answer, refined, tool_calls, error?}
    """
    if not _is_enabled():
        return {
            'success': False,
            'answer': (
                '🌙 المستشار الذكي لسه مش مفعّل على السيرفر — '
                'تواصل مع الإدارة لإضافة مفتاح Gemini.'
            ),
            'error': 'ai_disabled',
        }

    # --- Stage 1 ---
    refined = refine_query(user_query, sector=sector)

    # --- Stage 2: Reasoning loop with function calling ---
    model = getattr(settings, 'GEMINI_REASONING_MODEL', 'gemini-2.5-flash')
    url = f'{_GEMINI_BASE}/{model}:generateContent?key={_api_key()}'

    # Build conversation contents
    contents: list[dict] = []
    for msg in (history or [])[-10:]:  # آخر 10 رسائل بس عشان نوفر tokens
        role = 'user' if msg.get('role') == 'user' else 'model'
        text = str(msg.get('text', '')).strip()
        if text:
            contents.append({'role': role, 'parts': [{'text': text}]})

    # دفع السؤال المصفى للموديل + النص الأصلي عشان ميخسرش الـ context
    contents.append({
        'role': 'user',
        'parts': [{
            'text': (
                f'سؤال المستخدم الأصلي: {user_query}\n\n'
                f'النية المستخرجة: {refined.get("intent")}\n'
                f'السؤال بعد التصفية: {refined.get("refined_question")}'
            ),
        }],
    })

    base_payload = {
        'systemInstruction': {'parts': [{'text': _reasoning_system_prompt(sector)}]},
        'tools': [{'functionDeclarations': GEMINI_FUNCTION_DECLARATIONS}],
        'generationConfig': {'temperature': 0.3},
    }

    tool_calls_log: list[dict] = []

    for hop in range(_MAX_TOOL_HOPS):
        payload = {**base_payload, 'contents': contents}
        try:
            resp = _post_with_retry(url, payload)
        except Exception as e:
            logger.exception('[ADVISOR REASONING] network failure')
            return {
                'success': False,
                'answer': '⚠️ مش قادر أوصل لخدمة الذكاء دلوقتي — حاول تاني بعد ثواني.',
                'error': str(e),
                'refined': refined,
                'tool_calls': tool_calls_log,
            }

        if resp.status_code != 200:
            logger.error(f'[ADVISOR REASONING] HTTP {resp.status_code}: {resp.text[:300]}')
            return {
                'success': False,
                'answer': (
                    '⚠️ المساعد الذكي مش متاح دلوقتي. لو الكلام بيتكرر، '
                    'كلّم الدعم الفني.'
                ),
                'error': f'gemini_http_{resp.status_code}',
                'refined': refined,
                'tool_calls': tool_calls_log,
            }

        try:
            data = resp.json()
            candidate = data['candidates'][0]
            parts = candidate['content'].get('parts', [])
        except (KeyError, IndexError) as e:
            logger.error(f'[ADVISOR REASONING] malformed response: {e} — {data!r}')
            return {
                'success': False,
                'answer': '⚠️ وصلني رد غير مفهوم من السيرفر — حاول تاني.',
                'error': 'malformed_response',
                'refined': refined,
                'tool_calls': tool_calls_log,
            }

        # هل في function calls؟
        function_calls = [p['functionCall'] for p in parts if 'functionCall' in p]

        if not function_calls:
            # خلص — نص نهائي
            final_text = '\n'.join(p.get('text', '') for p in parts if 'text' in p).strip()
            return {
                'success': True,
                'answer': final_text or 'تمام، خلصت — بس مفيش رد واضح.',
                'refined': refined,
                'tool_calls': tool_calls_log,
            }

        # Append model's request (tool call) to history
        contents.append({'role': 'model', 'parts': parts})

        # Execute each tool call and append the response
        tool_response_parts = []
        for fc in function_calls:
            fname = fc.get('name')
            fargs = fc.get('args', {}) or {}
            tool_fn = TOOL_REGISTRY.get(fname)

            if not tool_fn:
                result = {'success': False, 'error': f'tool {fname} غير موجود'}
            else:
                try:
                    result = tool_fn(**fargs)
                except TypeError as e:
                    result = {'success': False, 'error': f'arguments غلط: {e}'}
                except Exception as e:
                    logger.exception(f'[ADVISOR TOOL] {fname} raised')
                    result = {'success': False, 'error': str(e)}

            tool_calls_log.append({'tool': fname, 'args': fargs, 'result_summary': _summarize(result)})

            tool_response_parts.append({
                'functionResponse': {
                    'name': fname,
                    'response': {'content': result},
                },
            })

        contents.append({'role': 'user', 'parts': tool_response_parts})

    # خلصت hops من غير ما يرد بنص نهائي
    return {
        'success': False,
        'answer': 'المساعد فضل يطلب بيانات بدون ما يجاوب — جرب صياغة أبسط للسؤال.',
        'error': 'max_hops_exceeded',
        'refined': refined,
        'tool_calls': tool_calls_log,
    }


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _post_with_retry(url: str, payload: dict) -> requests.Response:
    """POST مع retry على 429 و connection errors."""
    last_exc = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=_TIMEOUT)
            if resp.status_code == 429 and attempt < _MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            return resp
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            if attempt < _MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            raise
    raise last_exc if last_exc else RuntimeError('unknown retry failure')


def _summarize(result: Any) -> str:
    """ملخص قصير للـ tool result عشان الـ logging مايستهلكش حجم."""
    if not isinstance(result, dict):
        return str(result)[:120]
    if not result.get('success'):
        return f'error: {result.get("error", "?")[:80]}'
    return result.get('notes', '')[:160] or 'ok'
