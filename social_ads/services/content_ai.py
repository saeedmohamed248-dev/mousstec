"""
LLM content engine for Social Studio.

Two responsibilities:
  • GENERATE — write a ready-to-publish post (caption + hashtags + image brief)
    or ad copy, grounded in the tenant's brand profile AND the learned strategy
    memory, so each batch reflects what already worked for this business.
  • DISTILL — turn a performance summary into a short natural-language "learned
    brief" the next generation ingests (the LLM half of the learning loop; the
    numeric ranking lives in strategist.py).

Provider-agnostic plain REST (OpenAI / Gemini), matching omnichannel/services/
llm.py — no SDK dependency. Every public function degrades gracefully: on any
failure it returns a sensible deterministic fallback instead of raising, so the
autopilot never stalls on a flaky model call.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import requests
from django.conf import settings

logger = logging.getLogger("mouss_tec_core")

_TIMEOUT = 30
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

# The content angles the strategist rotates through and scores.
CONTENT_ANGLES = [
    ("عرض_سعري", "عرض أو خصم أو باقة بسعر واضح وجذّاب"),
    ("نصيحة", "نصيحة مفيدة أو معلومة سريعة تخص مجال النشاط"),
    ("شهادة_عميل", "قصة نجاح أو رأي عميل راضٍ (بأسلوب واقعي)"),
    ("خلف_الكواليس", "لقطة من داخل العمل تبني الثقة والإنسانية"),
    ("سؤال_تفاعلي", "سؤال يشجّع المتابعين على التعليق والمشاركة"),
    ("منتج_مميز", "إبراز منتج/خدمة واحدة ومزاياها"),
    ("مناسبة", "ربط المحتوى بمناسبة أو موسم أو حدث حالي"),
]

_ANGLE_LABELS = {k: v for k, v in CONTENT_ANGLES}


# =====================================================================
# GENERATION
# =====================================================================
def generate_post(config, memory, *, angle: str = "", occasion: str = "",
                  extra_hint: str = "") -> dict:
    """Return a post dict: {caption, hashtags, image_prompt, strategy_angle, rationale}.

    Never raises — falls back to a template if the LLM is unavailable.
    """
    angle = angle or _pick_default_angle(memory)
    system = _post_system_prompt(config, memory)
    user = _post_user_prompt(config, angle, occasion, extra_hint)

    raw = _generate(config, system, user, max_tokens=700)
    parsed = _parse_json(raw) if raw else None
    if parsed and parsed.get("caption"):
        return {
            "caption": _clean(parsed.get("caption", ""), 2100),
            "hashtags": _clean(parsed.get("hashtags", ""), 380),
            "image_prompt": _clean(parsed.get("image_prompt", ""), 500),
            "strategy_angle": angle,
            "rationale": _clean(parsed.get("rationale", ""), 400),
        }
    return _fallback_post(config, angle)


def generate_ad_copy(config, memory, *, objective: str, base_post=None) -> dict:
    """Return ad copy: {primary_text, headline, audience_spec, rationale}."""
    system = _ad_system_prompt(config, memory)
    user = _ad_user_prompt(config, objective, base_post)
    raw = _generate(config, system, user, max_tokens=600)
    parsed = _parse_json(raw) if raw else None
    if parsed and parsed.get("primary_text"):
        return {
            "primary_text": _clean(parsed.get("primary_text", ""), 900),
            "headline": _clean(parsed.get("headline", ""), 190),
            "audience_spec": parsed.get("audience_spec") or {},
            "rationale": _clean(parsed.get("rationale", ""), 400),
        }
    return _fallback_ad(config, base_post)


def summarize_learnings(config, stats_summary: str) -> str:
    """Ask the LLM to distill a performance summary into a short actionable brief.

    Returns "" on failure — the caller keeps the previous brief.
    """
    system = (
        "أنت خبير تسويق رقمي تحلّل أداء صفحة على فيسبوك وإنستجرام. "
        "اقرأ ملخص الأرقام واكتب توصيات عملية موجزة (٤–٦ نقاط) عن نوع المحتوى "
        "وأوقات النشر والأسلوب الذي يزيد التفاعل لهذا النشاط تحديداً. "
        "اكتب بالعربية، نقاطاً قصيرة قابلة للتنفيذ، دون مقدمات."
    )
    raw = _generate(config, system, stats_summary, max_tokens=400)
    return _clean(raw or "", 1500)


# =====================================================================
# Prompt builders
# =====================================================================
def _brand_block(config) -> str:
    lines = [
        f"النشاط: {config.business_display_name or config.tenant.name}",
        f"المجال: {config.industry or 'غير محدد'}",
        f"المنتجات/الخدمات: {config.products_services or 'غير محدد'}",
        f"الجمهور المستهدف: {config.target_audience or 'عام'}",
        f"النبرة: {config.brand_tone}",
        f"الأهداف: {config.business_goals}",
        f"الدعوة لاتخاذ إجراء: {config.call_to_action}",
    ]
    if config.contact_phone:
        lines.append(f"رقم التواصل: {config.contact_phone}")
    if config.website_url:
        lines.append(f"الموقع: {config.website_url}")
    if config.brand_keywords:
        lines.append(f"كلمات مفتاحية مميّزة: {config.brand_keywords}")
    if config.banned_words:
        lines.append(f"عبارات ممنوعة (لا تستخدمها إطلاقاً): {config.banned_words}")
    lang = {"ar": "العربية", "en": "الإنجليزية", "mix": "العربية مع لمسات إنجليزية"}.get(
        config.default_language, "العربية")
    lines.append(f"لغة المحتوى: {lang}")
    return "\n".join(lines)


def _memory_block(memory) -> str:
    if not memory or not memory.learned_brief:
        return ""
    return (
        "\n\nما تعلّمه البوت من أداء المنشورات السابقة (التزم به):\n"
        f"{memory.learned_brief.strip()}"
    )


def _post_system_prompt(config, memory) -> str:
    return (
        "أنت مدير تسويق محترف يدير حسابات فيسبوك وإنستجرام لنشاط تجاري. "
        "مهمتك كتابة منشور واحد جاهز للنشر يجذب الانتباه ويحقّق هدف التسويق.\n\n"
        "معلومات العلامة التجارية:\n"
        f"{_brand_block(config)}"
        f"{_memory_block(memory)}\n\n"
        "أعد الإجابة بصيغة JSON فقط بهذا الشكل بالضبط دون أي نص خارجها:\n"
        '{"caption": "نص المنشور بالكامل مع إيموجي مناسبة",'
        ' "hashtags": "#هاشتاج1 #هاشتاج2 (٥–٨ هاشتاجات ملائمة)",'
        ' "image_prompt": "وصف بصري دقيق بالإنجليزية لتوليد صورة احترافية للمنشور",'
        ' "rationale": "سبب مختصر لاختيار هذه الزاوية"}'
    )


def _post_user_prompt(config, angle: str, occasion: str, extra_hint: str) -> str:
    angle_desc = _ANGLE_LABELS.get(angle, angle)
    parts = [f"اكتب منشوراً بزاوية «{angle}»: {angle_desc}."]
    if occasion:
        parts.append(f"اربطه بالمناسبة/السياق: {occasion}.")
    if extra_hint:
        parts.append(f"ملاحظة إضافية: {extra_hint}.")
    parts.append("اجعله موجزاً وجذّاباً ومناسباً لطبيعة النشاط.")
    return " ".join(parts)


def _ad_system_prompt(config, memory) -> str:
    return (
        "أنت خبير إعلانات مدفوعة على Meta (فيسبوك/إنستجرام). اكتب نص إعلان يحقّق "
        "أعلى نسبة نقر/تحويل ضمن سياسات Meta الإعلانية.\n\n"
        "معلومات العلامة التجارية:\n"
        f"{_brand_block(config)}"
        f"{_memory_block(memory)}\n\n"
        "أعد JSON فقط بهذا الشكل:\n"
        '{"primary_text": "نص الإعلان الأساسي (٢–٤ أسطر)",'
        ' "headline": "عنوان قصير قوي (أقل من ٤٠ حرف)",'
        ' "audience_spec": {"interests": ["اهتمام1","اهتمام2"], "age_min": 21, "age_max": 55,'
        ' "genders": "all", "geo": "المدن/المناطق المقترحة"},'
        ' "rationale": "سبب اختيار هذا الجمهور والرسالة"}'
    )


def _ad_user_prompt(config, objective: str, base_post) -> str:
    objmap = {
        "OUTCOME_AWARENESS": "زيادة الوعي بالعلامة والوصول لأكبر جمهور",
        "OUTCOME_TRAFFIC": "جلب زيارات ونقرات للموقع/المتجر",
        "OUTCOME_ENGAGEMENT": "زيادة التفاعل والرسائل",
        "OUTCOME_LEADS": "جمع بيانات عملاء محتملين",
        "OUTCOME_SALES": "تحقيق مبيعات مباشرة",
    }
    goal = objmap.get(objective, objective)
    txt = [f"هدف الحملة: {goal}."]
    if base_post is not None and getattr(base_post, "caption", ""):
        txt.append(f"استلهم من هذا المنشور: {base_post.caption[:400]}")
    txt.append("اقترح جمهوراً دقيقاً مناسباً للنشاط والمنطقة.")
    return " ".join(txt)


# =====================================================================
# Deterministic fallbacks (never let the autopilot stall)
# =====================================================================
def _pick_default_angle(memory) -> str:
    if memory:
        best = memory.best_angles(1)
        if best:
            return best[0]
    return CONTENT_ANGLES[0][0]


def _fallback_post(config, angle: str) -> dict:
    name = config.business_display_name or config.tenant.name
    products = (config.products_services or "خدماتنا").split("\n")[0][:80]
    caption = (
        f"في {name} بنقدّملك {products} بأعلى جودة وأفضل سعر. 💪\n"
        f"{config.call_to_action}"
    )
    if config.contact_phone:
        caption += f"\n📞 {config.contact_phone}"
    tags = _default_hashtags(config)
    return {
        "caption": caption,
        "hashtags": tags,
        "image_prompt": f"Professional marketing photo for {config.industry or 'a local business'}, clean, bright, high quality",
        "strategy_angle": angle,
        "rationale": "مسودة افتراضية (تعذّر توليد المحتوى بالذكاء الاصطناعي — سيُعاد المحاولة).",
    }


def _fallback_ad(config, base_post) -> dict:
    name = config.business_display_name or config.tenant.name
    text = base_post.caption if (base_post and base_post.caption) else (
        f"جرّب خدمات {name} الآن. {config.call_to_action}")
    return {
        "primary_text": text[:600],
        "headline": (name or "اطلب الآن")[:190],
        "audience_spec": {"age_min": 21, "age_max": 55, "genders": "all"},
        "rationale": "إعداد افتراضي.",
    }


def _default_hashtags(config) -> str:
    tags = []
    for kw in (config.brand_keywords or "").split(","):
        kw = kw.strip().replace(" ", "_")
        if kw:
            tags.append("#" + kw)
    if not tags and config.industry:
        tags.append("#" + config.industry.strip().replace(" ", "_"))
    tags.append("#مصر")
    return " ".join(tags[:8])


# =====================================================================
# LLM plumbing (mirrors omnichannel/services/llm.py)
# =====================================================================
def _generate(config, system_prompt: str, user_message: str, *, max_tokens: int) -> Optional[str]:
    provider = config.llm_provider
    tenant_key = config.llm_api_key
    model = (config.llm_model or "").strip()
    try:
        if provider == config.LLMProvider.OPENAI and tenant_key:
            return _call_openai(tenant_key, model or "gpt-4o-mini", system_prompt, user_message, max_tokens)
        if provider == config.LLMProvider.GEMINI and tenant_key:
            return _call_gemini(tenant_key, model or _default_gemini_model(), system_prompt, user_message, max_tokens)
    except requests.RequestException as exc:
        logger.warning("social_ads: BYO LLM failed, using platform: %s", exc)
    return _call_platform_gemini(system_prompt, user_message, model, max_tokens)


def _default_gemini_model() -> str:
    return getattr(settings, "OMNICHANNEL_GEMINI_MODEL", "") or "gemini-flash-latest"


def _call_platform_gemini(system_prompt, user_message, model, max_tokens) -> Optional[str]:
    api_key = getattr(settings, "GEMINI_API_KEY", "")
    if not api_key:
        logger.error("social_ads: no platform GEMINI_API_KEY for content generation")
        return None
    try:
        return _call_gemini(api_key, model or _default_gemini_model(), system_prompt, user_message, max_tokens)
    except requests.RequestException as exc:
        logger.exception("social_ads: platform Gemini call failed: %s", exc)
        return None


def _call_openai(api_key, model, system_prompt, user_message, max_tokens) -> Optional[str]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.8,
        "max_tokens": max_tokens,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = requests.post(_OPENAI_URL, json=payload, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    try:
        return (data["choices"][0]["message"]["content"] or "").strip() or None
    except (KeyError, IndexError, TypeError):
        logger.warning("social_ads: unexpected OpenAI shape: %s", str(data)[:300])
        return None


def _call_gemini(api_key, model, system_prompt, user_message, max_tokens) -> Optional[str]:
    url = _GEMINI_URL_TEMPLATE.format(model=model)
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
        "generationConfig": {"temperature": 0.85, "maxOutputTokens": max_tokens, "topP": 0.95},
    }
    resp = requests.post(url, params={"key": api_key}, json=payload, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    try:
        return (data["candidates"][0]["content"]["parts"][0]["text"] or "").strip() or None
    except (KeyError, IndexError, TypeError):
        logger.warning("social_ads: unexpected Gemini shape: %s", str(data)[:300])
        return None


# =====================================================================
# Helpers
# =====================================================================
def _parse_json(raw: str) -> Optional[dict]:
    """Extract the first JSON object from a model response (handles ```json fences)."""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except (ValueError, TypeError):
            return None
    return None


def _clean(text: str, limit: int) -> str:
    t = (text or "").strip()
    return t[:limit] if len(t) > limit else t
