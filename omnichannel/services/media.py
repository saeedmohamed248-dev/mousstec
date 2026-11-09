"""
🎙️🖼️ فهم الفويس والصور اللي الزبون بيبعتها على واتساب/ماسنجر/إنستجرام.

الزبون في مصر كتير بيبعت فويس «عندك طرمبة بنزين لـ X5؟» أو صورة القطعة بدل ما يكتب.
هنا بننزّل الملف من Meta وبنطلب من Gemini (نفس المفتاح اللي البوت بيستخدمه):
  • الفويس ⇒ تفريغ نصّي بالعامية زي ما اتقال.
  • الصورة ⇒ سطر واحد: اسم القطعة + رقمها لو مكتوب + العربية لو باينة.
والنص ده بيدخل نفس طريق الرسالة المكتوبة (بحث المخزن + الرد).

مابيرميش استثناءات: أي فشل بيرجّع "" والمهمة بترد «اكتبلي اسم القطعة» وبتبلّغ صاحب المحل.
"""
from __future__ import annotations

import base64
import logging

import requests
from django.conf import settings

from .llm import _GEMINI_URL_TEMPLATE, _default_gemini_model
from .meta_api import _graph_base

logger = logging.getLogger("mouss_tec_core")

MAX_BYTES = 15 * 1024 * 1024
_TIMEOUT = 30

AUDIO_PROMPT = (
    "دي رسالة صوتية بعتها زبون على واتساب لمحل (قطع غيار سيارات / مركز صيانة / مطبعة). "
    "فرّغها نصّياً بنفس اللهجة اللي اتقالت بيها (غالباً عامية مصرية)، من غير ترجمة ولا تعليق ولا مقدمات. "
    "أسماء القطع والعربيات وأرقامها اكتبها زي ما اتنطقت. لو مفيش كلام مفهوم رجّع سطر فاضي."
)
IMAGE_PROMPT = (
    "دي صورة بعتها زبون لمحل قطع غيار سيارات / مركز صيانة / مطبعة. "
    "اكتب سطر واحد بس فيه: اسم القطعة أو المنتج بالعربي والإنجليزي، ورقم القطعة لو مكتوب عليها، "
    "والماركة ونوع العربية لو باينين. من غير مقدمات. لو الصورة مش لقطعة أو منتج، اوصف المطلوب منها في سطر."
)


def download(*, media_id: str = "", media_url: str = "", access_token: str = "") -> tuple[bytes, str]:
    """(bytes, mime) — واتساب: id ⇒ لينك مؤقت بالتوكن ⇒ الملف. ماسنجر/إنستجرام: اللينك مباشرة."""
    headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
    if media_id:
        meta = requests.get(f"{_graph_base()}/{media_id}", headers=headers, timeout=_TIMEOUT)
        meta.raise_for_status()
        info = meta.json()
        media_url = info.get("url") or ""
        mime = info.get("mime_type") or ""
        if int(info.get("file_size") or 0) > MAX_BYTES:
            raise ValueError("media too large")
    else:
        mime = ""
        headers = {}            # لينكات ماسنجر/إنستجرام موقّعة ومش محتاجة توكن
    if not media_url:
        raise ValueError("no media url")
    resp = requests.get(media_url, headers=headers, timeout=_TIMEOUT, stream=True)
    resp.raise_for_status()
    data = resp.raw.read(MAX_BYTES + 1, decode_content=True)
    if len(data) > MAX_BYTES:
        raise ValueError("media too large")
    mime = (mime or resp.headers.get("Content-Type", "") or "").split(";")[0].strip()
    return data, mime


def _gemini_key(config) -> str:
    try:
        if config.llm_provider == config.LLMProvider.GEMINI and config.llm_api_key:
            return config.llm_api_key
    except Exception:  # noqa: BLE001
        pass
    return getattr(settings, "GEMINI_API_KEY", "") or ""


def understand(config, *, kind: str, data: bytes, mime: str) -> str:
    """نص الفويس أو وصف الصورة — أو "" لو مقدرناش."""
    key = _gemini_key(config)
    if not key or not data:
        return ""
    # واتساب بيبعت «audio/ogg; codecs=opus» — Gemini عايز النوع من غير باراميترات
    mime = (mime or "").split(";")[0].strip() or ("audio/ogg" if kind == "audio" else "image/jpeg")
    prompt = AUDIO_PROMPT if kind == "audio" else IMAGE_PROMPT
    payload = {
        "contents": [{"role": "user", "parts": [
            {"text": prompt},
            {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}},
        ]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1024},
    }
    model = (getattr(settings, "OMNICHANNEL_MEDIA_MODEL", "") or _default_gemini_model())
    try:
        resp = requests.post(_GEMINI_URL_TEMPLATE.format(model=model), params={"key": key},
                             json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        parts = resp.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("omnichannel: media understanding (%s) failed: %s", kind, exc)
        return ""
