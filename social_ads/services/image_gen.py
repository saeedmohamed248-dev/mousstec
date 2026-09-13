"""
Image generation for Social Studio posts.

Reuses the platform's premium image pipeline (`erp_core.ai.printing_copilot.
generate_design_image` — FLUX for photos, Ideogram for text-heavy designs) so we
don't add a second image stack. The generated image is persisted to the project's
default storage (S3 in production) and an ABSOLUTE, publicly-fetchable URL is
returned, because Meta fetches the image server-side when publishing (Instagram
in particular requires a reachable image_url).

Wired as the default `settings.SOCIAL_ADS_IMAGE_HOOK`. It is called LAZILY at
publish time (see tasks.publish_post), not when a post is scheduled — production
storage hands out short-lived signed URLs, so generating right before the Meta
call keeps the URL fresh. Never raises: returns "" on any failure, and the
publisher falls back to a text-only Facebook post.

Signature (the hook contract): ``generate_social_image(config, prompt) -> str``.
"""
from __future__ import annotations

import logging
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

logger = logging.getLogger("mouss_tec_core")

_SIZE = "1024x1024"  # square — safe for both Facebook feed and Instagram


def generate_social_image(config, prompt: str) -> str:
    """Generate one image for a social post and return an absolute public URL ("" on failure).

    Order of preference:
      1. The platform FLUX/Ideogram pipeline (printing_copilot) — if a provider key
         is configured on the server.
      2. Gemini image generation using the tenant's OWN Gemini key (the same key
         that writes the captions) — so no extra image key/cost is needed.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return ""

    data = None
    try:
        from erp_core.ai.printing_copilot import generate_design_image
        img = generate_design_image(
            prompt=prompt[:1800],
            size=_SIZE,
            negative_prompt=(
                "low quality, blurry, watermark, distorted text, fake logo, "
                "duplicated elements, jpeg artifacts"
            ),
            category="social_media",
            quality_tier="hd",
        )
        if img and img.get("success"):
            data = _to_bytes(img)
            if not data:
                url = img.get("url") or ""
                if url.startswith("http"):
                    return url
        else:
            logger.info("social_ads: FLUX pipeline unavailable (%s) — trying Gemini",
                        (img or {}).get("error"))
    except Exception as exc:
        logger.info("social_ads: FLUX pipeline error (%s) — trying Gemini", exc)

    # Fallback: generate with the tenant's Gemini key (no separate image key needed).
    if not data:
        data = _generate_via_gemini(config, prompt)
    if not data:
        return ""

    try:
        name = f"social_ads/{config.tenant.schema_name}/{uuid.uuid4().hex}.png"
        saved = default_storage.save(name, ContentFile(data))
        return _absolute(default_storage.url(saved))
    except Exception as exc:
        logger.warning("social_ads: image persist failed for %s: %s",
                       config.tenant.schema_name, exc)
        return ""


# Gemini image-capable models to try (generateContent returns inline image bytes).
_GEMINI_IMAGE_MODELS = (
    "gemini-2.5-flash-image",
    "gemini-2.0-flash-preview-image-generation",
)


def _resolve_gemini_key(config) -> str:
    """The tenant's Gemini key — studio first, then the chatbot's, else platform."""
    key = ""
    try:
        if getattr(config, "llm_provider", "") == getattr(config.LLMProvider, "GEMINI", "gemini"):
            key = config.llm_api_key or ""
    except Exception:
        key = ""
    if not key:
        key = config.llm_api_key or ""
    if not key:
        try:
            from omnichannel.models import TenantChannelConfig
            occ = TenantChannelConfig.objects.filter(tenant=config.tenant).first()
            if occ:
                key = occ.llm_api_key or ""
        except Exception:
            pass
    if not key:
        key = str(getattr(settings, "GEMINI_API_KEY", "") or "")
    return key.strip()


def _generate_via_gemini(config, prompt: str) -> bytes | None:
    """Generate an image via the Gemini API using the tenant's key. Bytes or None."""
    import base64
    import requests

    key = _resolve_gemini_key(config)
    if not key:
        logger.info("social_ads: no Gemini key for image generation (%s)", config.tenant.schema_name)
        return None

    full_prompt = (
        "Create a professional, high-quality square marketing image for a Facebook/"
        "Instagram post. No text overlay, no watermark. Subject: " + prompt[:900]
    )
    for model in _GEMINI_IMAGE_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": full_prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        try:
            resp = requests.post(url, params={"key": key}, json=payload, timeout=60)
        except requests.RequestException as exc:
            logger.warning("social_ads: Gemini image net error on %s: %s", model, exc)
            continue
        if resp.status_code >= 400:
            logger.info("social_ads: Gemini image %s -> %s: %s", model, resp.status_code, resp.text[:200])
            continue
        try:
            parts = resp.json()["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        for p in parts:
            inline = p.get("inlineData") or p.get("inline_data")
            if inline and inline.get("data"):
                try:
                    return base64.b64decode(inline["data"])
                except (ValueError, TypeError):
                    continue
    logger.info("social_ads: Gemini produced no image for %s", config.tenant.schema_name)
    return None


def _to_bytes(img: dict) -> bytes | None:
    """Extract raw image bytes from the engine result (b64 or downloadable url)."""
    b64 = img.get("b64_json")
    if b64:
        import base64
        try:
            return base64.b64decode(b64)
        except (ValueError, TypeError):
            return None
    url = img.get("url")
    if url and url.startswith("http"):
        try:
            import requests
            resp = requests.get(url, timeout=30)
            if resp.status_code < 400 and resp.content:
                return resp.content
        except requests.RequestException:
            return None
    return None


def _absolute(url: str) -> str:
    """Ensure the storage URL is absolute so Meta can fetch it."""
    if not url:
        return ""
    if url.startswith("http"):
        return url  # S3 / custom domain already absolute
    base = getattr(settings, "SOCIAL_ADS_PUBLIC_BASE_URL", "") or \
        f"https://{getattr(settings, 'BASE_DOMAIN', '')}"
    base = base.rstrip("/")
    return f"{base}{url}" if base else url
