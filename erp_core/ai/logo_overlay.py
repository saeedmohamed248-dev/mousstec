"""
🎨 Brand Logo Compositing على الصور المولّدة بـ FLUX
=====================================================================
FLUX (والديفيوجن models عموماً) مبيقدرش يرسم لوجو معين بدقة — حتى مع
image-to-image conditioning النتيجة غالباً تكون "logo-ish" مش الـ logo
الفعلي للعميل.

الحل: نخلي FLUX يولّد المنتج (تيشرت/مج/سنيكر) ويحجز مساحة فاضية للوجو
(الـ LLM بيـ instruct ده عبر الـ "BRAND LOGO RESERVED" hint في
apply_brand_profile)، بعدها نـ paste اللوجو الحقيقي بـ PIL على
الـ category-specific placement zone.

Public API:
    composite_logo_on_image_url(image_url, logo_file_or_url, category, ...) -> dict
"""
from __future__ import annotations

import io
import logging
import os
import uuid
from typing import Any, Optional, Union

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

logger = logging.getLogger('mouss_tec_core')


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Placement registry — per presentation_category, where the logo lives.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Tuple = (position_key, width_ratio)
#   position_key drives the (x, y) anchor in _POSITION_ANCHORS below
#   width_ratio  = logo width as fraction of canvas width (height auto-scales)
# Categories absent from this map fall back to _DEFAULT_PLACEMENT.
# 'logo' category is INTENTIONALLY excluded — never composite a brand logo
# onto a design that IS a logo (it'd defeat the design's purpose).
_CATEGORY_PLACEMENT: dict[str, tuple[str, float]] = {
    'apparel':      ('chest_left',    0.10),   # left chest, screen-print zone
    'footwear':     ('side_panel',    0.10),   # outer side panel
    'document':     ('top_right',     0.10),   # letterhead / invoice corner
    'signage':      ('bottom_right',  0.09),   # poster / banner footer
    'packaging':    ('center',        0.14),   # box face center
    'social_post':  ('bottom_right',  0.10),
    'vehicle':      ('side_door',     0.11),
    'interior':     ('bottom_right',  0.08),
    'furniture':    ('bottom_right',  0.07),
    'electronics':  ('bottom_right',  0.08),
    'appliance':    ('bottom_right',  0.08),
    'food':         ('bottom_right',  0.09),
    'jewelry':      ('bottom_right',  0.07),
    'cosmetics':    ('center',        0.12),   # product label
    'accessory':    ('bottom_right',  0.09),
}
_DEFAULT_PLACEMENT: tuple[str, float] = ('bottom_right', 0.09)

# Categories where we MUST NOT composite (would corrupt the design intent).
_SKIP_CATEGORIES: frozenset[str] = frozenset({'logo', 'character', 'illustration'})


