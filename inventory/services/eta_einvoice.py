"""
🧾 الفاتورة الإلكترونية (مصلحة الضرائب المصرية — ETA) — التجهيز والتحقق.

build_document(invoice) بيرجّع (document, errors):
* document: dict بصيغة ETA (documentTypeVersion 1.0) جاهز للتوقيع بالتوكن.
* errors: رسايل عربي بكل اللي ناقص (رقم ضريبي، كود نشاط، كود صنف، عنوان العميل…)
  — الفاتورة مش جاهزة للإرسال طول ما فيه أخطاء.

الحساب نفس SaleInvoice.update_total: الخصم الإجمالي بيتوزّع على السطور بالنسبة
(ETA بيحسب الضريبة على كل سطر بعد خصمه)، والضريبة T1/V009 بنسبة tax_percentage.
السطور اللي «مش بتتحاسب على العميل» (is_billable=False) مش بتدخل.

⚠️ الإرسال المباشر للبوابة (OAuth + submit) مش هنا — محتاج Client Secret والتوقيع
بفلاشة الـ USB Token عند كل شركة.
"""
from datetime import timezone as dt_timezone
from decimal import ROUND_HALF_UP, Decimal

D0 = Decimal('0.00')
CENT = Decimal('0.01')
PERSON_ID_THRESHOLD = Decimal('50000')  # فاتورة فرد ≥ ٥٠ ألف ⇒ الرقم القومي إلزامي
TOTAL_TOLERANCE = Decimal('0.05')


def _q(x):
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def _code(code):
    """(itemType, itemCode): GS1 لو باركود رقمي ١٣ خانة، وإلا EGS."""
    code = (code or '').strip()
    if code.isdigit() and len(code) in (8, 12, 13, 14):
        return 'GS1', code
    return 'EGS', code


def _raw_lines(invoice, settings, errors):
    """السطور قبل توزيع الخصم: (وصف، كود، كمية، سعر الوحدة، خصم السطر)."""
    lines = []
    for it in invoice.items.select_related('product').filter(is_billable=True).order_by('id'):
        p = it.product
        if not p.eta_item_code:
            errors.append(f"الصنف «{p.name}» ({p.part_number}) مالوش كود في الفاتورة الإلكترونية.")
        lines.append({'description': p.name, 'internal': p.part_number, 'code': p.eta_item_code,
                      'qty': Decimal(it.quantity), 'unit': Decimal(it.unit_price),
                      'discount': Decimal(it.discount or 0)})
        core = Decimal(it.core_charge_applied or 0)
        if core > 0 and not it.is_core_returned:
            lines.append({'description': f"تأمين كور — {p.name}", 'internal': p.part_number,
                          'code': p.eta_item_code, 'qty': Decimal(it.quantity), 'unit': core,
                          'discount': D0})
    for sv in invoice.service_items.select_related('service').filter(is_billable=True).order_by('id'):
        price = Decimal(sv.price or 0)
        if price <= 0:
            continue
        code = sv.service.eta_item_code or settings.service_item_code
        if not code:
            errors.append(f"الخدمة «{sv.service.name}» مالهاش كود، ومفيش «كود المصنعية العامة» في الإعدادات.")
        lines.append({'description': sv.service.name, 'internal': f"SRV-{sv.service_id}", 'code': code,
                      'qty': Decimal('1'), 'unit': price, 'discount': D0})
    labor = Decimal(invoice.labor_cost_manual or 0)
    if labor > 0:
        if not settings.service_item_code:
            errors.append("الفاتورة فيها مصنعية إضافية ومفيش «كود المصنعية العامة» في الإعدادات.")
        lines.append({'description': "مصنعية", 'internal': "LABOR", 'code': settings.service_item_code,
                      'qty': Decimal('1'), 'unit': labor, 'discount': D0})
    return lines


def _allocate_discount(lines, discount):
    """الخصم الإجمالي على السطور بالنسبة لصافيها، والفرق من التقريب على آخر سطر."""
    if discount <= 0 or not lines:
        return
    nets = [_q(l['qty'] * l['unit']) - l['discount'] for l in lines]
    base = sum(nets, D0)
    if base <= 0:
        return
    left = discount
    for i, (line, net) in enumerate(zip(lines, nets)):
        share = left if i == len(lines) - 1 else _q(discount * net / base)
        share = min(share, net)
        line['discount'] += share
        left -= share


def _address(obj, prefix=''):
    return {
        'country': 'EG',
        'governate': getattr(obj, f'{prefix}governate', '') or '',
        'regionCity': getattr(obj, f'{prefix}region_city', '') or '',
        'street': getattr(obj, f'{prefix}street', '') or '',
        'buildingNumber': getattr(obj, f'{prefix}building_number', '') or '',
    }


