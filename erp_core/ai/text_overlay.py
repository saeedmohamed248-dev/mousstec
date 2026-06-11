"""
🅰️ Arabic / RTL Text Overlay على الصور المولّدة بـ FLUX
=====================================================================
FLUX (وكل الـ diffusion models) مش بترسم العربي صح — بتطلع حروف garbled.
الحل: نخلي FLUX يولّد التصميم بـ "مساحة فارغة للنص" → بعدها نطبع النص العربي
يدوياً بـ PIL + arabic-reshaper + python-bidi (دقة 100%).
"""
from __future__ import annotations

import io
import logging
import os
import uuid
from typing import Optional

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

logger = logging.getLogger('mouss_tec_core')

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Font resolution — يدور على Cairo / Amiri / NotoSansArabic بالترتيب
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_FONT_SEARCH_PATHS = [
    # Amiri-Bold = best Arabic typography (classical naskh, full glyph coverage)
    os.path.join(settings.BASE_DIR, 'static', 'fonts', 'Amiri-Bold.ttf'),
    os.path.join(settings.BASE_DIR, 'static', 'fonts', 'Cairo.ttf'),
    os.path.join(settings.BASE_DIR, 'static', 'fonts', 'Cairo-Bold.ttf'),
    os.path.join(settings.BASE_DIR, 'static', 'fonts', 'NotoSansArabic-Bold.ttf'),
    # System fallbacks
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',     # Ubuntu
    '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
    '/System/Library/Fonts/Supplemental/Tahoma.ttf',            # macOS (Arabic OK)
    '/Library/Fonts/Arial.ttf',
]


def _resolve_font_path() -> Optional[str]:
    for p in _FONT_SEARCH_PATHS:
        if os.path.exists(p):
            return p
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def has_arabic(text: str) -> bool:
    """يحدد لو الـ text فيه حروف عربية."""
    if not text:
        return False
    for ch in text:
        if '؀' <= ch <= 'ۿ' or 'ݐ' <= ch <= 'ݿ' or 'ﭐ' <= ch <= '﷿':
            return True
    return False


def _pil_has_libraqm() -> bool:
    """يتحقق لو PIL مبني مع libraqm (HarfBuzz). نـ cache النتيجة على module level."""
    global _LIBRAQM_CHECKED, _LIBRAQM_AVAILABLE
    if _LIBRAQM_CHECKED:
        return _LIBRAQM_AVAILABLE
    try:
        from PIL import features
        _LIBRAQM_AVAILABLE = bool(features.check('raqm'))
    except Exception:
        _LIBRAQM_AVAILABLE = False
    _LIBRAQM_CHECKED = True
    logger.info(f'[TEXT OVERLAY] libraqm available: {_LIBRAQM_AVAILABLE}')
    return _LIBRAQM_AVAILABLE


_LIBRAQM_CHECKED = False
_LIBRAQM_AVAILABLE = False


def reshape_arabic(text: str) -> str:
    """يحوّل النص العربي للـ shaped form الصحيح.

    🎯 Critical: لو PIL مبني مع libraqm (PIL 8+ مع HarfBuzz)، الـ shaping
    والـ bidi بيتم تلقائياً على مستوى الـ rendering — فلازم نـ pass الـ
    raw text بدون أي معالجة. لو نعمل reshape + bidi ثم نسيب libraqm يعمل
    bidi تاني، النص يطلع مقلوب (double-reversal).

    Fallback path للـ PIL بدون libraqm: reshape + bidi يدوياً.
    """
    if _pil_has_libraqm():
        # libraqm handles shaping + bidi natively. Pass text untouched.
        return text
    # Legacy path: PIL بدون libraqm — لازم نـ pre-process
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)
    except ImportError:
        logger.warning('[TEXT OVERLAY] arabic-reshaper/python-bidi not installed — text will appear reversed')
        return text
    except Exception as e:
        logger.warning(f'[TEXT OVERLAY] reshape failed: {e}')
        return text