def _position_anchor(
    pos_key: str,
    canvas_w: int,
    canvas_h: int,
    logo_w: int,
    logo_h: int,
) -> tuple[int, int]:
    """يحوّل الـ position_key لـ (x, y) coords مع safe margin من الحواف.
    Margin = 4% من الـ canvas — يبعد اللوجو عن edges عشان ميتقطعش في الـ print."""
    mx = int(canvas_w * 0.04)
    my = int(canvas_h * 0.04)

    anchors = {
        'top_left':       (mx,                          my),
        'top_right':      (canvas_w - logo_w - mx,      my),
        'bottom_left':    (mx,                          canvas_h - logo_h - my),
        'bottom_right':   (canvas_w - logo_w - mx,      canvas_h - logo_h - my),
        'center':         ((canvas_w - logo_w) // 2,    (canvas_h - logo_h) // 2),
        # Apparel: left chest, ~22% from top, slightly right of center-line
        'chest_left':     (int(canvas_w * 0.30),        int(canvas_h * 0.22)),
        # Footwear: side panel area (mid-height, ~35% from left)
        'side_panel':     (int(canvas_w * 0.35),        int(canvas_h * 0.55)),
        # Vehicle: side door area
        'side_door':      (int(canvas_w * 0.42),        int(canvas_h * 0.52)),
    }
    return anchors.get(pos_key, anchors['bottom_right'])


def _sniff_svg(data: bytes, hint_name: str = '') -> bool:
    """Detect SVG content. Checks extension hint + magic bytes (handles
    UTF-8 BOM, leading whitespace, XML declarations)."""
    if hint_name and hint_name.lower().endswith('.svg'):
        return True
    if not data:
        return False
    # Strip BOM + whitespace, then peek at the first 256 bytes
    head = data[:256].lstrip(b'\xef\xbb\xbf').lstrip()
    return head.startswith(b'<svg') or (head.startswith(b'<?xml') and b'<svg' in data[:1024])


# Rasterization width for SVG → PNG. Generous so downscale to any reasonable
# composite size (typical: 80-150px at 1024px canvas) stays sharp.
_SVG_RASTER_WIDTH = 1024


def _svg_to_png_bytes(svg_data: bytes) -> Optional[bytes]:
    """يـ rasterize SVG bytes إلى PNG bytes عبر CairoSVG.

    Returns None لو cairosvg مش مثبت أو الـ SVG عاطل. الـ caller المفروض
    يـ fallback gracefully (الـ logo بيتـ skip، الـ design بيخرج بدون لوجو).
    """
    try:
        import cairosvg  # type: ignore
    except ImportError:
        logger.warning(
            '[LOGO COMPOSITE] cairosvg not installed — SVG logos cannot '
            'be composited. Install with: pip install cairosvg'
        )
        return None
    try:
        return cairosvg.svg2png(
            bytestring=svg_data,
            output_width=_SVG_RASTER_WIDTH,
        )
    except Exception as e:
        logger.warning(f'[LOGO COMPOSITE] SVG rasterization failed: {e}')
        return None


def _bytes_to_pil(data: bytes, hint_name: str = '') -> Optional[Any]:
    """Bytes → PIL.Image RGBA. Auto-detects SVG and rasterizes via cairosvg."""
    try:
        from PIL import Image
    except ImportError:
        return None
    if not data:
        return None

    if _sniff_svg(data, hint_name):
        png_bytes = _svg_to_png_bytes(data)
        if png_bytes is None:
            return None
        data = png_bytes

    try:
        return Image.open(io.BytesIO(data)).convert('RGBA')
    except Exception as e:
        logger.warning(f'[LOGO COMPOSITE] PIL open failed: {e}')
        return None


def _load_logo_pil(logo_source: Union[str, Any]) -> Optional[Any]:
    """يقبل URL أو Django FileField — يرجع PIL.Image في RGBA.

    Handles:
      • http(s) URL → requests.get (SVG auto-detected & rasterized)
      • relative storage URL (e.g. /media/...) → default_storage.open
      • Django ImageField / FieldFile → .open() + read()
      • SVG content in any of the above → CairoSVG → PNG → PIL
    """
    # Case 1: ImageField / FieldFile
    if hasattr(logo_source, 'open') and hasattr(logo_source, 'read'):
        try:
            logo_source.open('rb')
            data = logo_source.read()
            logo_source.close()
            hint = getattr(logo_source, 'name', '') or ''
            return _bytes_to_pil(data, hint_name=hint)
        except Exception as e:
            logger.warning(f'[LOGO COMPOSITE] FieldFile load failed: {e}')
            return None

    # Case 2: string URL or storage path
    if isinstance(logo_source, str):
        s = logo_source.strip()
        if not s:
            return None
        try:
            if s.startswith(('http://', 'https://')):
                from erp_core.ai._safety import safe_fetch_image
                # tenant logos may live on the project's own MEDIA host, allow it too
                extra = set()
                try:
                    from urllib.parse import urlparse as _up
                    site = getattr(settings, 'SITE_DOMAIN', '') or getattr(settings, 'PUBLIC_HOST', '')
                    if site:
                        extra.add(_up(site).hostname or site)
                except Exception:
                    pass
                data = safe_fetch_image(s, timeout=20, extra_allowed_hosts=extra or None)
                if data is None:
                    return None
                return _bytes_to_pil(data, hint_name=s)
            # Relative storage URL — try default_storage. Strip /media/ prefix
            # if present (default_storage paths are relative to MEDIA_ROOT).
            media_url = getattr(settings, 'MEDIA_URL', '/media/')
            if s.startswith(media_url):
                s = s[len(media_url):]
            with default_storage.open(s, 'rb') as fh:
                return _bytes_to_pil(fh.read(), hint_name=s)
        except Exception as e:
            logger.warning(f'[LOGO COMPOSITE] logo source load failed ({s[:60]}): {e}')
            return None

    return None


def _avoids_text_overlay(
    logo_box: tuple[int, int, int, int],
    text_overlay_position: Optional[str],
    canvas_w: int,
    canvas_h: int,
) -> bool:
    """يـ check لو الـ logo placement بيـ overlap مع text overlay zone.
    Returns True لو آمن (no overlap)."""
    if not text_overlay_position:
        return True
    # Approximate text zones (matches text_overlay.py's pos_map)
    text_zones = {
        'chest':  (0, int(canvas_h * 0.28),  canvas_w, int(canvas_h * 0.46)),
        'back':   (0, int(canvas_h * 0.34),  canvas_w, int(canvas_h * 0.52)),
        'center': (0, int(canvas_h * 0.42),  canvas_w, int(canvas_h * 0.58)),
        'top':    (0, int(canvas_h * 0.04),  canvas_w, int(canvas_h * 0.18)),
        'bottom': (0, int(canvas_h * 0.78),  canvas_w, canvas_h),
    }
    tz = text_zones.get(text_overlay_position)
    if not tz:
        return True
    lx1, ly1, lx2, ly2 = logo_box
    tx1, ty1, tx2, ty2 = tz
    # Rectangle intersection test
    return not (lx1 < tx2 and lx2 > tx1 and ly1 < ty2 and ly2 > ty1)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def composite_logo_on_image_url(
    image_url: str,
    logo_source: Union[str, Any],
    category: str,
    *,
    text_overlay_position: Optional[str] = None,
    width_ratio_override: Optional[float] = None,
    position_override: Optional[str] = None,
    storage_subdir: str = 'ai_brand_logos',
    opacity: float = 0.96,
) -> dict:
    """يحمّل الصورة المولّدة، يـ paste البراند لوجو حسب الـ category، ويحفظها.

    Args:
        image_url:              رابط الصورة الناتجة من FLUX/Ideogram.
        logo_source:            URL أو Django ImageField/FieldFile للوجو.
        category:               presentation_category (apparel/footwear/...) —
                                بتـ control الـ placement zone والـ size.
        text_overlay_position:  لو فيه text overlay مطبّق، نـ avoid الـ zone بتاعه.
        width_ratio_override:   override للـ logo size (0.0 - 0.3).
        position_override:      override للـ position_key (e.g. 'top_left').
        storage_subdir:         folder تحت MEDIA_ROOT للحفظ.
        opacity:                0.0 - 1.0 — اللوجو ينطبع طبيعي مش flat sticker.

    Returns:
        {'success': True, 'url': str, 'storage_path': str,
         'placement': str, 'width_ratio': float, 'avoided_text': bool}
        أو {'success': False, 'error': str, 'skipped': bool?}
    """
    try:
        from PIL import Image, ImageFilter
    except ImportError:
        return {'success': False, 'error': 'pillow_missing'}

    if not image_url:
        return {'success': False, 'error': 'no_image_url'}
    if logo_source is None or (isinstance(logo_source, str) and not logo_source.strip()):
        return {'success': False, 'error': 'no_logo_source'}

    cat = (category or '').strip().lower()
    if cat in _SKIP_CATEGORIES:
        logger.info(f'[LOGO COMPOSITE] skipped — category={cat} not eligible')
        return {'success': False, 'error': 'category_excluded', 'skipped': True}

    pos_key, width_ratio = _CATEGORY_PLACEMENT.get(cat, _DEFAULT_PLACEMENT)
    if position_override:
        pos_key = position_override
    if width_ratio_override is not None:
        width_ratio = max(0.03, min(0.30, float(width_ratio_override)))

    # ── 1) Load canvas (SSRF-safe) ──────────────────────────────
    try:
        from erp_core.ai._safety import safe_fetch_image
        data = safe_fetch_image(image_url, timeout=30)
        if data is None:
            return {'success': False, 'error': 'canvas_blocked_or_failed'}
        canvas = Image.open(io.BytesIO(data)).convert('RGBA')
    except Exception as e:
        logger.exception('[LOGO COMPOSITE] canvas load failed')
        return {'success': False, 'error': f'canvas_load: {e}'}

    # ── 2) Load logo ────────────────────────────────────────────
    logo = _load_logo_pil(logo_source)
    if logo is None:
        return {'success': False, 'error': 'logo_load_failed'}

    # ── 3) Resize logo preserving aspect ratio ─────────────────
    canvas_w, canvas_h = canvas.size
    target_w = max(40, int(canvas_w * width_ratio))
    aspect = logo.height / logo.width if logo.width else 1.0
    target_h = max(20, int(target_w * aspect))
    # Cap logo height at 25% of canvas (tall thin logos shouldn't dominate)
    if target_h > int(canvas_h * 0.25):
        target_h = int(canvas_h * 0.25)
        target_w = int(target_h / aspect) if aspect else target_w
    try:
        logo = logo.resize((target_w, target_h), Image.LANCZOS)
    except Exception as e:
        return {'success': False, 'error': f'logo_resize: {e}'}

    # ── 4) Pick anchor + avoid text zone ────────────────────────
    x, y = _position_anchor(pos_key, canvas_w, canvas_h, target_w, target_h)
    logo_box = (x, y, x + target_w, y + target_h)
    avoided = True
    if not _avoids_text_overlay(logo_box, text_overlay_position, canvas_w, canvas_h):
        # Fallback to opposite corner from text
        fallback = 'top_right' if text_overlay_position in ('chest', 'center', 'bottom') else 'bottom_right'
        x, y = _position_anchor(fallback, canvas_w, canvas_h, target_w, target_h)
        logo_box = (x, y, x + target_w, y + target_h)
        avoided = False
        logger.info(
            f'[LOGO COMPOSITE] text_overlay at {text_overlay_position} — '
            f'rerouted logo to {fallback}'
        )

    # ── 5) Composite with soft shadow + opacity ────────────────
    # Soft shadow gives the logo physical depth on fabric/surface.
    try:
        shadow = Image.new('RGBA', canvas.size, (0, 0, 0, 0))
        # Render logo silhouette as shadow
        shadow_logo = Image.new('RGBA', (target_w, target_h), (0, 0, 0, 0))
        # Use logo's alpha as the shadow shape
        if logo.mode == 'RGBA':
            alpha = logo.split()[-1]
            black = Image.new('RGBA', (target_w, target_h), (0, 0, 0, 130))
            shadow_logo.paste(black, (0, 0), alpha)
        shadow.paste(shadow_logo, (x + max(2, int(target_w * 0.02)),
                                    y + max(2, int(target_h * 0.03))), shadow_logo)
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=max(2, int(target_w * 0.03))))

        # Apply opacity to logo itself
        if 0.0 < opacity < 1.0 and logo.mode == 'RGBA':
            logo_alpha = logo.split()[-1].point(lambda p: int(p * opacity))
            logo.putalpha(logo_alpha)

        composed = Image.alpha_composite(canvas, shadow)
        # Use logo's own alpha as the paste mask
        composed.paste(logo, (x, y), logo if logo.mode == 'RGBA' else None)
        composed = composed.convert('RGB')
    except Exception as e:
        logger.exception('[LOGO COMPOSITE] composite step failed')
        return {'success': False, 'error': f'composite: {e}'}

    # ── 6) Save to storage ─────────────────────────────────────
    buf = io.BytesIO()
    composed.save(buf, format='JPEG', quality=92, optimize=True)
    buf.seek(0)
    filename = f'{storage_subdir}/{uuid.uuid4().hex}.jpg'
    try:
        saved_path = default_storage.save(filename, ContentFile(buf.getvalue()))
        final_url = default_storage.url(saved_path)
        logger.info(
            f'[LOGO COMPOSITE] category={cat} placement={pos_key} '
            f'size={target_w}x{target_h} avoided_text={avoided} → {final_url}'
        )
        return {
            'success': True,
            'url': final_url,
            'storage_path': saved_path,
            'placement': pos_key,
            'width_ratio': width_ratio,
            'avoided_text': avoided,
        }
    except Exception as e:
        logger.exception('[LOGO COMPOSITE] storage save failed')
        return {'success': False, 'error': f'storage: {e}'}
