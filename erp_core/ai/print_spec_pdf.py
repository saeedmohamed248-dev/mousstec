"""
📄 Print-Ready Spec Sheet PDF Generator
=====================================================================
بيـ generate A4 PDF بكل المواصفات اللي المطبعة محتاجاها لتنفيذ التصميم:
  • Visual preview (الـ image مع overlay)
  • Print-ready text block (vector, scaled لـ physical mm)
  • Specs table (font, color, dimensions, garment, print method, bleed,
    resolution, color profile, file format)
  • Customer + order info
  • Production checklist للمطبعة

Dependencies: reportlab >=4.0
Arabic rendering: arabic_reshaper + python-bidi (reportlab مفيهوش libraqm)

🔧 Critical fix (Jun 2026):
كل النصوص اللي فيها أي حرف عربي بترسم بـ Amiri/Cairo TTF — مش Helvetica.
قبل كده الـ table labels (المنتج / الخامة / ...) كانت بتطلع ████ لأنها
كانت بترسم بـ Helvetica-Bold اللي مفيهوش Arabic glyphs.
"""
from __future__ import annotations

import io
import logging
import os
import re
from datetime import datetime
from typing import Optional

from django.conf import settings

logger = logging.getLogger('mouss_tec_core')


# ── Physical dimensions + print method defaults per category ──
# الـ MVP: قيم معيارية للسوق المصري. لاحقاً ممكن نخليها configurable per tenant.
CATEGORY_SPECS = {
    'tshirt':         {'w_mm': 250, 'h_mm': 200, 'material': 'قطن 200gsm',  'name_ar': 'تيشرت',      'name_en': 'T-Shirt',       'method': 'DTF (Direct-to-Film)', 'bleed_mm': 0,  'dpi': 300},
    'hoodie':         {'w_mm': 280, 'h_mm': 220, 'material': 'قطن مخلوط',   'name_ar': 'هودي',       'name_en': 'Hoodie',        'method': 'DTF (Direct-to-Film)', 'bleed_mm': 0,  'dpi': 300},
    'business_card':  {'w_mm': 90,  'h_mm': 55,  'material': 'كوشيه 300gsm','name_ar': 'كارت بزنس',  'name_en': 'Business Card', 'method': 'Offset / Digital',     'bleed_mm': 3,  'dpi': 300},
    'flyer':          {'w_mm': 210, 'h_mm': 297, 'material': 'كوشيه 150gsm','name_ar': 'فلاير A4',  'name_en': 'A4 Flyer',      'method': 'Offset / Digital',     'bleed_mm': 3,  'dpi': 300},
    'poster':         {'w_mm': 297, 'h_mm': 420, 'material': 'كوشيه 200gsm','name_ar': 'بوستر A3', 'name_en': 'A3 Poster',     'method': 'Digital Large Format', 'bleed_mm': 3,  'dpi': 200},
    'invitation':     {'w_mm': 130, 'h_mm': 180, 'material': 'كوشيه 300gsm','name_ar': 'كارت دعوة', 'name_en': 'Invitation',    'method': 'Digital',              'bleed_mm': 3,  'dpi': 300},
    'banner':         {'w_mm': 600, 'h_mm': 1500,'material': 'فلكس 440gsm', 'name_ar': 'بنر',        'name_en': 'Banner',        'method': 'Solvent Inkjet',       'bleed_mm': 10, 'dpi': 150},
    'mug':            {'w_mm': 200, 'h_mm': 95,  'material': 'سيراميك',       'name_ar': 'ماج',        'name_en': 'Mug',           'method': 'Sublimation',          'bleed_mm': 5,  'dpi': 300},
    'sticker':        {'w_mm': 100, 'h_mm': 100, 'material': 'فينيل لاصق',   'name_ar': 'ستيكر',     'name_en': 'Sticker',       'method': 'Digital + Die-Cut',    'bleed_mm': 2,  'dpi': 300},
    'menu':           {'w_mm': 210, 'h_mm': 297, 'material': 'كوشيه 200gsm','name_ar': 'منيو',       'name_en': 'Menu',          'method': 'Offset / Digital',     'bleed_mm': 3,  'dpi': 300},
    'packaging':      {'w_mm': 200, 'h_mm': 200, 'material': 'كرتون 350gsm','name_ar': 'تغليف',      'name_en': 'Packaging',     'method': 'Offset + Die-Cut',     'bleed_mm': 5,  'dpi': 300},
    'other':          {'w_mm': 250, 'h_mm': 250, 'material': 'يحدد المطبعة','name_ar': 'منتج',       'name_en': 'Product',       'method': 'يحدد المطبعة',          'bleed_mm': 3,  'dpi': 300},
}


