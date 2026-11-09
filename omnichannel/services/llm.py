"""
Provider-agnostic LLM client for the auto-reply engine.

Two provider families are supported:
  • OpenAI  — Chat Completions API (tenant BYO key)
  • Gemini  — generateContent REST API (tenant BYO key, or the platform key as
              the default "platform" provider)

We stick to plain REST + `requests` to match the rest of this codebase
(erp_core/ai/*, messenger_bot/services/gemini_service.py) and avoid adding an
SDK dependency. `generate_reply` builds the system prompt from the tenant's
custom AI instructions and the live catalogue context, then returns the model's
text — or None if generation failed (the caller then sends the fallback message).
"""
from __future__ import annotations

import logging
from typing import Optional

import requests
from django.conf import settings

logger = logging.getLogger("mouss_tec_core")

_TIMEOUT = 25
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


def build_system_prompt(config, catalog_context: str) -> str:
    """Compose the grounding system prompt from the tenant's settings."""
    business = config.business_display_name or config.tenant.name
    tone = config.tone_of_voice or "ودود ومحترف وموجز."
    discount = config.discount_policy.strip()
    extra = config.custom_instructions.strip()
    catalog = catalog_context.strip() or "(لا توجد بيانات كتالوج مطابقة الآن)"

    parts = [
        f"أنت مساعد خدمة عملاء آلي يعمل نيابة عن «{business}».",
        "تردّ على العملاء عبر واتساب/ماسنجر تلقائياً.",
        "",
        "القواعد الأساسية:",
        "- ردّ بنفس لغة ولهجة العميل (عربي أو إنجليزي).",
        f"- النبرة المطلوبة: {tone}",
        "- استند في الأسعار والتوفر إلى «الكتالوج» أدناه فقط. لا تخترع سعراً أو صنفاً أو توفراً.",
        "- إذا لم يغطِّ الكتالوج سؤال العميل، اطلب توضيحاً أو اعرض تحويله لموظف بشري.",
        "- كن موجزاً (أقل من ~120 كلمة) ما لم يطلب العميل تفاصيل أكثر.",
        "- قبل رسالة العميل الحالية فيه آخر رسايل المحادثة معاه — استخدمها عشان تفهم «بكام؟» أو «والتانية؟»"
        " بتتكلم عن إيه، ومتعيدش ترحيب أو أسئلة اتجاوبت.",
        "- الرسالة اللي أولها (رسالة صوتية) دي تفريغ فويس الزبون، و(صورة) وصف صورة بعتها — اتعامل معاهم"
        " كأنه كتبهم.",
    ]
    if discount:
        parts += ["", f"سياسة الخصومات (التزم بها حرفياً):\n{discount}"]
    if extra:
        parts += ["", f"تعليمات إضافية من الشركة:\n{extra}"]
    parts += ["", "الكتالوج (بيانات حيّة من مخزون الشركة):", catalog]
    return "\n".join(parts)


def generate_reply(config, user_message: str, catalog_context: str,
                   history: Optional[list] = None) -> Optional[str]:
    """Return the model's reply text, or None on failure.

    ``history``: [(customer_text, reply_text), ...] أقدم الأول — آخر رسايل نفس الزبون.
    🐛 [FIX]: كل رسالة كانت بتتبعت للموديل لوحدها، فـ «طيب بكام؟» بعد «عندك فلتر X5؟»
    كانت بتترد عليها كأنها أول رسالة.
    """
    system_prompt = build_system_prompt(config, catalog_context)
    history = list(history or [])

    provider = config.llm_provider
    tenant_key = config.llm_api_key
    model = config.llm_model.strip()

    try:
        if provider == config.LLMProvider.OPENAI and tenant_key:
            return _call_openai(tenant_key, model or "gpt-4o-mini", system_prompt, user_message, history)
        if provider == config.LLMProvider.GEMINI and tenant_key:
            return _call_gemini(tenant_key, model or _default_gemini_model(), system_prompt, user_message,
                                history)
    except requests.RequestException as exc:
        logger.warning("omnichannel: BYO LLM call failed, falling back to platform: %s", exc)

    # Platform default (or fallback) — Gemini via the platform key.
    return _call_platform_gemini(system_prompt, user_message, model, history)


def _default_gemini_model() -> str:
    # `gemini-flash-latest` always resolves to the current stable Flash model,
    # so deployments don't break when a specific version (e.g. gemini-2.0-flash)
    # is retired. Override per-environment with OMNICHANNEL_GEMINI_MODEL.
    return getattr(settings, "OMNICHANNEL_GEMINI_MODEL", "") or "gemini-3.6-flash"


def _call_platform_gemini(system_prompt: str, user_message: str, model: str,
                          history: Optional[list] = None) -> Optional[str]:
    api_key = getattr(settings, "GEMINI_API_KEY", "")
    if not api_key:
        logger.error("omnichannel: no platform GEMINI_API_KEY for fallback reply")
        return None
    try:
        return _call_gemini(api_key, model or _default_gemini_model(), system_prompt, user_message, history)
    except requests.RequestException as exc:
        logger.exception("omnichannel: platform Gemini call failed: %s", exc)
        return None


# ── OpenAI ────────────────────────────────────────────────────────────
def _history_turns(history) -> list[tuple[str, str]]:
    """[(role, text)] بالترتيب: user ثم assistant لكل تبادل — من غير خانات فاضية."""
    turns = []
    for inbound, outbound in history or []:
        if (inbound or "").strip():
            turns.append(("user", inbound.strip()))
        if (outbound or "").strip():
            turns.append(("assistant", outbound.strip()))
    return turns


def _call_openai(api_key: str, model: str, system_prompt: str, user_message: str,
                 history: Optional[list] = None) -> Optional[str]:
    messages = [{"role": "system", "content": system_prompt}]
    messages += [{"role": r, "content": t} for r, t in _history_turns(history)]
    messages.append({"role": "user", "content": user_message})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": 512,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = requests.post(_OPENAI_URL, json=payload, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("omnichannel: unexpected OpenAI response shape: %s", str(data)[:300])
        return None
    return (text or "").strip() or None


# ── Gemini ────────────────────────────────────────────────────────────
def _gemini_contents(history, user_message: str) -> list[dict]:
    """Gemini محتاج الأدوار متبادلة (user/model) — رسايل ورا بعض من نفس الدور بتتدمج."""
    contents: list[dict] = []
    for role, text in _history_turns(history) + [("user", user_message)]:
        g_role = "model" if role == "assistant" else "user"
        if contents and contents[-1]["role"] == g_role:
            contents[-1]["parts"][0]["text"] += "\n" + text
        else:
            contents.append({"role": g_role, "parts": [{"text": text}]})
    return contents


def _call_gemini(api_key: str, model: str, system_prompt: str, user_message: str,
                 history: Optional[list] = None) -> Optional[str]:
    url = _GEMINI_URL_TEMPLATE.format(model=model)
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": _gemini_contents(history, user_message),
        "generationConfig": {
            "temperature": 0.4,
            # Gemini 2.5/3.x "thinking" models spend output tokens on internal
            # reasoning; give generous headroom so the reply text isn't eaten.
            # (thinkingConfig is NOT sent — gemini-3.x rejects thinkingBudget=0.)
            "maxOutputTokens": 2048,
            "topP": 0.9,
        },
    }
    resp = requests.post(url, params={"key": api_key}, json=payload, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError):
        logger.warning("omnichannel: unexpected Gemini response shape: %s", str(data)[:300])
        return None
    return (text or "").strip() or None
