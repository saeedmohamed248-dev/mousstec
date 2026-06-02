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
# Daphne default http-timeout = 60s. نسيب margin مناسب للموديل 70B (بيرد JSON طويل)
_TIMEOUT_LLM = 45

# Defaults — overridable من settings
# ملاحظة: النسخة "-Free" مش متاحة كـ serverless لكل الحسابات.
# لو الحساب اشترى credit، استخدم النسخة Paid اللي بتشتغل serverless بدون عقبات.
_DEFAULT_LLM_MODEL = 'meta-llama/Llama-3.3-70B-Instruct-Turbo'

# Fallback chain — لو الموديل الأول فشل بـ 400/403 (مش متاح للحساب)،
# نجرب الموديلات دي بالترتيب. أسماء متحقّق منها من Together model registry.
_FALLBACK_LLM_MODELS = (
    'meta-llama/Llama-3.3-70B-Instruct-Turbo',
    'deepseek-ai/DeepSeek-V3',
    'Qwen/Qwen2.5-7B-Instruct-Turbo',
)


def _llm_model() -> str:
    return str(getattr(settings, 'TOGETHER_LLM_MODEL', '') or _DEFAULT_LLM_MODEL).strip()


def _llm_fallback_chain() -> tuple[str, ...]:
    primary = _llm_model()
    seen = {primary}
    chain = [primary]
    for m in _FALLBACK_LLM_MODELS:
        if m not in seen:
            chain.append(m)
            seen.add(m)
    return tuple(chain)


def _together_key() -> str:
    return str(getattr(settings, 'TOGETHER_API_KEY', '') or '').strip()


# =============================================================================
# Stage 1 — Universal Dynamic Schema Generator
# =============================================================================
_SCHEMA_SYSTEM = """You are a world-class creative prompt engineer for an image-generation system.

Given a user's short raw idea in any language (Arabic or English) about ANY design domain
(interior design, footwear, apparel, packaging, industrial, graphic, jewelry, automotive,
anything), you must:

1. Identify the most specific design domain.
2. Propose 4 to 7 fields an expert practitioner in THAT exact domain would ask before
   generating a professional reference image. Each field has a TYPE chosen from this list,
   picked to match what makes sense for the domain (don't use 'select' for everything):

   • "select"       — dropdown with 4-7 fixed options (e.g. style, mood, sub-category).
   • "multi_select" — multiple choices from 4-7 options (e.g. furniture items, features).
   • "text"         — short free text (max ~60 chars) — for brand names, slogans, model names.
   • "number"       — single numeric value with optional unit (e.g. budget, weight, age).
   • "dimensions"   — 2D (width × height) or 3D (length × width × height) measurements
                     with a unit. Use this for any sized object: rooms, t-shirts, packaging.
   • "color"        — single color picker (HTML color). Use for brand colors, walls, fabric.
   • "range"        — min–max numeric range (e.g. price range, age range).

3. Pick the RIGHT type per field:
   • Room/space → ALWAYS include a "dimensions" field (length × width × height, meters).
   • Logo/branding → "text" for brand name + 2× "color" + "select" for typography style.
   • Apparel/t-shirt → "select" for size (S/M/L/XL/XXL) + "dimensions" for print area
     (cm) + "color" for shirt + "color" for print + "select" for print position.
   • Packaging → "dimensions" (cm) + "text" for product name + "color" + "select" for finish.
   • Interior → "dimensions" + "color" for walls + "multi_select" for furniture items +
     "select" for style + "select" for lighting.
   • Footwear → "select" for size range + "select" for material + "color" + "select" for
     view angle + "select" for sole type.
   • Industrial/automotive → "color" + "dimensions" + "select" for material + "select"
     for view.

4. Field shape (return STRICT JSON, no markdown, no commentary):

{
  "domain": "<short English label>",
  "domain_ar": "<Arabic translation>",
  "fields": [
    {
      "key": "<snake_case_key>",
      "label": "<label in user's language>",
      "type": "<one of: select, multi_select, text, number, dimensions, color, range>",
      "options": ["..."],          // ONLY for select / multi_select (4-7 items)
      "unit": "m" | "cm" | "mm" | "in" | "ج.م" | "kg" | "g" | "%" | "",
      "axes": ["length","width","height"]  // ONLY for dimensions: 2 or 3 axes
      "min": <num>, "max": <num>, "step": <num>,   // ONLY for number / range
      "default": "<sensible default that hints at typical professional choice>",
      "placeholder": "<for text fields>"
    },
    ...
  ]
}

Rules:
- Field keys snake_case English; labels in user's language.
- Provide sensible "default" values (the user can edit; defaults must be production-grade).
- Options for select must be concrete and visually meaningful.
- NEVER reuse fields across unrelated domains.

🅰️ TEXT FIELD GUIDANCE — VERY IMPORTANT:
If the design typically displays TEXT on it (logo, business card, banner, t-shirt with phrase,
poster, mug, sticker with text, packaging label, signage, certificate, menu...), you MUST
include a "text" field with key="text_on_design" so the user provides EXACTLY what text to show.
Also include a "color" field for the text color and optionally a "select" field for font style.
Common Arabic typography choices for the font_style select: "خط ديواني فاخر", "خط كوفي عصري",
"خط النسخ التقليدي", "خط ثلث", "خط رقعة عربي حديث", "Sans-serif Modern", "Serif Classic".

Return JSON only."""