_ARABIC_RE = re.compile(r'[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]')


def _has_arabic(text: str) -> bool:
    return bool(text) and bool(_ARABIC_RE.search(text))


def _resolve_arabic_font_paths() -> tuple[Optional[str], Optional[str]]:
    """Returns (regular_path, bold_path). يحاول Cairo الأول، fallback لـ Amiri."""
    base = os.path.join(settings.BASE_DIR, 'static', 'fonts')
    regular_candidates = [
        os.path.join(base, 'Cairo.ttf'),
        os.path.join(base, 'NotoSansArabic-Regular.ttf'),
        os.path.join(base, 'Amiri-Regular.ttf'),
        os.path.join(base, 'Amiri-Bold.ttf'),  # last resort: bold as regular
    ]
    bold_candidates = [
        os.path.join(base, 'Cairo-Bold.ttf'),
        os.path.join(base, 'Amiri-Bold.ttf'),
        os.path.join(base, 'NotoSansArabic-Bold.ttf'),
        os.path.join(base, 'Cairo.ttf'),  # last resort: regular as bold
    ]
    reg = next((p for p in regular_candidates if os.path.exists(p)), None)
    bold = next((p for p in bold_candidates if os.path.exists(p)), None)
    return reg, bold


def _shape_arabic_for_pdf(text: str) -> str:
    """ReportLab مفيهوش libraqm — لازم نـ pre-process بـ reshape+bidi يدوياً.
    لو النص مفيهوش عربي، نرجعه زي ما هو (الـ bidi بيـ break الـ ASCII)."""
    if not text:
        return ''
    if not _has_arabic(text):
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(text))
    except Exception as e:
        logger.warning(f'[PDF] Arabic shaping failed: {e}')
        return text


def _hex_to_cmyk(hex_color: str) -> str:
    """يحوّل #RRGGBB لـ CMYK approximation للعرض في الـ spec table."""
    hex_color = (hex_color or '').lstrip('#')
    if len(hex_color) != 6:
        return 'C? M? Y? K?'
    try:
        r = int(hex_color[0:2], 16) / 255.0
        g = int(hex_color[2:4], 16) / 255.0
        b = int(hex_color[4:6], 16) / 255.0
    except ValueError:
        return 'C? M? Y? K?'
    k = 1 - max(r, g, b)
    if k >= 1:
        return 'C0 M0 Y0 K100'
    c = (1 - r - k) / (1 - k)
    m = (1 - g - k) / (1 - k)
    y = (1 - b - k) / (1 - k)
    return f'C{int(c*100)} M{int(m*100)} Y{int(y*100)} K{int(k*100)}'


def _get_category_specs(category: str) -> dict:
    cat = (category or 'other').strip().lower()
    aliases = {'tshirt': 'tshirt', 't-shirt': 'tshirt', 't_shirt': 'tshirt',
               'shirt': 'tshirt', 'apparel': 'tshirt'}
    cat = aliases.get(cat, cat)
    return CATEGORY_SPECS.get(cat, CATEGORY_SPECS['other'])


