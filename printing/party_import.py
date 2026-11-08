"""
👥 استيراد عملاء وموردين المطبعة بأرصدتهم الافتتاحية من Excel/CSV.

زي استيراد السيارات (inventory/services/party_import.py) بنفس التعرّف على الأعمدة:
* الرصيد في الملف = **الرصيد الافتتاحي** (المديونية قبل السيستم): للعميل موجب = عليه لينا،
  للمورد موجب = علينا له. القيد بيتعمل لوحده من إشارة الحفظ (printing/signals.py ⇒
  ledger.sync_customer_opening / sync_supplier_opening) — ومزامنة مش إضافة، فرفع نفس
  الملف تاني مابيكررش القيد.
* خانة الرصيد الفاضية ⇒ الرصيد الافتتاحي مايتلمسش.
* العميل/المورد بيتعرّف بالموبايل لو موجود، وإلا بالاسم.
* صف بايظ مايوقّفش باقي الملف (savepoint لكل صف).
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from inventory.services.party_import import clean_phone, parse_amount, resolve_columns

HEADERS = ['name', 'phone', 'balance', 'company']


def _cell(row, colmap, key):
    idx = colmap.get(key)
    if idx is None:
        return ''
    try:
        v = row[idx]
    except IndexError:
        return ''
    return '' if v is None else v


def _company_col(headers, colmap):
    """عمود «الشركة» (اختياري) — مش من أعمدة استيراد السيارات."""
    used = set(colmap.values())
    for i, h in enumerate(headers or []):
        h = str(h or '').strip().lower()
        if i not in used and (h == 'company' or 'الشركة' in h or 'شركة' == h):
            return i
    return None


def import_print_parties(kind: str, dataset) -> dict:
    """kind: 'customer' أو 'supplier'. بيرجّع إحصائيات + أول ٢٠ خطأ."""
    from printing.models import PrintCustomer, PrintSupplier
    model = PrintCustomer if kind == 'customer' else PrintSupplier
    headers = dataset.headers or []
    colmap = resolve_columns(headers)
    colmap.pop('tax_id', None)
    if 'name' not in colmap and 'phone' not in colmap:
        raise ValueError('مقدرتش ألاقي عمود الاسم أو الموبايل في الملف.')
    company_idx = _company_col(headers, colmap)

    stats = {'created': 0, 'updated': 0, 'balances': 0, 'total_balance': Decimal('0'), 'errors': []}
    for i, row in enumerate(dataset, start=2):
        name = str(_cell(row, colmap, 'name')).strip()
        phone = clean_phone(_cell(row, colmap, 'phone'))
        company = ''
        if company_idx is not None and company_idx < len(row):
            company = str(row[company_idx] or '').strip()
        if not name and not phone:
            continue
        try:
            with transaction.atomic():
                balance = parse_amount(_cell(row, colmap, 'balance'))
                party = model.objects.filter(phone=phone).first() if phone else None
                if party is None and name:
                    party = model.objects.filter(name__iexact=name).first()
                is_new = party is None
                if is_new:
                    if not name:
                        raise ValueError('الاسم مطلوب')
                    party = model(name=name[:150], phone=phone[:20], whatsapp=phone[:20],
                                  opening_date=timezone.localdate())
                else:
                    if name:
                        party.name = name[:150]
                    if phone and not party.phone:
                        party.phone = phone[:20]
                        party.whatsapp = party.whatsapp or phone[:20]
                if company:
                    party.company = company[:150]
                old = Decimal(str(party.opening_balance or 0))
                if balance is not None:
                    party.opening_balance = balance
                party.full_clean()
                party.save()            # الإشارة بتزامن قيد الرصيد الافتتاحي
                if balance is not None and balance != old:
                    stats['balances'] += 1
                if balance is not None:
                    stats['total_balance'] += balance
                stats['created' if is_new else 'updated'] += 1
        except Exception as exc:  # noqa: BLE001 — الصف يتسجّل غلط وباقي الملف يكمل
            msg = '; '.join(getattr(exc, 'messages', []) or [str(exc)])
            stats['errors'].append(f'صف {i} ({name or phone}): {msg}')
    stats['error_count'] = len(stats['errors'])
    stats['errors'] = stats['errors'][:20]
    return stats