_MEGA_SYSTEM = """You are a world-class art director and prompt engineer for FLUX.1-dev image
generation. Your prompts must produce magazine-quality, professional-grade visual designs.

You will receive:
- The user's raw idea (any language)
- The detected design domain
- A set of selected options (key → value)
- Optionally: descriptions of reference images uploaded by the user
- Optionally: text the user wants ON the design (especially Arabic — see CRITICAL RULE below)

🎯 PRODUCE A CINEMA-GRADE PROMPT (150-220 words) that includes EVERY relevant aspect:

1. **SUBJECT** — describe it with precise visual specificity. For products: invisible mannequin,
   floating presentation, hand-held, in-context use. For spaces: architectural angle, room corner
   view, eye-level perspective. For logos: clean isolation, brand mark presentation.

2. **CAMERA & FRAMING** — specify exactly: lens (35mm | 50mm | 85mm | 24-70mm), aperture
   (f/2.8 shallow DOF | f/8 sharp | f/16 deep focus), angle (eye-level | low | high | overhead
   flat-lay | three-quarter view), framing (close-up | medium | wide), composition rule
   (rule of thirds | golden ratio | symmetrical centered).

3. **LIGHTING** — describe the lighting setup like a photographer: key light direction (45°
   right | top-down | rembrandt | butterfly), fill light, rim/back light, softbox vs hard
   light, golden hour vs studio, color temperature (warm 3200K | neutral 5500K | cool 7500K).

4. **MATERIALS, TEXTURE, FINISH** — specifics. "Premium cotton with subtle weave", "matte
   ceramic with micro-scratches", "polished walnut wood grain", "brushed aluminum",
   "glossy ink on uncoated 350gsm paper". Avoid vague words like "nice texture".

5. **COLOR PALETTE** — exact colors with mood. Use hex codes from selections. Describe
   gradients, dominant/accent split, color harmony (analogous | complementary | triadic).

6. **STYLE & ART DIRECTION** — reference visual language: "Apple product photography",
   "Aesop minimalist branding", "Wes Anderson symmetry", "Scandinavian interior",
   "Bauhaus geometric", "1970s editorial", "Y2K chrome". Pick what matches the domain.

7. **DETAIL ENHANCERS** — always include: "ultra-detailed", "8K", "sharp focus",
   "professional color grading", "high dynamic range", "magazine quality", "shot on
   medium format camera" (when realistic), "octane render" (when CGI).

🅰️ CRITICAL — TEXT HANDLING (READ CAREFULLY):
FLUX cannot reliably render any text, ESPECIALLY Arabic/RTL. If user selections include
text content (any "text" or "text_on_design" field with non-empty value):
  • DO NOT include the actual text characters in the mega_prompt.
  • INSTEAD describe a clean, well-lit RECTANGULAR EMPTY AREA where text will be overlaid
    afterwards in post-processing.
  • Example: "...with a centered horizontal clean empty area roughly 60% width × 15% height
    in the chest region, lit evenly with no shadows, ready for text overlay..."
  • Set "text_overlay" object in JSON output with {text, position, color}.

If NO text in selections → omit text_overlay, and instruct in negative_prompt to avoid
any letters/glyphs/text artifacts.

🅱️ NEGATIVE PROMPT — be specific: "blurry, low resolution, jpeg artifacts, watermark,
signature, deformed anatomy, extra fingers, bad proportions, text artifacts, garbled text,
cluttered background, oversaturated, amateur photography, stock photo cliche".

📐 SIZE GUIDANCE:
  - Square (1024x1024): logos, social posts, packaging top-down, product shots
  - Portrait (1024x1536): mobile-first, posters, full-body shots, A4
  - Landscape (1536x1024): banners, t-shirt back, landscape photos

Return STRICT JSON only:
{
  "mega_prompt": "<single dense paragraph, 150-220 words, English>",
  "negative_prompt": "<comma-separated, specific>",
  "recommended_size": "<1024x1024 | 1024x1536 | 1536x1024>",
  "text_overlay": {
    "text": "<the exact text from user selections, preserve original script>",
    "position": "<center | top | bottom | chest>",
    "color": "<hex e.g. #000000>",
    "font_ratio": 0.08
  } | null
}"""


