"""
🤖 المساعد الذكي لاستيراد المنتجات والأسعار — أي ملف: Excel / CSV / PDF / صورة.

🐛 [FIX]: الاستيراد كان بيقبل شيت بعناوين محددة بالظبط في أول صف («اسم المنتج»…) — قايمة أسعار حقيقية
زي «Peak Media Print Price List» (العنوان «اسم البند»، والسعر «السعر / من»، و«سعر مطبعتك») كانت بتطلع
«مالقيناش منتجات». وبورصة الأسعار ماكانش فيها رفع ملفات خالص.

الطريقة:
1. Excel/CSV ⇒ قراءة مباشرة (من غير AI): بندوّر على صف العناوين في أول ٢٠ صف من كل شيت، بنفهم أسماء
   أعمدة كتير (عربي/إنجليزي)، وبنوزّع كل بند على قسم الكتالوج من اسمه/قسمه بكلمات مفتاحية.
2. صورة / PDF / شيت مش مفهوم ⇒ Gemini (call_llm_layer) بيرجّع JSON بنفس الشكل.
3. النتيجة دايماً «مقترحة» — البائع بيراجعها قبل الحفظ (preview).

شكل الصف الموحّد: {'title', 'category', 'custom_category', 'description', 'min_qty', 'price', 'unit', 'section'}
"""
from __future__ import annotations

import base64
import csv
import io
import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation

log = logging.getLogger('mouss_tec_core')

MAX_ROWS = 400   # × خانتين في صفحة المراجعة < DATA_UPLOAD_MAX_NUMBER_FIELDS
MAX_FILE = 15 * 1024 * 1024
SHEET_EXT = ('.xlsx', '.xlsm', '.csv')
AI_EXT = ('.pdf', '.png', '.jpg', '.jpeg', '.webp', '.heic', '.heif')
ACCEPT = ','.join(SHEET_EXT + AI_EXT)
_AR_DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩٫٬', '0123456789.,')


class ImportFailed(Exception):
    """رسالة للبائع (مترجمة) — الملف مش مدعوم أو مفيهوش حاجة مفهومة."""


# ─────────────── أسماء الأعمدة ───────────────

def _norm(s):
    s = str(s or '').strip().lower().translate(_AR_DIGITS)
    s = re.sub('[إأآ]', 'ا', s).replace('ة', 'ه').replace('ى', 'ي')
    return re.sub(r'[\s_\-/:()]+', ' ', s).strip()


COLS = {
    'title': ('اسم البند', 'البند', 'اسم المنتج', 'المنتج', 'الصنف', 'اسم الصنف', 'الاسم', 'الخدمه', 'item',
              'item name', 'product', 'product name', 'name', 'title', 'service'),
    'section': ('القسم', 'الفئه', 'التصنيف', 'النوع', 'category', 'section', 'type', 'group'),
    'material': ('الخامه', 'الخامات', 'material', 'paper', 'الورق'),
    'spec': ('المواصفه', 'المواصفه المطلوبه تفاصيل السعر', 'المواصفات', 'التفاصيل', 'الوصف', 'تفاصيل',
             'description', 'details', 'spec', 'specs', 'notes', 'ملاحظات'),
    'unit': ('وحده التسعير', 'الوحده', 'وحده', 'unit', 'uom', 'pricing unit'),
    'qty': ('الكميه المرجعيه', 'اقل كميه', 'الكميه', 'كميه', 'qty', 'quantity', 'moq', 'min qty', 'minimum'),
    'my_price': ('سعر مطبعتك', 'سعري', 'سعرك', 'my price', 'your price'),
    'price': ('السعر من', 'السعر', 'سعر', 'يبدا من', 'من', 'price', 'price from', 'from', 'unit price',
              'price egp', 'السعر ج م', 'السعر جنيه'),
    'price_to': ('الي', 'الى', 'to', 'price to', 'السعر الي'),
}
_COL_INDEX = {_norm(a): k for k, names in COLS.items() for a in names}


def _match_col(header):
    h = _norm(header)
    if not h:
        return None
    if h in _COL_INDEX:
        return _COL_INDEX[h]
    for alias, key in sorted(_COL_INDEX.items(), key=lambda kv: -len(kv[0])):   # الأطول الأول
        if len(alias) >= 3 and (h.startswith(alias) or alias in h):
            return key
    return None


