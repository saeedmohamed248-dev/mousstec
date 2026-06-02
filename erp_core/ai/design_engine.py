"""
🧠 Universal AI Design Engine — Data Flywheel
=====================================================================
نظام تصميم ديناميكي بدون أي hardcoding للـ categories. بيستخدم Together LLM
عشان يحلل أي فكرة تصميم في أي مجال (interior design, footwear, apparel,
packaging, industrial, ...) ويرجع dropdowns ديناميكية مخصصة للفكرة دي بالذات.

Pipeline:
  1) analyze_idea(raw)        → Together LLM يرجع dynamic JSON schema
                                 (domain + 3-5 fields with options)
  2) compose_mega_prompt(...)  → Together LLM يدمج الاختيارات في English mega prompt
  3) generate_image(prompt)    → Together Image (FLUX) — reuse existing
  4) log_to_flywheel(...)      → AIPromptLearningLog لبناء fine-tuning dataset

كل النتايج من LLM JSON-only عشان parsing موثوق.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger('mouss_tec_core')

_TOGETHER_CHAT_URL = 'https://api.together.xyz/v1/chat/completions'
_TIMEOUT_LLM = 30

# Defaults — overridable من settings
_DEFAULT_LLM_MODEL = 'meta-llama/Llama-3.3-70B-Instruct-Turbo-Free'


def _llm_model() -> str:
    return str(getattr(settings, 'TOGETHER_LLM_MODEL', '') or _DEFAULT_LLM_MODEL).strip()


def _together_key() -> str:
    return str(getattr(settings, 'TOGETHER_API_KEY', '') or '').strip()


# =============================================================================
# Stage 1 — Universal Dynamic Schema Generator
# =============================================================================
_SCHEMA_SYSTEM = """You are a world-class creative prompt engineer for an image-generation system.

Given a user's short raw idea in any language (Arabic or English), about ANY design domain
(architectural interior design, footwear manufacturing, apparel, packaging, industrial product
design, graphic design, jewelry, automotive, anything), you must:

1. Identify the most specific design domain.
2. Propose 3 to 5 dropdown fields that an expert practitioner in THAT exact domain would ask
   the user to specify before generating a professional reference image.
3. For each field, provide 4 to 7 carefully chosen options written in the same language as
   the raw idea (Arabic if Arabic, English if English).

You must return STRICT JSON with this exact shape:
{
  "domain": "<short label, e.g. 'Interior Design' or 'Footwear Manufacturing'>",
  "domain_ar": "<Arabic translation of the domain>",
  "fields": [
    {
      "key": "<snake_case stable key, e.g. 'architectural_style'>",
      "label": "<human label in the user's language>",
      "options": ["<opt1>", "<opt2>", "<opt3>", "..."]
    },
    ...
  ]
}

Rules:
- NEVER reuse fields across unrelated domains. Slippers must NOT get 'architectural style';
  living rooms must NOT get 'sole type'.
- Field keys are snake_case English; labels follow the user's language.
- Options must be concrete and visually meaningful (a Flux image model will use them).
- Return JSON only, no markdown fences, no commentary."""


_MEGA_SYSTEM = """You are a senior prompt engineer for FLUX.1 image generation.

You will receive:
- The user's raw idea (any language)
- The detected design domain
- A set of selected options (key → value)

Produce ONE production-grade English prompt optimized for FLUX.1, max ~90 words, dense visual
detail. Embed when relevant:
- Specific materials, textures, finishes
- Lighting (studio / natural / dramatic) appropriate to the domain
- Camera angle / view / framing
- Color palette
- Style modifiers ("photorealistic 8k product photo", "architectural visualization", etc.)
- Avoid: text artifacts, watermarks, low quality, distorted geometry