def _call_together_llm(system: str, user: str, *, temperature: float = 0.4) -> dict[str, Any]:
    """يستدعي Together Chat Completions — يجرب الـ fallback chain لو الموديل
    الأول رجع 400/403/404 (مش متاح serverless للحساب)."""
    key = _together_key()
    if not key:
        return {'success': False, 'error': 'together_key_missing'}

    headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
    messages = [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': user},
    ]

    last_error = 'unknown'
    last_detail = ''
    for model_name in _llm_fallback_chain():
        payload = {
            'model': model_name,
            'messages': messages,
            'temperature': temperature,
            'max_tokens': 800,
            'response_format': {'type': 'json_object'},
        }
        try:
            resp = requests.post(_TOGETHER_CHAT_URL, json=payload, headers=headers, timeout=_TIMEOUT_LLM)
            if resp.status_code in (400, 403, 404):
                # الموديل ده مش متاح للحساب — جرب التالي
                body = resp.text[:300]
                logger.warning(f'[DESIGN ENGINE LLM] model={model_name} HTTP {resp.status_code} — trying fallback. body={body}')
                last_error = f'together_llm_http_{resp.status_code}'
                last_detail = body
                continue
            if resp.status_code != 200:
                logger.warning(f'[DESIGN ENGINE LLM] model={model_name} HTTP {resp.status_code}: {resp.text[:200]}')
                return {
                    'success': False,
                    'error': f'together_llm_http_{resp.status_code}',
                    'detail': resp.text[:200],
                    'model_tried': model_name,
                }

            try:
                data = resp.json()
            except ValueError as e:
                logger.warning(f'[DESIGN ENGINE LLM] non-JSON body: {e}')
                return {'success': False, 'error': 'together_llm_invalid_body'}

            choices = data.get('choices') or []
            if not choices:
                return {'success': False, 'error': 'together_llm_empty_choices'}
            raw = (choices[0].get('message') or {}).get('content') or ''
            if not raw.strip():
                return {'success': False, 'error': 'together_llm_empty_content'}
            raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as e:
                logger.warning(f'[DESIGN ENGINE LLM] JSON parse failed: {e} | raw={raw[:200]}')
                return {'success': False, 'error': 'together_llm_invalid_json'}
            return {'success': True, 'data': parsed, 'model_used': model_name}

        except requests.Timeout:
            last_error = 'together_llm_timeout'
            logger.warning(f'[DESIGN ENGINE LLM] timeout on model={model_name} — trying fallback')
            continue
        except Exception as e:
            logger.exception(f'[DESIGN ENGINE LLM] crashed on model={model_name}')
            last_error = str(e)[:120]
            continue

    return {'success': False, 'error': last_error, 'detail': last_detail, 'all_models_failed': True}