def build_document(invoice, settings=None):
    from inventory.models import ETASettings
    settings = settings or ETASettings.load()
    errors = []

    if not settings.taxpayer_rin:
        errors.append("رقم التسجيل الضريبي للشركة (RIN) مش متسجّل في إعدادات الفاتورة الإلكترونية.")
    if not settings.activity_code:
        errors.append("كود النشاط مش متسجّل في إعدادات الفاتورة الإلكترونية.")
    issuer_addr = _address(settings)
    if not all(issuer_addr[k] for k in ('governate', 'regionCity', 'street', 'buildingNumber')):
        errors.append("عنوان الشركة (محافظة/مدينة/شارع/رقم مبنى) ناقص في إعدادات الفاتورة الإلكترونية.")
    if invoice.status != 'posted':
        errors.append("الفاتورة لسه مش معتمدة (تم التسليم).")

    rate = Decimal(invoice.tax_percentage or 0)
    lines = _raw_lines(invoice, settings, errors)
    if not lines:
        errors.append("الفاتورة مافيهاش سطور بتتحاسب على العميل.")
    _allocate_discount(lines, Decimal(invoice.discount or 0))

    inv_lines, sales_total, disc_total, net_total, tax_total = [], D0, D0, D0, D0
    for l in lines:
        sales = _q(l['qty'] * l['unit'])
        disc = _q(l['discount'])
        net = sales - disc
        tax = _q(net * rate / Decimal('100'))
        item_type, item_code = _code(l['code'])
        inv_lines.append({
            'description': l['description'],
            'itemType': item_type,
            'itemCode': item_code,
            'internalCode': l['internal'],
            'unitType': 'EA',
            'quantity': float(l['qty']),
            'unitValue': {'currencySold': 'EGP', 'amountEGP': float(_q(l['unit']))},
            'salesTotal': float(sales),
            'discount': {'rate': 0, 'amount': float(disc)},
            'netTotal': float(net),
            'taxableItems': [{'taxType': 'T1', 'subType': 'V009', 'rate': float(rate), 'amount': float(tax)}]
            if rate > 0 else [],
            'itemsDiscount': 0,
            'valueDifference': 0,
            'totalTaxableFees': 0,
            'total': float(net + tax),
        })
        sales_total += sales
        disc_total += disc
        net_total += net
        tax_total += tax
    total = net_total + tax_total

    cust = invoice.customer
    if cust.tax_id:
        receiver = {'type': 'B', 'id': cust.tax_id, 'name': cust.name, 'address': _address(cust, 'eta_')}
        if not all(receiver['address'][k] for k in ('governate', 'regionCity', 'street', 'buildingNumber')):
            errors.append(f"عنوان العميل «{cust.name}» ناقص (العميل الشركة لازم عنوانه كامل).")
    else:
        receiver = {'type': 'P', 'id': cust.national_id or '', 'name': invoice.customer_display_name,
                    'address': {'country': 'EG', 'governate': '', 'regionCity': '', 'street': '', 'buildingNumber': ''}}
        if total >= PERSON_ID_THRESHOLD and not cust.national_id:
            errors.append("فاتورة فرد بـ ٥٠ ألف أو أكتر لازم يتسجّل فيها الرقم القومي للعميل.")

    if invoice.is_return:
        errors.append("المرتجع بيتبعت «إشعار دائن» مربوط بـ UUID الفاتورة الأصلية على البوابة — "
                      "ابعت الفاتورة الأصلية الأول.")
    if lines and abs(total - Decimal(invoice.total_amount or 0)) > TOTAL_TOLERANCE:
        errors.append(f"إجمالي الفاتورة الإلكترونية ({total:,.2f}) مختلف عن إجمالي الفاتورة "
                      f"({Decimal(invoice.total_amount or 0):,.2f}).")

    issued = invoice.date_created.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    document = {
        'issuer': {'type': 'B', 'id': settings.taxpayer_rin, 'name': settings.taxpayer_name or '',
                   'address': dict(issuer_addr, branchID=settings.branch_id or '0')},
        'receiver': receiver,
        'documentType': 'c' if invoice.is_return else 'i',
        'documentTypeVersion': '1.0',
        'dateTimeIssued': issued,
        'taxpayerActivityCode': settings.activity_code,
        'internalID': str(invoice.pk),
        'invoiceLines': inv_lines,
        'totalSalesAmount': float(sales_total),
        'totalDiscountAmount': float(disc_total),
        'netAmount': float(net_total),
        'taxTotals': [{'taxType': 'T1', 'amount': float(tax_total)}] if rate > 0 else [],
        'extraDiscountAmount': 0,
        'totalItemsDiscountAmount': 0,
        'totalAmount': float(total),
    }
    return document, errors
