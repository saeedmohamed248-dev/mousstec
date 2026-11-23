"""🗂️ مكتب طلبات أسعار الكتالوج — تنظيم بيانات الطلب لصفحة السوبر أدمن (من غير DB).

- `extract_contacts`: يطلّع من «التفاصيل» اللي العميل كتبها أرقام الموبايل التانية واللينكات
  (فيسبوك/إنستجرام/…) ويرجّع باقي الكلام نضيف — بدل ما تظهر كتلة نص واحدة.
- `wa_digits`: رقم الواتساب الدولي من أي شكل مصري (01x… / 1x… / +20… / 0020…).
- `age_info`: عمر الطلب + لون الـ SLA (أخضر/أصفر/أحمر) للطلبات اللي لسه مفتوحة.
- `csv_rows`: صفوف ملف الإكسيل (CSV بـ BOM عشان العربي يفتح صح في Excel).
"""
from __future__ import annotations

import re
from datetime import timedelta

from django.utils import timezone
from django.utils.translation import gettext as _

_ARABIC_DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')
_URL_RE = re.compile(r'(?:https?://|www\.)[^\s<>"\']+', re.I)
# موبايل مصري: 01x + ٨ أرقام، بمسافات/شرط اختيارية، أو بكود الدولة
_PHONE_RE = re.compile(r'(?<!\d)(?:\+?20|0020)?[\s-]?0?1[0125](?:[\s-]?\d){8}(?!\d)')

_SOCIAL = (
    ('facebook', ('facebook.com', 'fb.com', 'fb.me', 'm.me'), 'fab fa-facebook', 'Facebook'),
    ('instagram', ('instagram.com', 'instagr.am'), 'fab fa-instagram', 'Instagram'),
    ('tiktok', ('tiktok.com',), 'fab fa-tiktok', 'TikTok'),
    ('whatsapp', ('wa.me', 'whatsapp.com'), 'fab fa-whatsapp', 'WhatsApp'),
    ('drive', ('drive.google.com', 'docs.google.com'), 'fab fa-google-drive', 'Google Drive'),
    ('maps', ('maps.app.goo.gl', 'goo.gl/maps', 'google.com/maps'), 'fas fa-location-dot', _('اللوكيشن')),
)
_IMAGE_EXT = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg')


def wa_digits(phone: str) -> str:
    """رقم واتساب دولي (أرقام بس). المصري من غير الصفر (1099355771) كان بيطلع لينك غلط."""
    d = ''.join(ch for ch in (phone or '').translate(_ARABIC_DIGITS) if ch.isdigit())
    if d.startswith('00'):
        d = d[2:]
    if d.startswith('0') and len(d) == 11:        # 01xxxxxxxxx ⇒ 201xxxxxxxxx
        return '20' + d[1:]
    if d.startswith('1') and len(d) == 10:        # 1xxxxxxxxx (نسي الصفر) ⇒ 201xxxxxxxxx
        return '20' + d
    return d


def local_phone(phone: str) -> str:
    """الشكل المحلي اللي بيتعرض ويتّصل بيه: 01xxxxxxxxx."""
    d = wa_digits(phone)
    if d.startswith('20') and len(d) == 12:
        return '0' + d[2:]
    return (phone or '').strip()


def _link_kind(url: str):
    low = url.lower()
    for key, hosts, icon, label in _SOCIAL:
        if any(h in low for h in hosts):
            return key, icon, label
    return 'web', 'fas fa-link', _('لينك')


