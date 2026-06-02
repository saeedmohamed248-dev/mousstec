"""
📄 Print-Ready Spec Sheet PDF Generator
=====================================================================
بيـ generate A4 PDF بكل المواصفات اللي المطبعة محتاجاها لتنفيذ التصميم:
  • Visual preview (الـ image مع overlay)
  • Print-ready text block (vector, scaled لـ physical mm)
  • Specs table (font, color, dimensions, garment)
  • Customer + order info

Dependencies: reportlab >=4.0
Arabic rendering: arabic_reshaper + python-bidi (reportlab مفيهوش libraqm)
"""
from __future__ import annotations

import io
import logging
import os
from datetime import datetime
from typing import Optional

import requests
from django.conf import settings

logger = logging.getLogger('mouss_tec_core')


# ── Physical dimensions per category (defaults للـ print area) ──
# الـ MVP: قيم معيارية للسوق المصري. لاحقاً ممكن نخليها configurable per tenant.
CATEGORY_SPECS = {
    'tshirt':         {'w_mm': 250, 'h_mm': 200, 'material': 'قطن 200gsm',  'name_ar': 'تيشرت',      'name_en': 'T-Shirt'},
    'hoodie':         {'w_mm': 280, 'h_mm': 220, 'material': 'قطن مخلوط',   'name_ar': 'هودي',       'name_en': 'Hoodie'},
    'business_card':  {'w_mm': 90,  'h_mm': 55,  'material': 'كوشيه 300gsm','name_ar': 'كارت بزنس',  'name_en': 'Business Card'},
    'flyer':          {'w_mm': 210, 'h_mm': 297, 'material': 'كوشيه 150gsm','name_ar': 'فلاير A4',  'name_en': 'A4 Flyer'},
    'poster':         {'w_mm': 297, 'h_mm': 420, 'material': 'كوشيه 200gsm','name_ar': 'بوستر A3', 'name_en': 'A3 Poster'},
    'invitation':     {'w_mm': 130, 'h_mm': 180, 'material': 'كوشيه 300gsm','name_ar': 'كارت دعوة', 'name_en': 'Invitation'},
    'banner':         {'w_mm': 600, 'h_mm': 1500,'material': 'فلكس',         'name_ar': 'بنر',        'name_en': 'Banner'},
    'mug':            {'w_mm': 200, 'h_mm': 95,  'material': 'سيراميك',       'name_ar': 'ماج',        'name_en': 'Mug'},
    'sticker':        {'w_mm': 100, 'h_mm': 100, 'material': 'فينيل لاصق',   'name_ar': 'ستيكر',     'name_en': 'Sticker'},
    'menu':           {'w_mm': 210, 'h_mm': 297, 'material': 'كوشيه 200gsm','name_ar': 'منيو',       'name_en': 'Menu'},
    'packaging':      {'w_mm': 200, 'h_mm': 200, 'material': 'كرتون 350gsm','name_ar': 'تغليف',      'name_en': 'Packaging'},
    'other':          {'w_mm': 250, 'h_mm': 250, 'material': 'يحدد المطبعة','name_ar': 'منتج',       'name_en': 'Product'},
}