def describe_reference_image(image_data_url: str, *, hint: str = '') -> dict[str, Any]:
    """يحلل صورة مرفوعة (data URL base64) بـ Together Vision model ويرجع
    وصف مختصر بالإنجليزي يقدر يندمج في الـ Mega Prompt.

    image_data_url: 'data:image/jpeg;base64,...' أو 'data:image/png;base64,...'
    hint: تلميح للوصف (مثلاً: 'logo' / 'wall texture' / 'product reference')
    """
    if not image_data_url or not image_data_url.startswith('data:image/'):
        return {'success': False, 'error': 'invalid_image_data'}

    key = _together_key()
    if not key:
        return {'success': False, 'error': 'together_key_missing'}

    system = (
        'You are a visual analyst for an image-generation pipeline. '
        'Describe the uploaded reference image in 1-3 sentences focused on '
        'visual style: colors (use hex if possible), texture, composition, '
        'shapes, materials, mood. Be specific and useful for FLUX prompting. '
        'Reply with PLAIN ENGLISH ONLY — no JSON, no markdown.'
    )
    user_content = [
        {'type': 'text', 'text': f'Hint: {hint or "general reference"}. Describe this image:'},
        {'type': 'image_url', 'image_url': {'url': image_data_url}},
    ]
    payload = {
        # Together vision-capable models
        'model': 'meta-llama/Llama-3.2-11B-Vision-Instruct-Turbo',
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user_content},
        ],
        'temperature': 0.3,
        'max_tokens': 250,
    }
    headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
    try:
        resp = requests.post(_TOGETHER_CHAT_URL, json=payload, headers=headers, timeout=_TIMEOUT_LLM)
        if resp.status_code != 200:
            logger.warning(f'[DESIGN VISION] HTTP {resp.status_code}: {resp.text[:200]}')
            return {'success': False, 'error': f'vision_http_{resp.status_code}'}
        data = resp.json()
        desc = (data.get('choices') or [{}])[0].get('message', {}).get('content', '').strip()
        if not desc:
            return {'success': False, 'error': 'vision_empty'}
        return {'success': True, 'description': desc[:600]}
    except requests.Timeout:
        return {'success': False, 'error': 'vision_timeout'}
    except Exception as e:
        logger.exception('[DESIGN VISION] crashed')
        return {'success': False, 'error': str(e)[:120]}


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

    # Clamp 7 fields max
    schema['fields'] = schema['fields'][:7]
    if len(schema['fields']) < 2:
        return {'success': False, 'error': 'too_few_fields', 'raw': schema}

    _ALLOWED_TYPES = {'select', 'multi_select', 'text', 'number', 'dimensions', 'color', 'range'}

    # Normalize each field according to its type
    cleaned = []
    for f in schema['fields']:
        if not isinstance(f, dict):
            continue
        key = str(f.get('key') or '').strip()
        label = str(f.get('label') or '').strip()
        ftype = str(f.get('type') or 'select').strip().lower()
        if not key or not label:
            continue
        if ftype not in _ALLOWED_TYPES:
            ftype = 'select'

        field = {
            'key': re.sub(r'[^a-z0-9_]', '_', key.lower())[:40] or 'field',
            'label': label[:80],
            'type': ftype,
        }
        # Optional metadata
        unit = f.get('unit')
        if unit:
            field['unit'] = str(unit)[:10]
        default = f.get('default')
        if default not in (None, ''):
            field['default'] = str(default)[:100]
        placeholder = f.get('placeholder')
        if placeholder:
            field['placeholder'] = str(placeholder)[:100]

        if ftype in ('select', 'multi_select'):
            opts = f.get('options') or []
            if not isinstance(opts, list) or len(opts) < 2:
                # Skip — invalid select/multi_select
                continue
            field['options'] = [str(o)[:80] for o in opts[:8]]

        elif ftype == 'dimensions':
            axes = f.get('axes') or ['length', 'width']
            if not isinstance(axes, list) or len(axes) not in (2, 3):
                axes = ['length', 'width']
            field['axes'] = [str(a)[:20] for a in axes[:3]]
            field.setdefault('unit', 'cm')

        elif ftype in ('number', 'range'):
            for k_param in ('min', 'max', 'step'):
                v = f.get(k_param)
                if isinstance(v, (int, float)):
                    field[k_param] = v

        elif ftype == 'color':
            # default should be a hex color
            d = field.get('default', '')
            if not (d.startswith('#') and len(d) in (4, 7)):
                field['default'] = '#7c3aed'

        elif ftype == 'text':
            field.setdefault('placeholder', '')

        cleaned.append(field)

    if len(cleaned) < 2:
        return {'success': False, 'error': 'no_valid_fields'}

    # 🅰️ SAFETY NET: لو الـ raw_idea فيه نص للكتابة على التصميم بس الـ LLM
    # متجاهل وما ضافش حقل text_on_design → نضيفه قسراً
    _ensure_text_field(cleaned, raw)

    return {
        'success': True,
        'domain': str(schema.get('domain') or 'General Design')[:80],
        'domain_ar': str(schema.get('domain_ar') or '')[:80],
        'fields': cleaned,
    }


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────
def _has_arabic(s: str) -> bool:
    if not s:
        return False
    for ch in s:
        if '؀' <= ch <= 'ۿ' or 'ݐ' <= ch <= 'ݿ' or 'ﭐ' <= ch <= '﻿':
            return True
    return False


