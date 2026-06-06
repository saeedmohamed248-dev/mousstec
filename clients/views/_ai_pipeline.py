"""
Marketplace AI pipeline helpers (Phase N.6+ — C1/C2/C3 unification).

Shared by ``design_store_generate`` / ``_regenerate`` / ``_refine`` so all
three honor the Brand Profile, Smart Router (FLUX/Ideogram), logo composite,
and Quality Gate. Extracted from ``_legacy.py`` as the first step of the
domain-module split (zero-downtime — ``_legacy.py`` re-imports the public
names).

Public API
----------
- :func:`_resolve_brand_context`
- :func:`_persist_remote_image`
- :func:`_composite_brand_logo`
- :func:`_run_marketplace_image_pipeline`

These are intentionally underscore-prefixed because they are an internal
contract between marketplace view modules — not part of the URL surface. The
leading underscore travels with them through ``from ._ai_pipeline import *``
(which would skip them) so all importers reference them by name explicitly.
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger('mouss_tec_core')


def _resolve_brand_context(customer, logo_file=None):
    """Return ``(brand_context, brand_logo_source, logo_was_uploaded)`` for the
    given marketplace customer + optional per-request logo upload.

    Centralizes the CustomerBrandProfile lookup that C1/C2/C3 all need so we
    can't accidentally diverge again (the original C2/C3 bug).
    """
    brand_context = None
    brand_logo_source = None
    logo_was_uploaded = bool(logo_file)
    try:
        bp = getattr(customer, 'brand_profile', None)
        if bp and bp.is_active:
            brand_context = bp.as_brand_context()
            if bp.auto_inject_logo and bp.has_logo:
                brand_context['logo_described'] = True
                brand_logo_source = bp.logo_image
    except Exception as e:
        logger.warning(f'[MP PIPELINE] brand profile lookup failed: {e}')

    if logo_was_uploaded:
        if brand_context is None:
            brand_context = {'brand_name': (customer.full_name or 'Brand')[:60]}
        brand_context['logo_described'] = True
    return brand_context, brand_logo_source, logo_was_uploaded


def _persist_remote_image(request, customer, *, url=None, b64=None, prefix='ai_store'):
    """Persist a (b64 or remote-URL) image to local storage and return the
    absolute media URL. Together AI URLs expire in ~1h so this is mandatory.

    Returns the URL string, or ``None`` if both inputs were empty/failed.
    """
    import base64 as _b64
    import uuid as _uuid
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    img_bytes = None
    if b64:
        try:
            img_bytes = _b64.b64decode(b64)
        except Exception as e:
            logger.warning(f'[MP PIPELINE] b64 decode failed: {e}')
    elif url:
        try:
            import requests as _req
            r = _req.get(url, timeout=30)
            if r.status_code == 200:
                img_bytes = r.content
        except Exception as e:
            logger.warning(f'[MP PIPELINE] remote fetch failed: {e}')
            return url  # caller can still use the remote URL as last resort

    if not img_bytes:
        return url
    filename = f'{prefix}/{customer.uid}/{_uuid.uuid4().hex}.png'
    saved = default_storage.save(filename, ContentFile(img_bytes))
    local = default_storage.url(saved)
    if local.startswith('/'):
        local = request.build_absolute_uri(local)
    return local


def _composite_brand_logo(
    request, *, image_url, logo_source, used_engine, presentation_category,
    text_overlay=None, prefix='ai_store',
):
    """Run logo composite (FLUX path only — Ideogram draws logos natively).

    Returns ``(image_url, composited:bool)``. Non-fatal on failure.
    """
    if not logo_source or used_engine == 'ideogram':
        return image_url, False
    try:
        from erp_core.ai.logo_overlay import composite_logo_on_image_url
        has_text = bool(text_overlay and text_overlay.get('text'))
        comp = composite_logo_on_image_url(
            image_url=image_url,
            logo_source=logo_source,
            category=presentation_category or '',
            text_overlay_position=(
                text_overlay.get('position') if has_text else None
            ),
        )
        if comp.get('success'):
            new_url = comp['url']
            if new_url and new_url.startswith('/'):
                new_url = request.build_absolute_uri(new_url)
            return new_url, True
        if not comp.get('skipped'):
            logger.warning(f'[MP PIPELINE] composite failed (non-fatal): {comp.get("error")}')
    except Exception as e:
        logger.warning(f'[MP PIPELINE] composite exception: {e}')
    return image_url, False


def _run_marketplace_image_pipeline(
    request, customer, *,
    engineered_prompt,
    description,
    category,
    canonical_size,
    logo_file=None,
    block_schnell_fallback=True,
    prefix='ai_store',
):
    """Unified Brand + Smart Router + Composite + Quality Gate pipeline.

    Used by C1 (generate) and C2 (regenerate). Returns either:
        ``{'ok': True, 'image_url', 'used_engine', 'used_model',
            'presentation_category', 'text_overlay', 'quality_score',
            'logo_composited', 'brand_applied', 'logo_was_uploaded',
            'final_prompt'}``
    or:
        ``{'ok': False, 'status': int, 'error_payload': dict}``
    """
    brand_context, brand_logo_source, logo_was_uploaded = _resolve_brand_context(
        customer, logo_file=logo_file,
    )

    # Stage A — compose (already_engineered=True ⇒ no double LLM rewrite)
    try:
        from erp_core.ai.design_engine import compose_mega_prompt
        mega = compose_mega_prompt(
            raw_idea=engineered_prompt,
            domain=category if category != 'other' else '',
            selections={},
            brand_context=brand_context,
            presentation_category=category if category != 'other' else None,
            already_engineered=True,
        )
    except Exception:
        logger.exception('[MP PIPELINE] compose_mega_prompt crashed')
        return {'ok': False, 'status': 502, 'error_payload': {
            'error': 'تعذرت صياغة البرومبت. حاول تعدّل الوصف.',
        }}

    if not mega.get('success'):
        return {'ok': False, 'status': 502, 'error_payload': {
            'error': 'مقدرناش نصيغ البرومبت — جرب توضّح الوصف.',
            'engine_error': mega.get('error', ''),
        }}

    final_prompt = mega['mega_prompt']
    final_negative = mega.get('negative_prompt', '')
    presentation_category = mega.get('presentation_category') or category
    text_overlay = mega.get('text_overlay')
    has_text = bool(text_overlay and text_overlay.get('text'))

    # Stage B — Smart Router (FLUX for photo, Ideogram for text/logo)
    try:
        from erp_core.ai.printing_copilot import generate_design_image
        img = generate_design_image(
            prompt=final_prompt[:1800],
            size=canonical_size,
            negative_prompt=final_negative or (
                'low quality, blurry, watermark, distorted text, fake logo, '
                'duplicated elements, extra fingers, jpeg artifacts'
            ),
            category=presentation_category,
            has_text_content=has_text,
            block_schnell_fallback=block_schnell_fallback,
        )
    except Exception:
        logger.exception('[MP PIPELINE] generate_design_image crashed')
        return {'ok': False, 'status': 502, 'error_payload': {
            'error': 'فشل توليد التصميم. حاول تاني.',
        }}

    if not img.get('success'):
        err = img.get('error', 'unknown')
        logger.error(f'[MP PIPELINE] image gen failed: {err} — {(img.get("detail") or "")[:200]}')
        return {'ok': False, 'status': 502, 'error_payload': {
            'error': 'فشل توليد التصميم. حاول تاني.',
            'engine_error': err,
        }}

    used_engine = img.get('engine', 'flux')
    used_model = img.get('model') or used_engine

    image_url = _persist_remote_image(
        request, customer, url=img.get('url'), b64=img.get('b64_json'), prefix=prefix,
    )
    if not image_url:
        return {'ok': False, 'status': 502, 'error_payload': {
            'error': 'محرك التوليد لم يُرجع صورة.',
        }}

    # Stage C — composite brand/uploaded logo (FLUX only)
    logo_source = logo_file if logo_file else brand_logo_source
    image_url, logo_composited = _composite_brand_logo(
        request, image_url=image_url, logo_source=logo_source,
        used_engine=used_engine, presentation_category=presentation_category,
        text_overlay=text_overlay, prefix=prefix,
    )

    # Stage D — optional quality gate
    quality_score = None
    if bool(getattr(settings, 'DESIGN_QUALITY_GATE_ENABLED', True)):
        try:
            from erp_core.ai.design_engine import verify_design_quality
            qr = verify_design_quality(
                image_url=image_url, raw_idea=description,
                category=presentation_category,
            )
            if qr.get('success'):
                quality_score = qr.get('score')
        except Exception as e:
            logger.warning(f'[MP PIPELINE] quality gate failed: {e}')

    return {
        'ok': True,
        'image_url': image_url,
        'used_engine': used_engine,
        'used_model': used_model,
        'presentation_category': presentation_category,
        'text_overlay': text_overlay,
        'quality_score': quality_score,
        'logo_composited': logo_composited,
        'brand_applied': (mega.get('brand_applied') or {}).get('applied', False),
        'logo_was_uploaded': logo_was_uploaded,
        'final_prompt': final_prompt,
    }