def _resolve_arabic_font_path() -> Optional[str]:
    """يلاقي خط Arabic-capable في الـ static/fonts/. نفس logic الـ text_overlay."""
    candidates = [
        os.path.join(settings.BASE_DIR, 'static', 'fonts', 'Cairo-Bold.ttf'),
        os.path.join(settings.BASE_DIR, 'static', 'fonts', 'Cairo.ttf'),
        os.path.join(settings.BASE_DIR, 'static', 'fonts', 'Amiri-Bold.ttf'),
        os.path.join(settings.BASE_DIR, 'static', 'fonts', 'NotoSansArabic-Bold.ttf'),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _shape_arabic_for_pdf(text: str) -> str:
    """ReportLab مفيهوش libraqm — لازم نـ pre-process بـ reshape+bidi يدوياً.
    (مختلف عن text_overlay اللي بيـ skip ده على PIL+libraqm)."""
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
    # Mapping أسماء بديلة
    aliases = {'tshirt': 'tshirt', 't-shirt': 'tshirt', 't_shirt': 'tshirt',
               'shirt': 'tshirt', 'apparel': 'tshirt'}
    cat = aliases.get(cat, cat)
    return CATEGORY_SPECS.get(cat, CATEGORY_SPECS['other'])


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
    from reportlab.lib.units import mm, cm
    from reportlab.lib.colors import HexColor, black, white, grey
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    # ── Setup fonts ──
    font_path = _resolve_arabic_font_path()
    arabic_font_name = 'ArabicFont'
    if font_path:
        try:
            pdfmetrics.registerFont(TTFont(arabic_font_name, font_path))
        except Exception as e:
            logger.warning(f'[PDF] Failed to register font: {e}')
            arabic_font_name = 'Helvetica'  # fallback
    else:
        logger.warning('[PDF] No Arabic font found — text will appear in fallback')
        arabic_font_name = 'Helvetica'

    specs = _get_category_specs(category)
    physical_w_mm = specs['w_mm']
    physical_h_mm = specs['h_mm']
    material = specs['material']
    product_name_ar = specs['name_ar']
    product_name_en = specs['name_en']

    # نقدّر الـ font height الفعلي بالـ mm
    # ratio أساسي: لو image height = physical_h_mm فيزيائياً، والـ overlay كان
    # بـ font_size_ratio من الـ image height → نضربها في physical_h_mm
    # default 0.035 للملابس مظبوط (~ 7mm على 200mm chest)
    if 'shirt' in category.lower() or 'apparel' in category.lower():
        est_font_ratio = 0.035
    elif category.lower() in ('poster', 'banner'):
        est_font_ratio = 0.09
    else:
        est_font_ratio = 0.07
    est_font_height_mm = round(physical_h_mm * est_font_ratio, 1)

    cmyk = _hex_to_cmyk(text_color)
    shaped_text = _shape_arabic_for_pdf(text) if text else ''

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
    c.drawString(20*mm, page_h - 24*mm, f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}   |   Code: {design_code}')

    y = page_h - 40*mm

    # ─── Visual mockup section ───
    c.setFillColor(black)
    c.setFont('Helvetica-Bold', 11)
    c.drawString(20*mm, y, '1. Visual Mockup (للمراجعة فقط — مش للطباعة المباشرة)')
    y -= 4*mm

    if image_url:
        try:
            # Download image
            resp = requests.get(image_url, timeout=20)
            if resp.status_code == 200:
                from reportlab.lib.utils import ImageReader
                img_reader = ImageReader(io.BytesIO(resp.content))
                # Fit في 80mm × 80mm preserving aspect
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
                c.drawString(22*mm, y - 8*mm, f'(image unavailable: HTTP {resp.status_code})')
                y -= 14*mm
        except Exception as e:
            logger.warning(f'[PDF] image embed failed: {e}')
            c.setFont('Helvetica-Oblique', 9)
            c.drawString(22*mm, y - 8*mm, '(image embed failed)')
            y -= 14*mm
    else:
        c.setFont('Helvetica-Oblique', 9)
        c.drawString(22*mm, y - 8*mm, '(no image provided)')
        y -= 14*mm

    # ─── Specs table ───
    c.setFillColor(black)
    c.setFont('Helvetica-Bold', 11)
    c.drawString(20*mm, y, '2. Print Specifications')
    y -= 6*mm

    rows = [
        ('Product / المنتج',     f'{product_name_en} ({product_name_ar})'),
        ('Material / الخامة',     material),
        ('Position / المكان',     text_position.upper()),
        ('Text / النص',           '<arabic-rendered-below>'),
        ('Font / الخط',           font_name),
        ('Font Size / حجم الخط',  f'{est_font_height_mm} mm  (~{int(est_font_height_mm * 2.83):d}pt)'),
        ('Color / اللون',         f'{text_color}   →   {cmyk}'),
        ('Print Area / مساحة الطباعة', f'{physical_w_mm} × {physical_h_mm} mm'),
        ('Quantity / الكمية',     str(quantity)),
    ]

    table_x = 20*mm
    table_w = page_w - 40*mm
    row_h = 7*mm
    label_w = 55*mm
    c.setStrokeColor(grey)
    c.setLineWidth(0.3)

    for label, value in rows:
        c.rect(table_x, y - row_h, table_w, row_h, fill=0, stroke=1)
        c.line(table_x + label_w, y - row_h, table_x + label_w, y)

        c.setFont('Helvetica-Bold', 9)
        c.setFillColor(HexColor('#475569'))
        c.drawString(table_x + 2*mm, y - 5*mm, label)

        c.setFillColor(black)
        if value == '<arabic-rendered-below>':
            # Render Arabic separately
            c.setFont(arabic_font_name, 11)
            c.drawString(table_x + label_w + 2*mm, y - 5*mm, shaped_text or '—')
        else:
            c.setFont('Helvetica', 9)
            c.drawString(table_x + label_w + 2*mm, y - 5*mm, value)
        y -= row_h

    y -= 6*mm

    # ─── Print-ready vector text block ───
    c.setFont('Helvetica-Bold', 11)
    c.setFillColor(black)
    c.drawString(20*mm, y, '3. Print-Ready Text Block (vector — مقاس 1:1)')
    y -= 5*mm
    c.setFont('Helvetica-Oblique', 8)
    c.setFillColor(grey)
    c.drawString(20*mm, y, 'This block reproduces the text at the intended physical print size. Use as direct artwork.')
    y -= 8*mm

    # Frame
    block_h = 35*mm
    c.setStrokeColor(HexColor('#a78bfa'))
    c.setLineWidth(1)
    c.setDash(3, 3)
    c.rect(20*mm, y - block_h, page_w - 40*mm, block_h, fill=0, stroke=1)
    c.setDash()  # solid line for next things

    # The text at physical size, centered
    if shaped_text:
        c.setFillColor(HexColor(text_color))
        # Use the estimated font height in mm → convert to points (1mm = 2.83pt)
        font_pt = max(14, int(est_font_height_mm * 2.83))
        c.setFont(arabic_font_name, font_pt)
        # Center the text in the block
        try:
            text_w = c.stringWidth(shaped_text, arabic_font_name, font_pt)
        except Exception:
            text_w = (page_w - 40*mm) * 0.6
        text_x = (page_w - text_w) / 2
        c.drawString(text_x, y - block_h/2 - font_pt/3, shaped_text)
    else:
        c.setFont('Helvetica-Oblique', 10)
        c.setFillColor(grey)
        c.drawString(page_w/2 - 30*mm, y - block_h/2, '(no text — pure design)')

    y -= (block_h + 8*mm)

    # ─── Customer / Order info ───
    c.setFillColor(black)
    c.setFont('Helvetica-Bold', 11)
    c.drawString(20*mm, y, '4. Customer & Order')
    y -= 6*mm

    customer_rows = [
        ('Customer / العميل', customer_name),
        ('Phone / الهاتف',     customer_phone),
        ('Quantity / الكمية',  str(quantity)),
        ('Notes / ملاحظات',    notes[:120] if notes else '—'),
    ]
    for label, value in customer_rows:
        c.rect(table_x, y - row_h, table_w, row_h, fill=0, stroke=1)
        c.line(table_x + label_w, y - row_h, table_x + label_w, y)
        c.setFont('Helvetica-Bold', 9)
        c.setFillColor(HexColor('#475569'))
        c.drawString(table_x + 2*mm, y - 5*mm, label)
        c.setFillColor(black)
        c.setFont('Helvetica', 9)
        c.drawString(table_x + label_w + 2*mm, y - 5*mm, str(value))
        y -= row_h

    # ─── Footer ───
    c.setFillColor(grey)
    c.setFont('Helvetica', 8)
    footer = 'Mouss Tec ERP  |  mousstec.com  |  This spec sheet is generated automatically from the AI design pipeline.'
    c.drawCentredString(page_w / 2, 12*mm, footer)
    if raw_idea:
        idea_short = (raw_idea[:90] + '…') if len(raw_idea) > 90 else raw_idea
        idea_shaped = _shape_arabic_for_pdf(idea_short)
        c.setFont(arabic_font_name, 7)
        c.setFillColor(grey)
        c.drawCentredString(page_w / 2, 7*mm, f'Brief: {idea_shaped}')

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()