_TEXT_HINTS_AR = ('مكتوب', 'مكتوبة', 'كتابة', 'اسم', 'شعار', 'عبارة', 'جملة',
                  'نص', 'كلمة', '«', '»', 'يكتب', 'بيكتب', 'بكتب')
_TEXT_HINTS_EN = ('write', 'written', 'text', 'logo', 'name', 'phrase', 'word',
                  'saying', 'quote', 'caption', 'tagline')


def _ensure_text_field(fields: list[dict], raw_idea: str) -> None:
    """لو الـ idea فيه نص محتمل للطباعة على التصميم، يضيف حقول الـ text
    تلقائياً (text_on_design + text_color + font_style) لو الـ LLM ما ضافش."""
    if not fields:
        return
    # عندنا حقل نص مسبقاً؟
    has_text_field = any(
        f['type'] == 'text' and 'text' in f['key'].lower()
        for f in fields
    )
    if has_text_field:
        return

    raw_lower = (raw_idea or '').lower()
    needs_text = any(h in raw_lower for h in _TEXT_HINTS_EN)
    if not needs_text:
        needs_text = any(h in (raw_idea or '') for h in _TEXT_HINTS_AR)
    # برضو لو الـ raw فيه أي نص عربي بشكل عام، نقترح حقل (الـ user يقدر يسيبه فاضي)
    if not needs_text and _has_arabic(raw_idea):
        needs_text = True

    if not needs_text:
        return

    # اقترح نص محتمل من الـ raw_idea (لو فيه quoted text)
    suggested = ''
    for marker in ('"', '«', "'"):
        if marker in (raw_idea or ''):
            parts = raw_idea.split(marker)
            if len(parts) >= 3:
                suggested = parts[1][:50]
                break

    fields.insert(0, {
        'key': 'text_on_design',
        'label': 'النص اللي عاوزه يظهر على التصميم',
        'type': 'text',
        'placeholder': 'مثال: "خليك جميل" أو اسم البراند',
        'default': suggested,
    })
    fields.insert(1, {
        'key': 'text_color',
        'label': 'لون النص',
        'type': 'color',
        'default': '#000000',
    })


