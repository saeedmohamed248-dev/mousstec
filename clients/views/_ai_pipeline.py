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
- :func:`_resolve_quality_size`
- :func:`_upscale_local_image`
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


# FLUX/Ideogram cap each side at 2048. Anything beyond that requires a
# post-generation upscale step (PIL Lanczos).
_FLUX_MAX_SIDE = 2048


def _resolve_quality_size(canonical_size, package):
    """Pick the generation size + final upscale target based on the package.

    ``package.resolution_max`` ('2048x2048', '4096x4096', ...) is the contract
    the customer paid for. We scale ``canonical_size`` so its longest side
    reaches that target, but the FLUX call is clamped at 2048 — anything
    above is reached via :func:`_upscale_local_image` after persist.

    Returns ``(gen_size:str, target_max_dim:int)``. Falls back to 1024 on any
    parse failure so we never break the generation flow over a quality hint.
    """
    target_max_dim = 1024
    if package is not None:
        try:
            res = (getattr(package, 'resolution_max', '') or '').lower()
            target_max_dim = max(int(x) for x in res.split('x') if x.strip().isdigit())
        except (ValueError, AttributeError):
            target_max_dim = 1024
    try:
        w, h = (int(x) for x in str(canonical_size).lower().split('x'))
    except (ValueError, AttributeError):
        w, h = 1024, 1024
    max_side = max(w, h, 1)
    gen_target = min(target_max_dim, _FLUX_MAX_SIDE)
    if gen_target <= max_side:
        return f'{w}x{h}', target_max_dim
    scale = gen_target / max_side
    gw = min(max(int(round(w * scale)), 256), _FLUX_MAX_SIDE)
    gh = min(max(int(round(h * scale)), 256), _FLUX_MAX_SIDE)
    return f'{gw}x{gh}', target_max_dim


def _upscale_local_image(request, customer, image_url, target_max_dim, *, prefix='ai_store'):
    """Lanczos-upscale a persisted image so its longest side ≥ target_max_dim.

    Only runs when the target exceeds what FLUX produced (typically 4096 for
    'ultra' packs). Returns the new public URL on success, the original URL
    on no-op or failure (non-fatal — never blocks the response).
    """
    if not image_url or target_max_dim <= _FLUX_MAX_SIDE:
        return image_url
    try:
        import io
        import uuid as _uuid
        from PIL import Image as PILImage
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        img_bytes = None
        for media_prefix in ('/media/', 'media/'):
            if media_prefix in image_url:
                rel_path = image_url.split(media_prefix, 1)[-1]
                try:
                    if default_storage.exists(rel_path):
                        with default_storage.open(rel_path, 'rb') as fp:
                            img_bytes = fp.read()
                except Exception:
                    pass
                break
        if img_bytes is None:
            try:
                import requests as _req
                r = _req.get(image_url, timeout=30)
                if r.status_code == 200:
                    img_bytes = r.content
            except Exception:
                return image_url
        if not img_bytes:
            return image_url

        img = PILImage.open(io.BytesIO(img_bytes))
        w, h = img.size
        cur_max = max(w, h, 1)
        if cur_max >= target_max_dim:
            return image_url
        scale = target_max_dim / cur_max
        new_size = (max(int(round(w * scale)), 1), max(int(round(h * scale)), 1))
        upscaled = img.resize(new_size, PILImage.LANCZOS)
        buf = io.BytesIO()
        upscaled.save(buf, format='PNG')
        cust_uid = getattr(customer, 'uid', None) or 'anon'
        filename = f'{prefix}/{cust_uid}/{_uuid.uuid4().hex}_hi.png'
        saved = default_storage.save(filename, ContentFile(buf.getvalue()))
        new_url = default_storage.url(saved)
        if new_url.startswith('/'):
            new_url = request.build_absolute_uri(new_url)
        return new_url
    except Exception as e:
        logger.warning(f'[MP PIPELINE] upscale failed: {e}')
        return image_url


def _run_marketplace_image_pipeline(
    request, customer, *,
    engineered_prompt,
    description,
    category,
    canonical_size,
    package=None,
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
    gen_size, target_max_dim = _resolve_quality_size(canonical_size, package)
    quality_tier = (getattr(package, 'quality_level', None) or 'hd') if package else 'hd'
    try:
        from erp_core.ai.printing_copilot import generate_design_image
        img = generate_design_image(
            prompt=final_prompt[:1800],
            size=gen_size,
            negative_prompt=final_negative or (
                'low quality, blurry, watermark, distorted text, fake logo, '
                'duplicated elements, extra fingers, jpeg artifacts'
            ),
            category=presentation_category,
            has_text_content=has_text,
            block_schnell_fallback=block_schnell_fallback,
            quality_tier=quality_tier,
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

    # Stage B.5 — upscale to package's resolution_max if it exceeds FLUX's cap
    image_url = _upscale_local_image(
        request, customer, image_url, target_max_dim, prefix=prefix,
    )

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