def _num(v):
    """رقم من خانة: 2.5 / '٢٫٥' / '1,200 ج.م' ⇒ Decimal — نص زي «عرض سعر مطلوب» ⇒ None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float, Decimal)):
        d = Decimal(str(v))
    else:
        s = str(v).translate(_AR_DIGITS).replace(',', '')
        m = re.search(r'\d+(?:\.\d+)?', s)
        if not m:
            return None
        try:
            d = Decimal(m.group(0))
        except InvalidOperation:
            return None
    if not d.is_finite() or d <= 0 or d >= Decimal('10000000'):
        return None
    return d.quantize(Decimal('0.01'))


def _cell(v):
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return ' '.join(str(v).split())


# ─────────────── الأقسام بالكلمات المفتاحية ───────────────

CATEGORY_KEYWORDS = {
    'printing': (
        ('cards', ('كارت', 'كروت', 'كارنيه', 'بطاقه', 'business card', 'card', 'كوبون', 'تذكره', 'تذاكر')),
        ('flyers', ('فلاير', 'فلاير', 'بروشور', 'منيو', 'مطويه', 'flyer', 'brochure', 'menu', 'leaflet')),
        ('notebooks', ('دفتر', 'نوت', 'اجنده', 'نتيجه', 'تقويم', 'notebook', 'notepad', 'planner', 'calendar',
                       'بلوك', 'ايصال', 'فاتوره', 'فواتير')),
        ('mugs', ('كوبايه', 'كوباي', 'مج', 'mug', 'cup', 'كاس', 'زمزميه', 'ترمس')),
        ('pens', ('قلم', 'اقلام', 'pen')),
        ('apparel', ('تيشيرت', 'تي شيرت', 'يونيفورم', 'قميص', 'كاب', 'هودي', 'شيرت', 'مريله', 'shirt', 'uniform',
                     'hoodie', 'cap', 'بولو', 'ملابس', 'سلك سكرين ملابس', 'dtf', 'سبلميشن قماش', 'فلوك')),
        ('banners', ('بنر', 'بانر', 'لافته', 'يافطه', 'رول اب', 'رول أب', 'ستاند', 'فليكس', 'banner', 'roll up',
                     'rollup', 'sign', 'لوحه')),
        ('stickers', ('ستيكر', 'استيكر', 'ليبل', 'لاصق', 'sticker', 'label', 'فينيل')),
        ('packaging', ('علبه', 'علب', 'شنطه', 'شنط', 'كيس', 'اكياس', 'تغليف', 'كرتونه', 'box', 'bag', 'packag')),
        ('design', ('لوجو', 'هويه', 'تصميم شعار', 'logo', 'branding', 'identity')),
        ('social', ('سوشيال', 'بوست', 'social', 'post')),
        ('events', ('دعوه', 'دعوات', 'فرح', 'زفاف', 'invitation', 'wedding')),
        ('gifts', ('هدايا', 'هديه', 'ميداليه', 'سلسله', 'gift', 'keychain', 'دروع', 'درع')),
        ('office', ('ورق مراسلات', 'ظرف', 'اظرف', 'فولدر', 'letterhead', 'envelope', 'folder', 'مراسلات', 'ختم',
                    'اختام', 'روشته', 'ملف', 'مستندات', 'stamp', 'بادج', 'ورق مطبوع')),
        ('photo', ('كانفاس', 'برواز', 'براويز', 'تابلوه', 'صوره', 'صور', 'canvas', 'frame', 'photo', 'ورق حائط')),
        ('cafe', ('منيو', 'مفرش', 'كوستر', 'menu', 'coaster', 'كوب ورقي', 'سليف', 'ساندوتش', 'طبق', 'كوب بلاستيك')),
    ),
    'automotive': (
        ('ignition', ('بوجيه', 'كهربا', 'مارش', 'دينامو', 'spark', 'ignition')),
        ('brakes', ('فرامل', 'تيل', 'طنبوره', 'ديسك', 'brake')),
        ('suspension', ('مساعد', 'عفشه', 'مقص', 'جلبه', 'suspension', 'shock')),
        ('filters', ('فلتر', 'زيت', 'filter', 'oil')),
        ('tires', ('كاوتش', 'اطار', 'جنط', 'tire', 'tyre', 'rim')),
        ('battery', ('بطاريه', 'battery')),
        ('cooling', ('رادياتير', 'تكييف', 'مروحه', 'radiator', 'cooling')),
        ('engine', ('موتور', 'سير', 'كاتينه', 'engine', 'belt')),
        ('lights', ('فانوس', 'كشاف', 'لمبه', 'light', 'lamp')),
        ('body', ('صاج', 'مرايه', 'اكصدام', 'كبوت', 'body', 'bumper', 'mirror')),
    ),
}


def _has(blob, word):
    """الكلمة جوه النص — الكلمات القصيرة (٣ حروف وأقل) ككلمة كاملة بس («مج» مش جوه «ومجلدة»)."""
    w = _norm(word)
    if len(w) <= 3:
        return re.search(rf'(?<!\w){re.escape(w)}(?!\w)', blob) is not None
    return w in blob


def guess_category(sector, *texts):
    """(قسم الكتالوج، النوع لو أخرى) من الاسم أو القسم أو المواصفة."""
    from clients.views.catalog_views import SECTORS
    cats = SECTORS[sector]['categories']
    blob = ' '.join(_norm(t) for t in texts if t)
    for key, words in CATEGORY_KEYWORDS.get(sector, ()):
        if key in cats and any(_has(blob, w) for w in words):
            return key, ''
    # اسم قسم الكتالوج نفسه مكتوب؟
    for key, (ar, _icon) in cats.items():
        if _norm(ar) in blob or key == _norm(texts[0] if texts else ''):
            return key, ''
    first = next((t for t in texts if t), '')
    return 'other', ' '.join(str(first).split())[:80]


# ─────────────── قراءة الشيتات ───────────────

def _rows_from_sheet(upload, ext):
    """[(sheet_name, [row cells])] لكل شيت."""
    upload.seek(0)
    if ext == '.csv':
        raw = upload.read()
        for enc in ('utf-8-sig', 'cp1256', 'latin-1'):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        dialect = csv.excel
        try:
            dialect = csv.Sniffer().sniff(text[:4000], delimiters=',;\t|')
        except csv.Error:
            pass
        return [('csv', [list(r) for r in csv.reader(io.StringIO(text), dialect)])]
    from openpyxl import load_workbook
    try:
        wb = load_workbook(upload, read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001 — ملف بايظ/محمي بباسورد
        raise ImportFailed(str(e)) from e
    out = []
    for ws in wb.worksheets:
        rows = []
        for i, r in enumerate(ws.iter_rows(values_only=True)):
            if i > MAX_ROWS + 30:
                break
            rows.append(list(r))
        out.append((ws.title, rows))
    return out


def _find_header(rows):
    """(رقم صف العناوين، {col_index: key}) — أول صف فيه عمود اسم + عمودين معروفين على الأقل."""
    for i, row in enumerate(rows[:20]):
        mapping = {}
        for j, cell in enumerate(row):
            key = _match_col(cell)
            if key and key not in mapping.values():
                mapping[j] = key
        if 'title' in mapping.values() and len(mapping) >= 2:
            return i, mapping
    return None, None


def parse_sheet(upload, ext, sector):
    """صفوف موحّدة من Excel/CSV — أو [] لو مالقيناش صف عناوين مفهوم (⇒ نجرّب الـ AI)."""
    out = []
    for _sheet, rows in _rows_from_sheet(upload, ext):
        hi, mapping = _find_header(rows)
        if hi is None:
            continue
        last_section = ''
        for row in rows[hi + 1:]:
            vals = {key: (row[j] if j < len(row) else None) for j, key in mapping.items()}
            title = _cell(vals.get('title'))
            section = _cell(vals.get('section')) or last_section
            last_section = section
            if not title or len(title) < 2:
                continue
            price = _num(vals.get('my_price')) or _num(vals.get('price'))
            material, spec, unit = _cell(vals.get('material')), _cell(vals.get('spec')), _cell(vals.get('unit'))
            qty = _cell(vals.get('qty'))
            desc = ' — '.join(x for x in (material, spec) if x)
            cat, custom = guess_category(sector, title, section, material)
            out.append({
                'title': title[:200], 'category': cat, 'custom_category': custom if cat == 'other' else '',
                'description': desc[:3000], 'min_qty': (f'{qty} {unit}'.strip() if qty else unit)[:60],
                'price': price, 'unit': unit[:60], 'section': section[:80],
            })
            if len(out) >= MAX_ROWS:
                return out
    return out


# ─────────────── الذكاء الاصطناعي (صور / PDF / شيت مش مفهوم) ───────────────

_AI_PROMPT = """You extract a print shop's (or car-parts shop's) product/price list from a file.
Return ONLY JSON: {"items":[{"title":"","section":"","description":"","min_qty":"","unit":"","price":0}]}
- title: product/service name exactly as written (keep Arabic).
- section: the category/heading it belongs to if any.
- description: material/specs in one short line.
- min_qty: minimum or reference quantity with unit if shown.
- price: the shop's own price if there is a "your price" column, else the starting price. Number in EGP, or null
  if it says "on request"/no price.
