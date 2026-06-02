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


def reshape_arabic(text: str) -> str:
    """يحوّل النص العربي للـ shaped form الصحيح (مع البديل)."""
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

    # ── 1) تحميل الصورة ──
    try:
        resp = requests.get(image_url, timeout=30)
        if resp.status_code != 200:
            return {'success': False, 'error': f'image_download_http_{resp.status_code}'}
        img = Image.open(io.BytesIO(resp.content)).convert('RGBA')
    except Exception as e:
        logger.exception('[TEXT OVERLAY] image load failed')
        return {'success': False, 'error': f'image_load: {e}'}

    # ── 2) Arabic shaping ──
    final_text = reshape_arabic(text.strip()) if has_arabic(text) else text.strip()

    # ── 3) رسم النص بـ font_size مناسب لحجم الصورة ──
    w, h = img.size
    font_size = max(28, int(h * font_size_ratio))
    try:
        font = ImageFont.truetype(font_path, font_size)
    except Exception as e:
        return {'success': False, 'error': f'font_load: {e}'}

    # طبقة شفافة للرسم عشان نحافظ على جودة الـ image
    overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # قياس النص
    try:
        bbox = draw.textbbox((0, 0), final_text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
    except Exception:
        text_w, text_h = font.getsize(final_text)  # legacy PIL

    # تحديد الـ position
    pos_map = {
        'top':    ((w - text_w) // 2, int(h * 0.08)),
        'center': ((w - text_w) // 2, (h - text_h) // 2),
        'bottom': ((w - text_w) // 2, int(h * 0.85) - text_h),
        'chest':  ((w - text_w) // 2, int(h * 0.42)),   # T-shirt صدر
    }
    x, y = pos_map.get(position, pos_map['center'])

    # ظل خفيف للقراءة (offset 2px أسود)
    shadow_color = (0, 0, 0, 110)
    draw.text((x + 2, y + 2), final_text, font=font, fill=shadow_color)
    # النص الفعلي
    color_rgb = _parse_hex_color(color)
    draw.text((x, y), final_text, font=font, fill=color_rgb)

    # دمج
    combined = Image.alpha_composite(img, overlay).convert('RGB')

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
