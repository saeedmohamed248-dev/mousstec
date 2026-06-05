"""
🧠🧠 Two-Stage Cognitive Advisor Pipeline (Together AI / Llama-3.3)
=====================================================================
المرحلة 1 (Refiner): بوت وسيط خفي بيصفي طلب المستخدم بالعامية ويحوله
                     لسؤال تقني نظيف ومرتب فيه السياق + النية.

المرحلة 2 (Reasoning + Tools): Llama-3.3-70B عبر Together AI بيشوف الطلب،
                               يستدعي الـ Tools المناسبة من advisor_tools
                               عبر tool-calling spec (مطابق لـ OpenAI's shape
                               اللي Together بيقبله — مش بنكلم OpenAI أصلاً).

⚠️ كل النصوص والـ JSON تتم عبر Together AI حصراً (Llama-3.3) — لا Gemini ولا OpenAI.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests
from django.conf import settings

from .advisor_tools import GEMINI_FUNCTION_DECLARATIONS, TOOL_REGISTRY

logger = logging.getLogger('mouss_tec_core')

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
_TOGETHER_CHAT_URL = 'https://api.together.xyz/v1/chat/completions'
_DEFAULT_LLM_MODEL = 'meta-llama/Llama-3.3-70B-Instruct-Turbo'
_TIMEOUT = 30
_MAX_RETRIES = 2
_MAX_TOOL_HOPS = 4


def _api_key() -> str:
    return str(getattr(settings, 'TOGETHER_API_KEY', '') or '').strip()


def _model() -> str:
    return str(getattr(settings, 'TOGETHER_LLM_MODEL', '') or _DEFAULT_LLM_MODEL).strip()


def _is_enabled() -> bool:
    return bool(getattr(settings, 'ENABLE_AI_PREDICTIONS', True)) and bool(_api_key())


# Translate Gemini-shaped function declarations into the {type:function,function:...}
# tool-spec shape that Together AI's chat-completions endpoint expects.
# (parameters are already JSON-Schema, so it's a thin wrapper. The name historically
# said "openai" because Together's API mirrors the OpenAI tool-call spec — kept as
# `_tool_declarations` post-cleanup to stop misleading readers into thinking we hit OpenAI.)
def _tool_declarations() -> list[dict]:
    return [
        {
            'type': 'function',
            'function': {
                'name': fd['name'],
                'description': fd['description'],
                'parameters': fd.get('parameters', {'type': 'object', 'properties': {}}),
            },
        }
        for fd in GEMINI_FUNCTION_DECLARATIONS
    ]


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
""".strip()


def refine_query(user_query: str, sector: str = 'printing') -> dict[str, Any]:
    """Stage 1: refiner — Together AI + JSON mode."""
    fallback = {
        'refined_question': user_query.strip(),
        'intent': 'general',
        'parameters': {},
        'language_hint': 'ar',
        'refiner_status': 'fallback',
    }

    if not _is_enabled():
        return fallback

    # Lazy import to avoid circular dep at module load
    from inventory.ai_services import call_llm_layer

    messages = [
        {'role': 'system', 'content': f'{_REFINER_SYSTEM}\n\nالقطاع الحالي: {sector}'},
        {'role': 'user', 'content': user_query},
    ]
    raw = call_llm_layer(messages, json_mode=True, max_retries=2)
    if not raw:
        return fallback
    try:
        parsed = json.loads(raw)
        parsed['refiner_status'] = 'ok'
        return parsed
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f'[ADVISOR REFINER] JSON parse failed: {e}')
        return fallback