Ignore headers, totals, phone numbers and decorations. Up to 300 items."""


def _ai_available():
    from django.conf import settings
    return bool(getattr(settings, 'ENABLE_AI_PREDICTIONS', False) and (
        getattr(settings, 'GEMINI_API_KEY', '') or getattr(settings, 'AI_VISION_API_KEY', '')))


def _ai_rows(messages, sector):
    from inventory.ai_services import call_llm_layer
    raw = call_llm_layer(messages, json_mode=True, max_retries=2)
    if not raw:
        raise ImportFailed('ai_empty')
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ImportFailed('ai_json') from e
    items = data.get('items') if isinstance(data, dict) else data
    out = []
    for it in (items or [])[:MAX_ROWS]:
        if not isinstance(it, dict):
            continue
        title = _cell(it.get('title'))
        if len(title) < 2:
            continue
        section = _cell(it.get('section'))
        cat, custom = guess_category(sector, title, section)
        out.append({
            'title': title[:200], 'category': cat, 'custom_category': custom if cat == 'other' else '',
            'description': _cell(it.get('description'))[:3000], 'min_qty': _cell(it.get('min_qty'))[:60],
            'price': _num(it.get('price')), 'unit': _cell(it.get('unit'))[:60], 'section': section[:80],
        })
    return out


def parse_with_ai(upload, ext, sector):
    if not _ai_available():
        raise ImportFailed('ai_off')
    upload.seek(0)
    data = upload.read()
    if ext in SHEET_EXT:
        # شيت مش مفهوم ⇒ نبعته نص (TSV) للموديل
        lines = []
        for name, rows in _rows_from_sheet(io.BytesIO(data), ext):
            lines.append(f'# sheet: {name}')
            for r in rows[:350]:
                cells = [_cell(c) for c in r]
                if any(cells):
                    lines.append('\t'.join(cells))
        content = [{'type': 'text', 'text': _AI_PROMPT + '\n\nFILE:\n' + '\n'.join(lines)[:60000]}]
        return _ai_rows([{'role': 'user', 'content': content[0]['text']}], sector)
    mime = {'.pdf': 'application/pdf', '.png': 'image/png', '.webp': 'image/webp',
            '.heic': 'image/heic', '.heif': 'image/heif'}.get(ext, 'image/jpeg')
    b64 = base64.b64encode(data).decode('ascii')
    return _ai_rows([{'role': 'user', 'content': [
        {'type': 'text', 'text': _AI_PROMPT},
        {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{b64}'}},
    ]}], sector)


def read_any(upload, sector):
    """🤖 أي ملف ⇒ (صفوف، 'sheet'|'ai'). بيرمي ImportFailed برسالة واضحة."""
    from django.utils.translation import gettext as _
    name = getattr(upload, 'name', '') or ''
    ext = os.path.splitext(name)[1].lower()
    if getattr(upload, 'size', 0) > MAX_FILE:
        raise ImportFailed(_('الملف أكبر من ١٥ ميجا.'))
    if ext == '.xls':
        raise ImportFailed(_('ملف Excel القديم (.xls) — افتحه واحفظه «xlsx» وارفعه تاني، أو صوّر القايمة وارفع الصورة.'))
    if ext not in SHEET_EXT + AI_EXT:
        raise ImportFailed(_('نوع الملف ده مش مدعوم — ارفع Excel أو CSV أو PDF أو صورة.'))
    rows, how = [], 'sheet'
    if ext in SHEET_EXT:
        try:
            rows = parse_sheet(upload, ext, sector)
        except ImportFailed:
            rows = []
    if not rows:
        how = 'ai'
        try:
            rows = parse_with_ai(upload, ext, sector)
        except ImportFailed as e:
            if str(e) == 'ai_off':
                raise ImportFailed(_('مقدرناش نفهم الملف ده لوحدنا، والمساعد الذكي مش متاح دلوقتي — '
                                     'ارفع شيت فيه عمود «اسم المنتج» أو «البند»، أو ابعت الملف لدعم Mouss Tec.'))
            raise ImportFailed(_('المساعد الذكي مقدرش يقرا الملف — جرّب صورة أوضح أو شيت Excel.'))
        except Exception:  # noqa: BLE001
            log.exception('[SMART IMPORT] AI parse failed for %s', ext)
            raise ImportFailed(_('المساعد الذكي مقدرش يقرا الملف — جرّب صورة أوضح أو شيت Excel.'))
    if not rows:
        raise ImportFailed(_('مالقيناش منتجات في الملف.'))
    return rows, how


# ─────────────── بورصة الأسعار: بنود الملف ⇒ بنود البورصة ───────────────

# كلمات كل بند + كام وحدة في وحدة البورصة (عشان سعر «القطعة» يتحوّل لسعر الـ ١٠٠٠)
SPEC_MATCH = {
    'cards-1000': (('كارت شخصي', 'كروت شخصيه', 'business card'), 1000),
    'banner-flex-m2': (('بنر', 'بانر', 'فليكس', 'banner', 'flex'), 1),
    'rollup-85': (('رول اب', 'رول أب', 'roll up', 'rollup'), 1),
    'flyer-a5-1000': (('فلاير a5', 'flyer a5'), 1000),
    'flyer-a4-1000': (('فلاير a4', 'flyer a4'), 1000),
    'brochure-a4-3fold': (('بروشور', 'brochure', 'مطويه'), 1000),
    'sticker-vinyl-m2': (('ستيكر فينيل', 'فينيل', 'vinyl'), 1),
    'label-roll-1000': (('ليبل', 'label', 'ستيكر رول'), 1000),
    'letterhead-a4-1000': (('ورق مراسلات', 'letterhead'), 1000),
    'envelope-dl-1000': (('ظرف', 'envelope'), 1000),
    'notepad-a5-100': (('بلوك نوت', 'نوت بوك', 'notepad'), 100),
    'mug-print-50': (('كوبايه', 'مج', 'mug'), 50),
    'tshirt-print-50': (('تيشيرت', 'تي شيرت', 't-shirt', 'tshirt'), 50),
    'invoice-book-a5': (('دفتر فواتير', 'دفتر ايصالات', 'invoice book'), 10),
    'pvc-sign-m2': (('فوم', 'foam'), 1),
}
_PER_PIECE = ('قطعه', 'قطعة', 'piece', 'pcs', 'الواحده', 'each', 'unit', 'نسخه', 'كارت', 'ورقه')


def suggest_prices(rows, specs):
    """{spec_id: (Decimal, 'من السطر …')} — أول سطر بيطابق كل بند، والسعر متحوّل لوحدة البورصة."""
    out = {}
    for spec in specs:
        rule = SPEC_MATCH.get(spec.code)
        if not rule:
            continue
        words, factor = rule
        for r in rows:
            if r.get('price') is None:
                continue
            blob = _norm(r['title'] + ' ' + r.get('description', ''))
            if not any(_has(blob, w) for w in words):
                continue
            unit = _norm(r.get('unit', ''))
            per_piece = any(_norm(p) in unit for p in _PER_PIECE)
            price = r['price'] * factor if (per_piece and factor > 1) else r['price']
            out[spec.pk] = (price.quantize(Decimal('1')) if price >= 10 else price, r['title'])
            break
    return out