Return STRICT JSON only:
{
  "mega_prompt": "<single English paragraph>",
  "negative_prompt": "<short comma-separated list>",
  "recommended_size": "<e.g. '1024x1024' or '1024x1536' or '1536x1024'>"
}"""


def _call_together_llm(system: str, user: str, *, temperature: float = 0.4) -> dict[str, Any]:
    """يستدعي Together Chat Completions ويرجع JSON parsed (أو error dict)."""
    key = _together_key()
    if not key:
        return {'success': False, 'error': 'together_key_missing'}

    payload = {
        'model': _llm_model(),
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ],
        'temperature': temperature,
        'max_tokens': 1200,
        'response_format': {'type': 'json_object'},
    }
    headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}

    try:
        resp = requests.post(_TOGETHER_CHAT_URL, json=payload, headers=headers, timeout=_TIMEOUT_LLM)
        if resp.status_code != 200:
            logger.warning(f'[DESIGN ENGINE LLM] HTTP {resp.status_code}: {resp.text[:200]}')
            return {'success': False, 'error': f'together_llm_http_{resp.status_code}'}
        try:
            data = resp.json()
        except ValueError as e:
            logger.warning(f'[DESIGN ENGINE LLM] non-JSON body: {e}')
            return {'success': False, 'error': 'together_llm_invalid_body'}
        choices = data.get('choices') or []
        if not choices:
            logger.warning(f'[DESIGN ENGINE LLM] empty choices: {data!r}')
            return {'success': False, 'error': 'together_llm_empty_choices'}
        raw = (choices[0].get('message') or {}).get('content') or ''
        if not raw.strip():
            return {'success': False, 'error': 'together_llm_empty_content'}
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())
        parsed = json.loads(raw)
        return {'success': True, 'data': parsed}
    except requests.Timeout:
        return {'success': False, 'error': 'together_llm_timeout'}
    except json.JSONDecodeError as e:
        logger.warning(f'[DESIGN ENGINE LLM] JSON parse failed: {e}')
        return {'success': False, 'error': 'together_llm_invalid_json'}
    except Exception as e:
        logger.exception('[DESIGN ENGINE LLM] failed')
        return {'success': False, 'error': str(e)}


def analyze_idea(raw_idea: str) -> dict[str, Any]:
    """يحلل فكرة المستخدم ويرجع dynamic schema (domain + dropdowns)."""
    raw = (raw_idea or '').strip()
    if not raw:
        return {'success': False, 'error': 'empty_idea'}

    result = _call_together_llm(
        _SCHEMA_SYSTEM,
        f'Raw design idea: {raw}',
        temperature=0.5,
    )
    if not result.get('success'):
        return result

    schema = result['data']
    # Validate shape
    if not isinstance(schema, dict) or 'fields' not in schema or not isinstance(schema['fields'], list):
        return {'success': False, 'error': 'invalid_schema_shape', 'raw': schema}

    # Clamp 3-5 fields
    schema['fields'] = schema['fields'][:5]
    if len(schema['fields']) < 2:
        return {'success': False, 'error': 'too_few_fields', 'raw': schema}

    # Normalize each field
    cleaned = []
    for f in schema['fields']:
        if not isinstance(f, dict):
            continue
        key = str(f.get('key') or '').strip()
        label = str(f.get('label') or '').strip()
        opts = f.get('options') or []
        if not key or not label or not isinstance(opts, list) or len(opts) < 2:
            continue
        cleaned.append({
            'key': re.sub(r'[^a-z0-9_]', '_', key.lower())[:40] or 'field',
            'label': label[:80],
            'options': [str(o)[:80] for o in opts[:8]],
        })
    if len(cleaned) < 2:
        return {'success': False, 'error': 'no_valid_fields'}

    return {
        'success': True,
        'domain': str(schema.get('domain') or 'General Design')[:80],
        'domain_ar': str(schema.get('domain_ar') or '')[:80],
        'fields': cleaned,
    }


def compose_mega_prompt(
    raw_idea: str,
    domain: str,
    selections: dict[str, str],
) -> dict[str, Any]:
    """يدمج الفكرة + الاختيارات في English mega prompt للـ FLUX."""
    raw = (raw_idea or '').strip()
    if not raw:
        return {'success': False, 'error': 'empty_idea'}

    # Build user message
    selection_lines = '\n'.join(
        f'- {k}: {v}' for k, v in (selections or {}).items() if v
    )
    user_msg = (
        f'Raw idea: {raw}\n'
        f'Detected domain: {domain or "General"}\n'
        f'User selections:\n{selection_lines or "(none)"}'
    )

    result = _call_together_llm(_MEGA_SYSTEM, user_msg, temperature=0.3)
    if not result.get('success'):
        return result

    data = result['data']
    mega = str(data.get('mega_prompt') or '').strip()
    if not mega:
        return {'success': False, 'error': 'empty_mega_prompt'}

    return {
        'success': True,
        'mega_prompt': mega[:2000],
        'negative_prompt': str(data.get('negative_prompt') or 'low quality, blurry, watermark, distorted')[:500],
        'recommended_size': str(data.get('recommended_size') or '1024x1024')[:20],
    }