def overlay_text_on_image_url(
    image_url: str,
    text: str,
    *,
    position: str = 'center',          # 'center' | 'top' | 'bottom' | 'chest'
    color: str = '#000000',            # hex لون النص
    font_size_ratio: float = 0.08,      # نسبة من ارتفاع الصورة (0.08 = 8%)
    storage_subdir: str = 'ai_overlays',
) -> dict:
    """
    يحمّل الصورة من URL، يرسم عليها النص (مع Arabic shaping تلقائي)، ويحفظها
    على نفس الـ storage (S3 أو local) ويرجع الـ URL الجديد.

    Returns: {'success': bool, 'url': str|None, 'error': str|None}
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return {'success': False, 'error': 'pillow_missing'}

    if not text or not text.strip():
        return {'success': False, 'error': 'empty_text'}
    if not image_url:
        return {'success': False, 'error': 'no_image_url'}

    font_path = _resolve_font_path()
    if not font_path:
        logger.warning('[TEXT OVERLAY] no Arabic-capable font found — download Cairo-Bold.ttf to static/fonts/')
        return {'success': False, 'error': 'no_arabic_font'}

    # ── 1) تحميل الصورة (SSRF-safe) ──
    try:
        from erp_core.ai._safety import safe_fetch_image
        data = safe_fetch_image(image_url, timeout=30)
        if data is None:
            return {'success': False, 'error': 'image_download_blocked_or_failed'}
        img = Image.open(io.BytesIO(data)).convert('RGBA')
    except Exception as e:
        logger.exception('[TEXT OVERLAY] image load failed')
        return {'success': False, 'error': f'image_load: {e}'}

    # ── 2) Arabic shaping ──
    final_text = reshape_arabic(text.strip()) if has_arabic(text) else text.strip()

    # ── 3) رسم النص بـ font_size مناسب لحجم الصورة ──
    w, h = img.size
    font_size = max(28, int(h * font_size_ratio))

    # 🎯 لو الـ text فيه عربي ومعانا libraqm، نـ pass direction='rtl' كـ hint
    # واضح للـ shaper. لو لأ، نـ render LTR (default).
    text_is_arabic = has_arabic(final_text)
    text_kwargs = {}
    if text_is_arabic and _pil_has_libraqm():
        text_kwargs = {'direction': 'rtl', 'language': 'ar'}

    # ── 3a) Auto-fit width: قبل ما نـ render، نـ measure النص بالـ font_size
    # المقترح. لو طوله > 78% من عرض الصورة، نـ shrink الـ font_size عشان
    # يـ fit. ده بيمنع النص من إنه يطلع برة الـ garment / يتقطع من الـ
    # edges. الـ printable zone على الـ chest عادة ~65-70% من عرض الـ
    # image؛ فـ 78% حد أقصى آمن.
    _measure_draw = ImageDraw.Draw(Image.new('RGBA', (1, 1), (0, 0, 0, 0)))
    max_text_width = int(w * 0.78)
    min_font_size = max(20, int(h * 0.05))  # ما ننزلش تحت 5% — يبقى مقروء

    def _measure(font_obj):
        try:
            bbox = _measure_draw.textbbox((0, 0), final_text, font=font_obj, **text_kwargs)
            return bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            try:
                bbox = _measure_draw.textbbox((0, 0), final_text, font=font_obj)
                return bbox[2] - bbox[0], bbox[3] - bbox[1]
            except Exception:
                return font_obj.getsize(final_text)  # legacy PIL

    try:
        font = ImageFont.truetype(font_path, font_size)
    except Exception as e:
        return {'success': False, 'error': f'font_load: {e}'}

    text_w, text_h = _measure(font)
    if text_w > max_text_width and text_w > 0:
        # shrink proportionally — نضرب الـ font_size في النسبة عشان يبقى exact fit
        shrink_ratio = max_text_width / float(text_w)
        new_font_size = max(min_font_size, int(font_size * shrink_ratio * 0.97))  # 0.97 buffer
        if new_font_size < font_size:
            logger.info(
                f'[TEXT OVERLAY] shrinking font {font_size}px → {new_font_size}px '
                f'(text_w={text_w} > max={max_text_width})'
            )
            try:
                font = ImageFont.truetype(font_path, new_font_size)
                font_size = new_font_size
                text_w, text_h = _measure(font)
            except Exception:
                pass  # keep original font if reload failed

    # تحديد الـ position
    pos_map = {
        'top':    ((w - text_w) // 2, int(h * 0.08)),
        'center': ((w - text_w) // 2, (h - text_h) // 2),
        'bottom': ((w - text_w) // 2, int(h * 0.85) - text_h),
        # T-shirt UPPER chest area — تقريباً 32% من الـ top (مش 42% اللي
        # كان قريب من البطن). 32% بيقع على منطقة الصدر العلوية بين
        # الكولّر والصدر — اللي بتبان عليها الـ branding في mockups المحترفة.
        'chest':  ((w - text_w) // 2, int(h * 0.32) - text_h // 2),
        # 🔄 BACK view — منطقة بين لوحي الكتف (upper back). الـ mockup المفروض
        # يكون back-view من FLUX، فالنص يبقى في 38% من الـ top — اللي بيـ correspond
        # للـ upper-back area تحت الـ collar شوية.
        'back':   ((w - text_w) // 2, int(h * 0.38) - text_h // 2),
    }
    x, y = pos_map.get(position, pos_map['center'])
    color_rgb = _parse_hex_color(color)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 🎨 Multi-layer compositing عشان النص ينتمي للقماش (مش flat sticker):
    # 1️⃣ Soft drop shadow layer: gaussian-blurred, larger offset، أعمق depth
    # 2️⃣ Text layer: مع micro-blur على الـ edges عشان ينطبع زي screen-print
    # 3️⃣ Composition: shadow @ 38% opacity + text @ 88% opacity (يـ blend
    #     مع الـ fabric texture بدل ما يكون block صلب)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    try:
        from PIL import ImageFilter
    except ImportError:
        ImageFilter = None

    # احسب shadow offset حسب حجم الخط — أعمق وأكبر للـ headings
    shadow_offset_x = max(3, int(font_size * 0.05))
    shadow_offset_y = max(4, int(font_size * 0.07))
    shadow_blur_radius = max(2, int(font_size * 0.08))

    # Layer 1: shadow (يـ blur بعد ما يـ render)
    shadow_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    shadow_draw.text(
        (x + shadow_offset_x, y + shadow_offset_y),
        final_text, font=font, fill=(0, 0, 0, 255), **text_kwargs,
    )
    if ImageFilter:
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=shadow_blur_radius))
    # خفض opacity للـ shadow عشان يبقى soft (38%)
    shadow_alpha = shadow_layer.split()[-1].point(lambda p: int(p * 0.38))
    shadow_layer.putalpha(shadow_alpha)

    # Layer 2: النص الفعلي مع micro-blur (0.6px) عشان edges screen-print
    text_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
    text_draw = ImageDraw.Draw(text_layer)
    # color_rgb دلوقتي 4-tuple (r,g,b,a) من _parse_hex_color — نـ force alpha=255
    text_fill = (color_rgb[0], color_rgb[1], color_rgb[2], 255)
    text_draw.text((x, y), final_text, font=font, fill=text_fill, **text_kwargs)
    if ImageFilter:
        text_layer = text_layer.filter(ImageFilter.GaussianBlur(radius=0.6))
    # opacity 88% عشان الـ fabric texture يبان من تحت → realistic ink integration
    text_alpha = text_layer.split()[-1].point(lambda p: int(p * 0.88))
    text_layer.putalpha(text_alpha)

    # Compose: shadow أولاً، بعدها النص فوقيه
    combined = Image.alpha_composite(img, shadow_layer)
    combined = Image.alpha_composite(combined, text_layer).convert('RGB')

    # ── 4) حفظ على storage (S3 لو متفعّل، أو local) ──
    buf = io.BytesIO()
    combined.save(buf, format='JPEG', quality=92, optimize=True)
    buf.seek(0)
    filename = f'{storage_subdir}/{uuid.uuid4().hex}.jpg'

    try:
        saved_path = default_storage.save(filename, ContentFile(buf.getvalue()))
        final_url = default_storage.url(saved_path)
        # في حالة local، الـ URL ممكن يكون نسبي — نرجعه كده وDjango يخدم static
        return {'success': True, 'url': final_url, 'storage_path': saved_path}
    except Exception as e:
        logger.exception('[TEXT OVERLAY] storage save failed')
        return {'success': False, 'error': f'storage: {e}'}


def _parse_hex_color(hex_str: str) -> tuple:
    """يحوّل '#RRGGBB' أو '#RGB' لـ (r, g, b, a=255). Defaults لأسود لو غلط."""
    if not hex_str or not hex_str.startswith('#'):
        return (0, 0, 0, 255)
    h = hex_str.lstrip('#')
    try:
        if len(h) == 3:
            return tuple(int(c * 2, 16) for c in h) + (255,)
        if len(h) == 6:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
    except ValueError:
        pass
    return (0, 0, 0, 255)