def extract_contacts(text: str, *, main_phone: str = '') -> dict:
    """{'phones': [{'local','wa'}], 'links': [{'url','kind','icon','label'}], 'note': نص بعد الشيل}."""
    text = text or ''
    links, seen_urls = [], set()
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip('.,،؛;)!؟?')
        full = url if url.lower().startswith('http') else 'https://' + url
        if full in seen_urls:
            continue
        seen_urls.add(full)
        kind, icon, label = _link_kind(full)
        links.append({'url': full, 'kind': kind, 'icon': icon, 'label': label})
    phones, seen = [], {wa_digits(main_phone)} if main_phone else set()
    lines = []
    for raw in text.splitlines():
        line = _URL_RE.sub(' ', raw).translate(_ARABIC_DIGITS)
        had_contact = line != raw.translate(_ARABIC_DIGITS)
        for m in _PHONE_RE.finditer(line):
            wa = wa_digits(m.group(0))
            if len(wa) != 12:
                continue
            had_contact = True
            if wa not in seen:
                seen.add(wa)
                phones.append({'local': local_phone(wa), 'wa': wa})
        line = _PHONE_RE.sub(' ', line)
        line = re.sub(r'[ \t]+', ' ', line).strip(' /:-–—|،,')
        # سطر كان فيه رقم/لينك وباقيه كلمتين وصف («Facebook page») ⇒ الزرار بيغني عنه
        if line and not (had_contact and len(line.split()) <= 5):
            lines.append(line)
    return {'phones': phones, 'links': links, 'note': '\n'.join(lines)}


def is_image(name: str) -> bool:
    return (name or '').lower().endswith(_IMAGE_EXT)


def age_info(created_at, status: str, now=None) -> dict:
    """عمر الطلب + مستوى التأخير للمفتوح بس: ok (< ساعة) · warn (< ٦ ساعات) · late."""
    now = now or timezone.now()
    delta = max(now - created_at, timedelta(0))
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        label = _('من %(n)s دقيقة') % {'n': max(minutes, 1)}
    elif minutes < 60 * 24:
        label = _('من %(n)s ساعة') % {'n': minutes // 60}
    else:
        label = _('من %(n)s يوم') % {'n': minutes // (60 * 24)}
    level = ''
    if status == 'new':
        level = 'ok' if minutes < 60 else ('warn' if minutes < 360 else 'late')
    return {'label': label, 'level': level, 'minutes': minutes}


CSV_HEADER = ('الكود', 'التاريخ', 'الحالة', 'القسم', 'المنتج', 'المتجر', 'العميل', 'الموبايل',
              'أرقام تانية', 'الكمية', 'المقاس', 'المقاس بالسنتي', 'اللون', 'الشكل', 'التشطيب',
              'العربية', 'التفاصيل', 'لينكات', 'ملف التصميم', 'ملاحظاتك')


def csv_row(r, *, attachment_url: str = '') -> list:
    o = r.options or {}
    c = extract_contacts(r.details, main_phone=r.phone)
    return [
        str(r.request_code)[:8], timezone.localtime(r.created_at).strftime('%Y-%m-%d %H:%M'),
        r.get_status_display(), r.get_sector_display() if hasattr(r, 'get_sector_display') else r.sector,
        r.item_title, r.store_name, r.name, local_phone(r.phone),
        ' / '.join(p['local'] for p in c['phones']), r.quantity, o.get('size', ''), o.get('dimensions', ''),
        o.get('color', ''), o.get('shape', ''), o.get('finish', ''), r.car_info, c['note'],
        ' '.join(link['url'] for link in c['links']), attachment_url, r.admin_notes,
    ]


def vcard(r) -> str:
    """كارت الاتصال (.vcf) — يتحفظ في موبايلك بالاسم + كل أرقامه."""
    c = extract_contacts(r.details, main_phone=r.phone)
    name = (r.name or '').replace(';', ' ').replace('\n', ' ').strip()
    lines = ['BEGIN:VCARD', 'VERSION:3.0', f'FN:{name}', f'N:{name};;;;',
             f'TEL;TYPE=CELL:+{wa_digits(r.phone)}']
    lines += [f'TEL;TYPE=OTHER:+{p["wa"]}' for p in c['phones']]
    lines += [f'URL:{link["url"]}' for link in c['links'][:3]]
    lines.append(f'NOTE:Mouss Tec — {r.item_title}'.replace('\n', ' '))
    lines.append('END:VCARD')
    return '\r\n'.join(lines) + '\r\n'