# ─────────────────────────────────────────────────────────────────
# Text drawing helper — auto-selects font by content
# ─────────────────────────────────────────────────────────────────
def _draw_text(c, x, y, text, *, size, bold, arabic_font, arabic_font_bold,
               latin_font='Helvetica', latin_font_bold='Helvetica-Bold',
               color=None, align='left'):
    """
    Smart text drawer:
      • Pure ASCII → Helvetica
      • Contains Arabic → reshape + Amiri/Cairo TTF
      • Bilingual → split on " / " and draw each half with its proper font

    Returns the total width drawn (for chaining inline text runs).
    """
    if color is not None:
        c.setFillColor(color)
    if not text:
        return 0

    # Bilingual split: "English Label / Arabic Label" pattern
    if ' / ' in text and _has_arabic(text):
        latin, arabic = text.split(' / ', 1)
        font_latin = latin_font_bold if bold else latin_font
        font_ar = arabic_font_bold if bold else arabic_font
        # Latin part
        c.setFont(font_latin, size)
        c.drawString(x, y, latin + ' / ')
        latin_w = c.stringWidth(latin + ' / ', font_latin, size)
        # Arabic part (shaped)
        c.setFont(font_ar, size)
        c.drawString(x + latin_w, y, _shape_arabic_for_pdf(arabic))
        return latin_w + c.stringWidth(_shape_arabic_for_pdf(arabic), font_ar, size)

    if _has_arabic(text):
        font = arabic_font_bold if bold else arabic_font
        shaped = _shape_arabic_for_pdf(text)
        c.setFont(font, size)
        if align == 'right':
            w = c.stringWidth(shaped, font, size)
            c.drawString(x - w, y, shaped)
            return w
        if align == 'center':
            w = c.stringWidth(shaped, font, size)
            c.drawString(x - w / 2, y, shaped)
            return w
        c.drawString(x, y, shaped)
        return c.stringWidth(shaped, font, size)

    font = latin_font_bold if bold else latin_font
    c.setFont(font, size)
    if align == 'right':
        w = c.stringWidth(text, font, size)
        c.drawString(x - w, y, text)
        return w
    if align == 'center':
        w = c.stringWidth(text, font, size)
        c.drawString(x - w / 2, y, text)
        return w
    c.drawString(x, y, text)
    return c.stringWidth(text, font, size)


