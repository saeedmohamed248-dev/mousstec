"""
🔧 Offline diagnostic answer — fault codes explained without an AI provider.

When the LLM diagnostic expert can't run (no key, outage, quota), the chat
used to answer only "the service isn't enabled". A workshop still gets real
value from:

* the DTC catalog (``diagnostics_catalog.DTCDefinition`` — description,
  severity, guided test steps, likely OEM parts), and when a code isn't in
  the catalog, the standard SAE meaning of its family (P03xx = ignition /
  misfire, P04xx = emissions …);
* the parts for that fault that are in the shop's own stock right now.
"""
from __future__ import annotations

import re
from html import escape

_DTC = re.compile(r'\b([PCBU])\s?([0-9A-F]{4})\b', re.IGNORECASE)

# SAE J2012 families → (meaning, stock search keywords)
_FAMILIES = [
    ('P00', 'التحكم في الهواء/الوقود والبخار (Fuel & air metering, auxiliary emission)', ['حساس', 'MAF', 'VANOS', 'فانوس']),
    ('P01', 'قياس الهواء والوقود (MAF / MAP / حساسات الأكسجين)', ['MAF', 'حساس هوا', 'حساس اكسجين', 'لامدا', 'فلتر هوا']),
    ('P02', 'دائرة البخاخات والوقود (Injectors / fuel)', ['بخاخ', 'injector', 'طرمبه', 'فلتر بنزين']),
    ('P03', 'الإشعال وتقطيع الاحتراق (Ignition / misfire)', ['بوجيه', 'بوجيهات', 'موبينه', 'كويل', 'coil', 'spark']),
    ('P04', 'أنظمة العادم والانبعاثات (EGR / EVAP / Catalyst)', ['كتلايزر', 'شكمان', 'حساس اكسجين', 'EGR', 'صمام']),
    ('P05', 'سرعة العربية والرلنتي (Speed / idle control)', ['حساس سرعه', 'بوابه', 'throttle', 'رلنتي']),
    ('P06', 'كمبيوتر المحرك ودوائر الخرج (ECU / outputs)', ['كمبيوتر', 'ECU', 'ريليه']),
    ('P07', 'ناقل الحركة (Transmission)', ['فتيس', 'زيت فتيس', 'mechatronic', 'ميكاترونيك']),
    ('P08', 'ناقل الحركة (Transmission)', ['فتيس', 'زيت فتيس']),
    ('P09', 'ناقل الحركة (Transmission)', ['فتيس', 'زيت فتيس']),
    ('P1', 'كود خاص بالمصنّع للمحرك (Manufacturer-specific powertrain)', []),
    ('P2', 'المحرك/الانبعاثات — كود عام إضافي (Powertrain, SAE)', ['حساس']),
    ('P3', 'المحرك — كود خاص بالمصنّع أو مشترك', []),
    ('C', 'الشاسيه: فرامل ABS / ثبات / تعليق (Chassis)', ['ABS', 'حساس ABS', 'فرامل', 'تيل']),
    ('B', 'البودي: إيرباج / تكييف / إضاءة (Body)', ['ايرباج', 'كشاف', 'تكييف']),
    ('U', 'شبكة الاتصال بين الكمبيوترات (CAN bus / network)', ['كمبيوتر', 'فيشه', 'بطاريه']),
]

_SEVERITY = {'low': '🟢 بسيط', 'medium': '🟡 متوسط', 'high': '🟠 عالي', 'critical': '🔴 خطير — ماتسوقش العربية'}


def _family(code: str):
    for prefix, meaning, words in _FAMILIES:
        if code.startswith(prefix):
            return meaning, words
    return None, []


def extract_codes(text: str) -> list[str]:
    seen, out = set(), []
    for sys_, digits in _DTC.findall(text or ''):
        code = f'{sys_.upper()}{digits.upper()}'
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out[:5]


def _stock_for(words, oem_parts):
    """Parts in the shop's stock for these keywords / OEM numbers (tenant schema)."""
    try:
        from django.db.models import Q, Sum
        from inventory.models import Product
    except Exception:
        return []
    cond = Q()
    for w in words:
        cond |= Q(name__icontains=w)
    for oem in oem_parts or []:
        cond |= Q(part_number__icontains=oem) | Q(oem_cross_reference__icontains=oem)
    if not cond:
        return []
    try:
        return list(Product.objects.filter(is_active=True).filter(cond)
                    .annotate(qty=Sum('inventory__quantity'))
                    .filter(qty__gt=0)[:5])
    except Exception:
        return []


def offline_diagnosis(user_text: str, audience: str = 'shop') -> dict:
    codes = extract_codes(user_text)
    if not codes:
        return {
            'success': True, 'mode': 'offline',
            'answer': ('🔧 التشخيص الذكي بالكلام مش متاح دلوقتي، بس لو معاك كود العطل '
                       '(زي P0300 أو P0171) اكتبه وهشرحه لك وأطلعلك القطع المتاحة عندك.'),
        }
    try:
        from django_tenants.utils import schema_context
        from diagnostics_catalog.models import DTCDefinition
        with schema_context('public'):
            defs = {d.code: d for d in DTCDefinition.objects.filter(code__in=codes)}
    except Exception:
        defs = {}

    blocks = []
    for code in codes:
        d = defs.get(code)
        meaning, words = _family(code)
        lines = [f'🔎 {code}']
        oem = []
        if d is not None:
            lines.append(f'• المعنى: {escape(d.short_description)}')
            if d.full_description:
                lines.append(f'• التفاصيل: {escape(d.full_description[:300])}')
            lines.append(f"• الخطورة: {_SEVERITY.get(d.severity, d.severity)}")
            if audience == 'shop' and d.guided_steps:
                lines.append('• خطوات الفحص:')
                for st in d.guided_steps[:4]:
                    lines.append(f"   {st.get('step', '')}. {escape(str(st.get('title', '')))} — {escape(str(st.get('action', '')))}")
            oem = d.likely_oem_parts or []
        elif meaning:
            lines.append(f'• الكود مش في الكتالوج بالتفصيل — عيلته: {meaning}.')
            if code.startswith('P030'):
                cyl = code[-1]
                lines.append('• P0300 = تقطيع عشوائي في أكتر من سلندر.' if cyl == '0'
                             else f'• تقطيع في السلندر رقم {cyl}.')
                if audience == 'shop':
                    lines.append('• ابدأ بـ: بدّل الموبينة/البوجيه بين السلندرات وشوف العطل بيتنقل ولا لأ، '
                                 'بعدين البخاخ وضغط السلندر.')
        stock = _stock_for(words, oem)
        if stock:
            lines.append('• موجود عندك في المخزن:')
            for p in stock:
                lines.append(f'   – {escape(p.name)} ({escape(p.part_number)}) — {p.qty} قطعة')
        blocks.append('\n'.join(lines))

    head = '🔧 (رد سريع من كتالوج الأعطال — التشخيص الذكي الكامل مش متاح دلوقتي)\n\n'
    tail = '' if audience == 'shop' else '\n\n⚠️ اعرض العربية على فني قبل ما تغيّر أي قطعة.'
    return {'success': True, 'mode': 'offline', 'answer': head + '\n\n'.join(blocks) + tail}
