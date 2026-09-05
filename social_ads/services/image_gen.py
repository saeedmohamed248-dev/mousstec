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
    """Generate one image for a social post and return an absolute public URL ("" on failure)."""
    prompt = (prompt or "").strip()
    if not prompt:
        return ""
    try:
        from erp_core.ai.printing_copilot import generate_design_image
    except Exception as exc:  # pipeline not available in this deployment
        logger.info("social_ads: image pipeline unavailable: %s", exc)
        return ""

    try:
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
    except Exception as exc:
        logger.warning("social_ads: generate_design_image raised for %s: %s",
                       config.tenant.schema_name, exc)
        return ""

    if not (img and img.get("success")):
        logger.info("social_ads: image gen failed for %s: %s",
                    config.tenant.schema_name, (img or {}).get("error"))
        return ""

    data = _to_bytes(img)
    if not data:
        # Engine returned a URL but no bytes we could fetch — use it directly only
        # if it looks absolute (Together URLs are public for ~1h; we publish now).
        url = img.get("url") or ""
        return url if url.startswith("http") else ""

    try:
        name = f"social_ads/{config.tenant.schema_name}/{uuid.uuid4().hex}.png"
        saved = default_storage.save(name, ContentFile(data))
        return _absolute(default_storage.url(saved))
    except Exception as exc:
        logger.warning("social_ads: image persist failed for %s: %s",
                       config.tenant.schema_name, exc)
        return ""


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