def build_print_spec_pdf(
    *,
    design_code: str,
    image_url: Optional[str],
    text: str,
    text_color: str = '#000000',
    text_position: str = 'center',
    category: str = 'other',
    font_name: str = 'Cairo Bold',
    customer_name: str = '—',
    customer_phone: str = '—',
    quantity: int = 1,
    notes: str = '',
    raw_idea: str = '',
) -> bytes:
    """
    يبني print-ready PDF (A4) ويرجعها كـ bytes.

    Args:
        design_code: كود التصميم للـ tracking (مثلاً CD-...)
        image_url: URL للصورة الكاملة بعد overlay (للـ visual reference)
        text: النص العربي اللي بيتطبع
        text_color: hex color (#RRGGBB)
        text_position: chest | center | bottom | top
        category: tshirt | business_card | poster | ...
        font_name: اسم الخط
        customer_name, customer_phone, quantity, notes: optional metadata
        raw_idea: الـ brief الأصلي للـ design (للـ audit)
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.colors import HexColor, black, white, grey
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    # ── Setup Arabic fonts (regular + bold) ──
    reg_path, bold_path = _resolve_arabic_font_paths()
    arabic_font_name = 'MTArabic'
    arabic_font_bold = 'MTArabic-Bold'

    if reg_path:
        try:
            pdfmetrics.registerFont(TTFont(arabic_font_name, reg_path))
        except Exception as e:
            logger.warning(f'[PDF] Failed to register regular Arabic font: {e}')
            arabic_font_name = 'Helvetica'
    else:
        logger.warning('[PDF] No Arabic regular font found — labels will fallback to Helvetica (squares!)')
        arabic_font_name = 'Helvetica'

    if bold_path:
        try:
            pdfmetrics.registerFont(TTFont(arabic_font_bold, bold_path))
        except Exception as e:
            logger.warning(f'[PDF] Failed to register bold Arabic font: {e}')
            arabic_font_bold = arabic_font_name
    else:
        arabic_font_bold = arabic_font_name

    specs = _get_category_specs(category)
    physical_w_mm = specs['w_mm']
    physical_h_mm = specs['h_mm']
    material = specs['material']
    product_name_ar = specs['name_ar']
    product_name_en = specs['name_en']
    print_method = specs['method']
    bleed_mm = specs['bleed_mm']
    target_dpi = specs['dpi']

    # نقدّر الـ font height الفعلي بالـ mm
    if 'shirt' in category.lower() or 'apparel' in category.lower():
        est_font_ratio = 0.035
    elif category.lower() in ('poster', 'banner'):
        est_font_ratio = 0.09
    else:
        est_font_ratio = 0.07
    est_font_height_mm = round(physical_h_mm * est_font_ratio, 1)

    cmyk = _hex_to_cmyk(text_color)
    shaped_text = _shape_arabic_for_pdf(text) if text else ''

    # File-format recommendation per print method
    if print_method.lower().startswith('dtf'):
        file_format = 'PNG (transparent) — 4500×3600 px @ 300 DPI'
        color_profile = 'sRGB (DTF printer converts to CMYK internally)'
    elif print_method.lower().startswith('sublim'):
        file_format = 'PNG / TIFF — 4500×2100 px @ 300 DPI'
        color_profile = 'sRGB (sublimation uses RGB workflow)'
    elif 'solvent' in print_method.lower() or 'banner' in category.lower():
        file_format = 'PDF / EPS (vector) — أو TIFF @ 150 DPI'
        color_profile = 'CMYK — Fogra39 / U.S. Web Coated v2'
    elif 'offset' in print_method.lower():
        file_format = 'PDF/X-4 (vector + outlines) — 300 DPI rasters'
        color_profile = 'CMYK — Fogra39'
    else:
        file_format = 'PDF / PNG — 300 DPI'
        color_profile = 'CMYK preferred'

    # Pixel dimensions at target DPI for the customer's reference
    px_w = int((physical_w_mm / 25.4) * target_dpi)
    px_h = int((physical_h_mm / 25.4) * target_dpi)

    # ── PDF setup ──
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4

    # ─── Header bar ───
    c.setFillColor(HexColor('#1e1b4b'))
    c.rect(0, page_h - 30*mm, page_w, 30*mm, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont('Helvetica-Bold', 18)
    c.drawString(20*mm, page_h - 18*mm, 'MOUSS TEC — Print Spec Sheet')
    c.setFont('Helvetica', 9)
    c.drawString(20*mm, page_h - 24*mm,
                 f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}   |   Code: {design_code}')

    y = page_h - 40*mm

    # ─── 1. Visual mockup section ───
    c.setFillColor(black)
    _draw_text(c, 20*mm, y, '1. Visual Mockup / المعاينة البصرية',
               size=11, bold=True,
               arabic_font=arabic_font_name, arabic_font_bold=arabic_font_bold)
    y -= 4*mm
    _draw_text(c, 20*mm, y, '(للمراجعة فقط — مش للطباعة المباشرة)',
               size=8, bold=False, color=grey,
               arabic_font=arabic_font_name, arabic_font_bold=arabic_font_bold)
    y -= 4*mm

    if image_url:
        try:
            from erp_core.ai._safety import safe_fetch_image
            data = safe_fetch_image(image_url, timeout=20)
            if data is not None:
                from reportlab.lib.utils import ImageReader
                img_reader = ImageReader(io.BytesIO(data))
                iw, ih = img_reader.getSize()
                aspect = iw / ih if ih else 1.0
                target_h = 75 * mm
                target_w = target_h * aspect
                max_w = 100 * mm
                if target_w > max_w:
                    target_w = max_w
                    target_h = target_w / aspect
                c.drawImage(
                    img_reader, 20*mm, y - target_h, width=target_w, height=target_h,
                    preserveAspectRatio=True, mask='auto',
                )
                y -= (target_h + 8*mm)
            else:
                c.setFont('Helvetica-Oblique', 9)
                c.setFillColor(grey)
                c.drawString(22*mm, y - 8*mm, '(image unavailable)')
                y -= 14*mm
        except Exception as e:
            logger.warning(f'[PDF] image embed failed: {e}')
            c.setFont('Helvetica-Oblique', 9)
            c.drawString(22*mm, y - 8*mm, '(image embed failed)')
            y -= 14*mm
    else:
        c.setFont('Helvetica-Oblique', 9)
        c.setFillColor(grey)
        c.drawString(22*mm, y - 8*mm, '(no image provided)')
        y -= 14*mm

    # ─── 2. Specs table ───
    c.setFillColor(black)
    _draw_text(c, 20*mm, y, '2. Print Specifications / مواصفات الطباعة',
               size=11, bold=True,
               arabic_font=arabic_font_name, arabic_font_bold=arabic_font_bold)
    y -= 6*mm

    rows = [
        ('Product / المنتج',          f'{product_name_en} ({product_name_ar})'),
        ('Material / الخامة',          material),
        ('Print Method / طريقة الطباعة', print_method),
        ('Position / مكان الطباعة',    text_position.upper()),
        ('Text / النص',                shaped_text or '—'),
        ('Font / الخط',                font_name),
        ('Font Size / حجم الخط',       f'{est_font_height_mm} mm  (~{int(est_font_height_mm * 2.83):d}pt)'),
        ('Color / اللون',              f'{text_color}   →   {cmyk}'),
        ('Color Profile / ملف الألوان', color_profile),
        ('Print Area / مساحة الطباعة', f'{physical_w_mm} × {physical_h_mm} mm'),
        ('Resolution / الدقة',         f'{target_dpi} DPI  →  {px_w} × {px_h} px'),
        ('Bleed / الـ Bleed',          f'{bleed_mm} mm كل ضلع' if bleed_mm else 'لا يوجد (طباعة على قماش)'),
        ('File Format / صيغة الملف',   file_format),
        ('Quantity / الكمية',          str(quantity)),
    ]

    table_x = 20*mm
    table_w = page_w - 40*mm
    row_h = 6.5*mm
    label_w = 62*mm  # wider for bilingual labels
    c.setStrokeColor(HexColor('#cbd5e1'))
    c.setLineWidth(0.4)

    for i, (label, value) in enumerate(rows):
        # zebra stripes
        if i % 2 == 0:
            c.setFillColor(HexColor('#f8fafc'))
            c.rect(table_x, y - row_h, table_w, row_h, fill=1, stroke=0)
        c.setStrokeColor(HexColor('#cbd5e1'))
        c.rect(table_x, y - row_h, table_w, row_h, fill=0, stroke=1)
        c.line(table_x + label_w, y - row_h, table_x + label_w, y)

        # Label (bilingual)
        _draw_text(c, table_x + 2*mm, y - 4.5*mm, label,
                   size=8.5, bold=True, color=HexColor('#334155'),
                   arabic_font=arabic_font_name, arabic_font_bold=arabic_font_bold)

        # Value
        _draw_text(c, table_x + label_w + 2*mm, y - 4.5*mm, str(value),
                   size=9, bold=False, color=black,
                   arabic_font=arabic_font_name, arabic_font_bold=arabic_font_bold)
        y -= row_h

    y -= 6*mm

    # ─── 3. Print-ready vector text block ───
    c.setFillColor(black)
    _draw_text(c, 20*mm, y, '3. Print-Ready Text Block (vector — مقاس 1:1)',
               size=11, bold=True,
               arabic_font=arabic_font_name, arabic_font_bold=arabic_font_bold)
    y -= 5*mm
    _draw_text(c, 20*mm, y,
               'This block reproduces the text at the intended physical print size. Use as direct artwork.',
               size=8, bold=False, color=grey,
               arabic_font=arabic_font_name, arabic_font_bold=arabic_font_bold)
    y -= 8*mm

    block_h = 35*mm
    c.setStrokeColor(HexColor('#a78bfa'))
    c.setLineWidth(1)
    c.setDash(3, 3)
    c.rect(20*mm, y - block_h, page_w - 40*mm, block_h, fill=0, stroke=1)
    c.setDash()

    if shaped_text:
        font_pt = max(14, int(est_font_height_mm * 2.83))
        c.setFillColor(HexColor(text_color))
        c.setFont(arabic_font_bold, font_pt)
        try:
            text_w = c.stringWidth(shaped_text, arabic_font_bold, font_pt)
        except Exception:
            text_w = (page_w - 40*mm) * 0.6
        text_x = (page_w - text_w) / 2
        c.drawString(text_x, y - block_h/2 - font_pt/3, shaped_text)
    else:
        c.setFont('Helvetica-Oblique', 10)
        c.setFillColor(grey)
        c.drawCentredString(page_w/2, y - block_h/2, '(no text — pure design)')

    y -= (block_h + 8*mm)

    # ─── 4. Production Checklist (new!) ───
    c.setFillColor(black)
    _draw_text(c, 20*mm, y, '4. Production Checklist / قائمة فحص الإنتاج',
               size=11, bold=True,
               arabic_font=arabic_font_name, arabic_font_bold=arabic_font_bold)
    y -= 6*mm

    checklist = [
        '☐ تأكد إن الصورة بدقة 300 DPI على الأقل قبل التصدير',
        '☐ راجع الـ bleed: ' + (f'{bleed_mm} mm كل ضلع' if bleed_mm else 'مش مطلوب على هذه الخامة'),
        '☐ حوّل النصوص لـ outlines/curves قبل التصدير من Illustrator/PS',
        '☐ تحقق من الـ color profile: ' + color_profile,
        '☐ اطبع proof واحد (نموذج) قبل الإنتاج الكامل لو الكمية > 50',
        '☐ راجع موضع النص (' + text_position.upper() + ') على الـ mockup الفعلي',
    ]
    for item in checklist:
        _draw_text(c, 22*mm, y, item,
                   size=8.5, bold=False, color=HexColor('#475569'),
                   arabic_font=arabic_font_name, arabic_font_bold=arabic_font_bold)
        y -= 4.5*mm

    y -= 4*mm

    # ─── 5. Customer / Order info ───
    c.setFillColor(black)
    _draw_text(c, 20*mm, y, '5. Customer & Order / العميل والطلب',
               size=11, bold=True,
               arabic_font=arabic_font_name, arabic_font_bold=arabic_font_bold)
    y -= 6*mm

    customer_rows = [
        ('Customer / العميل',   customer_name),
        ('Phone / الهاتف',      customer_phone),
        ('Quantity / الكمية',   str(quantity)),
        ('Notes / ملاحظات',     notes[:160] if notes else '—'),
    ]
    for i, (label, value) in enumerate(customer_rows):
        if i % 2 == 0:
            c.setFillColor(HexColor('#f8fafc'))
            c.rect(table_x, y - row_h, table_w, row_h, fill=1, stroke=0)
        c.setStrokeColor(HexColor('#cbd5e1'))
        c.rect(table_x, y - row_h, table_w, row_h, fill=0, stroke=1)
        c.line(table_x + label_w, y - row_h, table_x + label_w, y)
        _draw_text(c, table_x + 2*mm, y - 4.5*mm, label,
                   size=8.5, bold=True, color=HexColor('#334155'),
                   arabic_font=arabic_font_name, arabic_font_bold=arabic_font_bold)
        _draw_text(c, table_x + label_w + 2*mm, y - 4.5*mm, str(value),
                   size=9, bold=False, color=black,
                   arabic_font=arabic_font_name, arabic_font_bold=arabic_font_bold)
        y -= row_h

    # ─── Footer ───
    c.setFillColor(grey)
    c.setFont('Helvetica', 8)
    footer = 'Mouss Tec ERP  |  mousstec.com  |  This spec sheet is generated automatically from the AI design pipeline.'
    c.drawCentredString(page_w / 2, 12*mm, footer)
    if raw_idea:
        idea_short = (raw_idea[:90] + '…') if len(raw_idea) > 90 else raw_idea
        _draw_text(c, page_w / 2, 7*mm, f'Brief: {idea_short}',
                   size=7, bold=False, color=grey, align='center',
                   arabic_font=arabic_font_name, arabic_font_bold=arabic_font_bold)

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()
