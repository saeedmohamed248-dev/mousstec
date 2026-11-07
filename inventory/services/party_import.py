"""
👥 استيراد العملاء والموردين بأرصدتهم الافتتاحية من Excel/CSV
================================================================
أول أسبوع لأي شركة جديدة: عندها كشكول فيه «مين عليه فلوس لينا» و«إحنا علينا لمين».
قبل كده ده كان بيتعمل من لوحة Django الفنية بس (أعمدة إنجليزي وعمود id).

القواعد:
* الرصيد المكتوب في الملف = **الرصيد النهائي** للطرف (مش إضافة عليه).
  الفرق عن الرصيد الحالي بيتقيّد في الدفتر (AccountingService.post_party_balance_adjustment)
  ⇒ ميزان المراجعة يفضل مطابق لكشوف الحساب.
* عمود الرصيد مش موجود أو خانته فاضية ⇒ الرصيد مايتلمسش.
* العميل بيتعرّف برقم الموبايل (مطلوب). المورد بالموبايل لو موجود وإلا بالاسم.
* «عميل نقدي» مشترك ⇒ ممنوع يتحط عليه رصيد (آجل).
* صف بايظ مايوقّفش باقي الملف (savepoint لكل صف).
"""
from __future__ import annotations

import re
from decimal import Decimal

from django.db import transaction

from inventory.services.accounting_service import AccountingService

HEADERS = ['name', 'phone', 'balance', 'tax_id']

_KEYWORDS = {
    'phone': ['phone', 'mobile', 'tel', 'موبايل', 'الموبايل', 'تليفون', 'التليفون', 'هاتف', 'الهاتف', 'رقم'],
    'balance': ['balance', 'debt', 'due', 'opening', 'رصيد', 'الرصيد', 'مديونية', 'المديونية',
                'عليه', 'له', 'مستحق', 'المستحق', 'باقي', 'الباقي', 'آجل', 'اجل'],
    'tax_id': ['tax', 'vat', 'ضريبي', 'الضريبي', 'ضريبة'],
    'name': ['name', 'customer', 'vendor', 'supplier', 'اسم', 'الاسم', 'العميل', 'عميل', 'المورد', 'مورد'],
}
_ORDER = ['tax_id', 'phone', 'balance', 'name']


def resolve_columns(headers) -> dict:
    norm = [str(h or '').strip().lower().replace('_', ' ') for h in headers]
    mapping, used = {}, set()
    for field in _ORDER:                     # تطابق مباشر بالاسم الإنجليزي
        for i, h in enumerate(norm):
            if i not in used and h.replace(' ', '_') == field:
                mapping[field] = i
                used.add(i)
                break
    for field in _ORDER:                     # بعدين الكلمات المفتاحية
        if field in mapping:
            continue
        for i, h in enumerate(norm):
            if i not in used and any(k in h for k in _KEYWORDS[field]):
                mapping[field] = i
                used.add(i)
                break
    return mapping


def clean_phone(raw) -> str:
    """Excel بيشيل الصفر اللي على الشمال ويكتب الرقم 1012345678.0 — نرجّعه 01012345678."""
    s = str(raw or '').strip()
    if re.fullmatch(r'\d+\.0+', s):
        s = s.split('.')[0]
    s = re.sub(r'[\s\-\(\)]+', '', s)
    if re.fullmatch(r'1[0125]\d{8}', s):
        s = '0' + s
    return s


def parse_amount(raw):
    """رقم (موجب أو سالب) أو None لو الخانة فاضية. بيقبل الأرقام العربي والفواصل."""
    s = str(raw if raw is not None else '').strip()
    if not s:
        return None
    s = s.translate(str.maketrans('٠١٢٣٤٥٦٧٨٩٫٬', '0123456789.,')).replace(',', '').replace(' ', '')
    neg = s.startswith('(') and s.endswith(')')
    s = s.strip('()')
    try:
        d = Decimal(s)
    except Exception:
        raise ValueError(f'الرصيد «{raw}» مش رقم')
    if not d.is_finite():
        raise ValueError(f'الرصيد «{raw}» مش رقم')
    return (-d if neg else d).quantize(Decimal('0.01'))


def import_parties(kind: str, dataset, user=None) -> dict:
    """kind: 'customer' أو 'vendor'. بيرجّع إحصائيات + أول ٢٠ خطأ."""
    from inventory.models import Customer, Vendor
    model = Customer if kind == 'customer' else Vendor
    colmap = resolve_columns(dataset.headers or [])
    if 'name' not in colmap and 'phone' not in colmap:
        raise ValueError('مقدرتش ألاقي عمود الاسم أو الموبايل في الملف.')

    def cell(row, key):
        idx = colmap.get(key)
        if idx is None:
            return ''
        try:
            v = row[idx]
        except IndexError:
            return ''
        return '' if v is None else v

    stats = {'created': 0, 'updated': 0, 'balances': 0, 'total_balance': Decimal('0'), 'errors': []}
    for i, row in enumerate(dataset, start=2):
        name = str(cell(row, 'name')).strip()
        phone = clean_phone(cell(row, 'phone'))
        tax_id = str(cell(row, 'tax_id')).strip()
        if not name and not phone:
            continue
        try:
            with transaction.atomic():
                balance = parse_amount(cell(row, 'balance'))
                if kind == 'customer':
                    if not phone:
                        raise ValueError('رقم الموبايل مطلوب للعميل')
                    party = Customer.find_by_phone(phone)
                else:
                    party = None
                    if phone:
                        party = Vendor.objects.filter(phone=phone).first()
                    if party is None and name:
                        party = Vendor.objects.filter(name__iexact=name).first()

                is_new = party is None
                if is_new:
                    party = model(name=name or phone, phone=phone or None)
                    if kind == 'customer':
                        # 🐛 [FIX]: الرقم لازم يتوحّد قبل فحص «عميل نقدي» — وإلا 0000000000
                        #    كان بيتسجّل عميل جديد عليه آجل بدل ما يترفض.
                        party.phone = Customer.normalize_phone(phone)
                else:
                    if name:
                        party.name = name
                    if phone and not party.phone:
                        party.phone = phone
                if tax_id:
                    party.tax_id = tax_id
                if kind == 'customer' and getattr(party, 'is_walk_in', False):
                    raise ValueError('ده «عميل نقدي» مشترك — مايتحطش عليه رصيد آجل')

                old = Decimal(str(party.balance or 0)) if not is_new else Decimal('0')
                if balance is not None:
                    party.balance = balance
                party.save()
                delta = (balance - old) if balance is not None else Decimal('0')
                if delta:
                    AccountingService.post_party_balance_adjustment(party=party, delta=delta, created_by=user)
                    stats['balances'] += 1
                stats['created' if is_new else 'updated'] += 1
                if balance is not None:
                    stats['total_balance'] += balance
        except Exception as exc:
            stats['errors'].append(f'صف {i} ({name or phone}): {exc}')
    stats['error_count'] = len(stats['errors'])
    stats['errors'] = stats['errors'][:20]
    return stats