def compose_mega_prompt(
    raw_idea: str,
    domain: str,
    selections: dict[str, str],
    reference_descriptions: list[str] | None = None,
) -> dict[str, Any]:
    """يدمج الفكرة + الاختيارات + أوصاف الصور المرجعية في English mega prompt."""
    raw = (raw_idea or '').strip()
    if not raw:
        return {'success': False, 'error': 'empty_idea'}

    selection_lines = '\n'.join(
        f'- {k}: {v}' for k, v in (selections or {}).items() if v
    )
    refs = ''
    if reference_descriptions:
        refs = '\n\nReference images uploaded by the user (incorporate their visual style):\n'
        for i, desc in enumerate(reference_descriptions, 1):
            refs += f'  [{i}] {desc}\n'

    user_msg = (
        f'Raw idea: {raw}\n'
        f'Detected domain: {domain or "General"}\n'
        f'User selections:\n{selection_lines or "(none)"}'
        + refs
    )

    result = _call_together_llm(_MEGA_SYSTEM, user_msg, temperature=0.3)
    if not result.get('success'):
        return result

    data = result['data']
    mega = str(data.get('mega_prompt') or '').strip()
    if not mega:
        return {'success': False, 'error': 'empty_mega_prompt'}

    # Extract text overlay instruction (will be applied post-FLUX)
    overlay = data.get('text_overlay')
    text_overlay = None
    if isinstance(overlay, dict) and overlay.get('text'):
        text_overlay = {
            'text': str(overlay.get('text'))[:200],
            'position': str(overlay.get('position', 'center'))[:20],
            'color': str(overlay.get('color', '#000000'))[:10],
            'font_ratio': float(overlay.get('font_ratio') or 0.08),
        }

    # 🅰️ SAFETY NET: لو الـ LLM متجاهل ورجع null، بنفحص الـ selections بنفسنا
    # ولو لقينا text بقيمة فيها عربي (أو text_on_design موجود) نعمل overlay قسراً
    if not text_overlay:
        text_keys = ('text_on_design', 'design_text', 'logo_text', 'shirt_text',
                     'banner_text', 'message', 'phrase', 'tagline', 'brand_name',
                     'cover_text', 'company_name', 'title_text')
        text_color = None
        text_value = None
        for k, v in (selections or {}).items():
            if not v:
                continue
            kl = k.lower()
            if any(t in kl for t in text_keys) and not _has_arabic(k):  # key matches
                text_value = str(v)
            elif 'text' in kl and 'color' in kl:
                text_color = str(v)
            elif any(t in kl for t in ('color',)) and not text_color and kl.startswith('text'):
                text_color = str(v)
        # لو لقينا text value → اعمل overlay
        if text_value:
            # position default = center؛ للقمصان نخليه chest
            pos = 'center'
            if 'shirt' in (domain or '').lower() or 'تيشرت' in (domain or ''):
                pos = 'chest'
            text_overlay = {
                'text': text_value[:200],
                'position': pos,
                'color': (text_color or '#000000')[:10],
                'font_ratio': 0.08,
            }

    return {
        'success': True,
        'mega_prompt': mega[:2500],
        'negative_prompt': str(data.get('negative_prompt') or
            'blurry, low resolution, jpeg artifacts, watermark, signature, deformed anatomy, '
            'text artifacts, garbled text, cluttered, oversaturated, amateur'
        )[:600],
        'recommended_size': str(data.get('recommended_size') or '1024x1024')[:20],
        'text_overlay': text_overlay,
    }
