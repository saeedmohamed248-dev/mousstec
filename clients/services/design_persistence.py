"""
🛡️ Canonical CustomerDesign persistence (industry-standard pattern).

Provider image URLs (Together AI / FLUX / Kontext) expire after ~1 hour. Any
code path that creates a CustomerDesign with the raw provider URL stored in
`image_url` will produce broken cards in the customer's gallery once the URL
times out.

This module is the **single source of truth**: it downloads the image to the
project's default_storage (local FS or S3 — both work with django-storages),
generates two WebP thumbnails for fast gallery rendering, and only then
writes the CustomerDesign row with canonical, permanent URLs.

Industry-standard variants (matches Canva / Adobe Firefly / Midjourney):
  • thumb    — 200×200 WebP, served in the gallery grid (~15 KB/image)
  • preview  — 512×512 WebP, served in modals & light-boxes (~60 KB)
  • original — full resolution PNG/JPG as returned by the AI provider

Idempotent: if the URL passed in already points at our own MEDIA_URL, the
download is skipped and the URL is used verbatim (e.g. when an upstream
logo-composite step already saved a local copy).
"""
from __future__ import annotations

import logging
import uuid
from io import BytesIO
from typing import Any, Optional

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.http import HttpRequest
from django.utils import timezone

from clients.models import CustomerDesign, MarketplaceCustomer

logger = logging.getLogger('mouss_tec_core')

# Industry defaults — match the per-row payload budgets used by Canva
# (≈15 KB thumb / ≈60 KB preview). Tunable via settings without redeploy.
THUMB_SIZE_PX = int(getattr(settings, 'DESIGN_THUMB_SIZE_PX', 200))
PREVIEW_SIZE_PX = int(getattr(settings, 'DESIGN_PREVIEW_SIZE_PX', 512))
WEBP_QUALITY = int(getattr(settings, 'DESIGN_WEBP_QUALITY', 85))


def _is_already_persisted(url: str, request: Optional[HttpRequest]) -> bool:
    """True when `url` already lives on our own storage (local or S3-backed)."""
    if not url:
        return False
    media_url = getattr(settings, 'MEDIA_URL', '/media/') or '/media/'
    if url.startswith(media_url):
        return True
    if url.startswith('/media/'):
        return True
    if request is not None:
        try:
            abs_media = request.build_absolute_uri(media_url)
            if url.startswith(abs_media):
                return True
        except Exception:
            pass
    return False


def _absolutize(url: str, request: Optional[HttpRequest]) -> str:
    """Promote `/media/...` → `https://host/media/...` when we have a request.

    S3 backends return absolute URLs already, so this is a no-op for them.
    """
    if not url or not url.startswith('/'):
        return url
    if request is not None:
        try:
            return request.build_absolute_uri(url)
        except Exception:
            return url
    return url


def _generate_variant(original_bytes: bytes, max_dim: int, quality: int) -> bytes:
    """Resize-fit into a max_dim square and re-encode as WebP."""
    from PIL import Image
    img = Image.open(BytesIO(original_bytes))
    # Normalize mode — WebP supports RGB/RGBA only; reject palette/CMYK.
    if img.mode not in ('RGB', 'RGBA'):
        img = img.convert('RGBA' if 'A' in img.mode else 'RGB')
    img.thumbnail((max_dim, max_dim), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format='WEBP', quality=quality, method=6)
    return buf.getvalue()


def _fetch_bytes(url: str) -> Optional[bytes]:
    """Download the provider URL. Returns None on any failure."""
    try:
        import requests
        r = requests.get(url, timeout=30)
        if r.status_code == 200 and r.content:
            return r.content
        logger.warning(f'[DESIGN PERSIST] provider returned HTTP {r.status_code} for {url[:80]}')
    except Exception as e:
        logger.warning(f'[DESIGN PERSIST] fetch failed for {url[:80]}: {e}')
    return None


def _save_bytes(img_bytes: bytes, customer_uid: Any, prefix: str, ext: str) -> str:
    """Save bytes to default_storage and return the storage URL."""
    filename = f'{prefix}/{customer_uid}/{uuid.uuid4().hex}.{ext}'
    saved_path = default_storage.save(filename, ContentFile(img_bytes))
    return default_storage.url(saved_path)