# =============================================================================
# Stage 2 — Reasoning + Function Calling (Together AI tool-calling)
# =============================================================================
def _reasoning_system_prompt(sector: str) -> str:
    sector_label = 'التصاميم والمطابع' if sector == 'printing' else 'السيارات وقطع الغيار'
    return f"""
أنت "المستشار الذكي" داخل منصة Mousstec ERP — قطاع: {sector_label}.

دورك:
• ترد على أسئلة صاحب البزنس بالعامية المصرية الواضحة (مع أرقام دقيقة من قاعدة بياناته).
• تستدعي الـ Tools المتاحة لما تحتاج بيانات حقيقية. متخمنش أرقام أبداً.
• لو السؤال محتاج لينك صفحة استدعي generate_report_link واعرض الـ HTML كما هو.
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
    Pipeline كامل: Refine → Reasoning (Llama-3.3 tool-loop) → Final Answer.
    """
    if not _is_enabled():
        return {
            'success': False,
            'answer': (
                '🌙 المستشار الذكي لسه مش مفعّل على السيرفر — '
                'تواصل مع الإدارة لإضافة مفتاح Together AI.'
            ),
            'error': 'ai_disabled',
        }

    # --- Stage 1 ---
    refined = refine_query(user_query, sector=sector)

    # --- Stage 2: build tool-calling chat history (OpenAI-compatible shape that Together accepts) ---
    messages: list[dict] = [
        {'role': 'system', 'content': _reasoning_system_prompt(sector)},
    ]
    for msg in (history or [])[-10:]:
        role = 'user' if msg.get('role') == 'user' else 'assistant'
        text = str(msg.get('text', '')).strip()
        if text:
            messages.append({'role': role, 'content': text})

    messages.append({
        'role': 'user',
        'content': (
            f'سؤال المستخدم الأصلي: {user_query}\n\n'
            f'النية المستخرجة: {refined.get("intent")}\n'
            f'السؤال بعد التصفية: {refined.get("refined_question")}'
        ),
    })

    tools = _tool_declarations()
    tool_calls_log: list[dict] = []

    for hop in range(_MAX_TOOL_HOPS):
        try:
            resp = _post_with_retry(messages, tools=tools, temperature=0.3)
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
                'answer': '⚠️ المساعد الذكي مش متاح دلوقتي — كلّم الدعم الفني لو الكلام بيتكرر.',
                'error': f'together_http_{resp.status_code}',
                'refined': refined,
                'tool_calls': tool_calls_log,
            }

        try:
            data = resp.json()
        except ValueError as e:
            logger.error(f'[ADVISOR REASONING] invalid JSON body: {e}')
            return _malformed(refined, tool_calls_log)

        choices = data.get('choices') or []
        if not choices:
            logger.error(f'[ADVISOR REASONING] empty choices: {data!r}')
            return _malformed(refined, tool_calls_log)

        message = choices[0].get('message') or {}
        tool_calls = message.get('tool_calls') or []

        if not tool_calls:
            final_text = (message.get('content') or '').strip()
            return {
                'success': True,
                'answer': final_text or 'تمام، خلصت — بس مفيش رد واضح.',
                'refined': refined,
                'tool_calls': tool_calls_log,
            }

        # Append assistant turn (with tool_calls) — required by the tool-calling spec
        messages.append({
            'role': 'assistant',
            'content': message.get('content') or '',
            'tool_calls': tool_calls,
        })

        for tc in tool_calls:
            fname = (tc.get('function') or {}).get('name')
            raw_args = (tc.get('function') or {}).get('arguments') or '{}'
            try:
                fargs = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError:
                fargs = {}

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

            messages.append({
                'role': 'tool',
                'tool_call_id': tc.get('id', ''),
                'name': fname,
                'content': json.dumps(result, ensure_ascii=False),
            })

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
def _malformed(refined, tool_log):
    return {
        'success': False,
        'answer': '⚠️ وصلني رد غير مفهوم من السيرفر — حاول تاني.',
        'error': 'malformed_response',
        'refined': refined,
        'tool_calls': tool_log,
    }


def _post_with_retry(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    temperature: float = 0.3,
) -> requests.Response:
    """POST to Together chat completions with 429/connection retry."""
    headers = {'Authorization': f'Bearer {_api_key()}', 'Content-Type': 'application/json'}
    payload: dict[str, Any] = {
        'model': _model(),
        'messages': messages,
        'temperature': temperature,
    }
    if tools:
        payload['tools'] = tools
        payload['tool_choice'] = 'auto'

    last_exc = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.post(_TOGETHER_CHAT_URL, headers=headers, json=payload, timeout=_TIMEOUT)
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
    if not isinstance(result, dict):
        return str(result)[:120]
    if not result.get('success'):
        return f'error: {str(result.get("error", "?"))[:80]}'
    return str(result.get('notes', ''))[:160] or 'ok'