def persist_image_with_variants(
    request: Optional[HttpRequest],
    customer: MarketplaceCustomer,
    *,
    provider_image_url: str,
    prefix: str = 'ai_designs',
) -> dict:
    """Persist original + variants. Returns a dict with all four URLs/metadata.

    Returns:
      {
        'image_url':        canonical original (absolute),
        'thumb_url':        200×200 WebP (absolute),
        'preview_url':      512×512 WebP (absolute),
        'size_bytes':       original byte size,
        'already_local':    True if input was already on our storage,
      }

    Raises RuntimeError on unrecoverable failure — caller should treat as
    a failed generation (don't bill, don't create a DB row).
    """
    if not provider_image_url:
        raise RuntimeError('empty_provider_url')

    customer_uid = getattr(customer, 'uid', None) or customer.pk

    # Fast path: URL already on our storage. We can't derive variants without
    # re-downloading (the storage backend doesn't give us bytes for free), so
    # for now we mirror the URL across all three slots — the gallery still
    # works, just without the bandwidth win. A future job can backfill.
    if _is_already_persisted(provider_image_url, request):
        abs_url = _absolutize(provider_image_url, request)
        return {
            'image_url': abs_url,
            'thumb_url': abs_url,
            'preview_url': abs_url,
            'size_bytes': None,
            'already_local': True,
        }

    original_bytes = _fetch_bytes(provider_image_url)
    if not original_bytes:
        raise RuntimeError('image_fetch_failed')

    # 1) Save the original as-is (PNG — providers return PNG/JPG; we don't
    #    re-encode the original to preserve quality for downloads/print).
    original_url = _save_bytes(original_bytes, customer_uid, prefix, 'png')

    # 2) Generate + save both WebP variants. Failure here is non-fatal —
    #    the gallery falls back to image_url via the template's |default.
    thumb_url = ''
    preview_url = ''
    try:
        thumb_bytes = _generate_variant(original_bytes, THUMB_SIZE_PX, WEBP_QUALITY)
        thumb_url = _save_bytes(thumb_bytes, customer_uid, f'{prefix}/thumbs', 'webp')
    except Exception as e:
        logger.warning(f'[DESIGN PERSIST] thumb generation failed: {e}')

    try:
        preview_bytes = _generate_variant(original_bytes, PREVIEW_SIZE_PX, WEBP_QUALITY)
        preview_url = _save_bytes(preview_bytes, customer_uid, f'{prefix}/previews', 'webp')
    except Exception as e:
        logger.warning(f'[DESIGN PERSIST] preview generation failed: {e}')

    return {
        'image_url': _absolutize(original_url, request),
        'thumb_url': _absolutize(thumb_url, request),
        'preview_url': _absolutize(preview_url, request),
        'size_bytes': len(original_bytes),
        'already_local': False,
    }


def persist_and_create_design(
    request: HttpRequest,
    customer: MarketplaceCustomer,
    *,
    provider_image_url: str,
    title: str,
    description: str,
    raw_input: str,
    engineered_prompt: str,
    engine: str,
    category: str = 'other',
    purchase: Any = None,
    is_free_trial: bool = False,
    regenerations_allowed: int = 0,
    negative_prompt: str = '',
    prefix: str = 'ai_designs',
    **extra_fields: Any,
) -> CustomerDesign:
    """Full pipeline: persist image + variants, then create CustomerDesign.

    Raises RuntimeError if persistence fails — caller MUST handle this and
    avoid billing the customer for a generation we can't store.
    """
    persisted = persist_image_with_variants(
        request, customer,
        provider_image_url=provider_image_url, prefix=prefix,
    )

    return CustomerDesign.objects.create(
        customer=customer,
        purchase=purchase,
        is_free_trial=is_free_trial,
        title=(title or 'Design')[:200],
        description=(description or '')[:1000],
        category=category,
        raw_input=raw_input,
        engineered_prompt=engineered_prompt,
        negative_prompt=negative_prompt,
        image_url=persisted['image_url'][:600],
        image_thumb_url=(persisted['thumb_url'] or '')[:600],
        image_preview_url=(persisted['preview_url'] or '')[:600],
        image_persisted_at=timezone.now(),
        image_size_bytes=persisted['size_bytes'],
        model_used=engine,
        regenerations_allowed=regenerations_allowed,
        **extra_fields,
    )
