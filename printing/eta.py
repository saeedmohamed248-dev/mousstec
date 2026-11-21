"""🧾 الفاتورة الإلكترونية (ETA) لفواتير المطابع — نفس صيغة inventory/services/eta_einvoice.py.

🆕 الفاتورة الإلكترونية كانت لقطاع العربيات بس (SaleInvoice) — المطبعة اللي بتشتغل مع شركات
مكانتش بتعرف تطلّع ملف للبوابة. هنا build_document(order) بيرجّع (document, errors) لطلب اتسلّم:
* البنود = مهام الطلب (الكمية × النسخ)، وكود كل بند من «نوع البند» أو «كود المصنعية العامة».
* الضريبة على الطلب مبلغ (tax_amount) ⇒ بنحوّلها نسبة على الصافي (14٪ عادةً) ونتأكد إن الإجمالي مطابق.
* المستقبل: شركة (رقم ضريبي + عنوان كامل) أو فرد (رقم قومي لو ٥٠ ألف أو أكتر).
الإعدادات (RIN، كود النشاط، عنوان المطبعة) هي نفسها ETASettings بتاعة الشركة.
"""
from datetime import timezone as dt_timezone
from decimal import Decimal

from inventory.services.eta_einvoice import (
    D0, PERSON_ID_THRESHOLD, TOTAL_TOLERANCE, _address, _allocate_discount, _code, _q,
)

ADDR_KEYS = ('governate', 'regionCity', 'street', 'buildingNumber')


def _lines(order, settings, errors):
    lines = []
    for job in order.jobs.select_related('product_type').order_by('pk'):
        if not job.total_price:
            continue
        code = (job.product_type.eta_item_code if job.product_type_id else '') or settings.service_item_code
        if not code:
            errors.append(f"البند «{job.description[:60]}» مالوش كود، ومفيش «كود المصنعية العامة» في الإعدادات.")
        qty = Decimal(job.quantity) * Decimal(job.copies or 1)
        lines.append({'description': job.description, 'internal': f"JOB-{job.pk}", 'code': code,
                      'qty': qty, 'unit': Decimal(job.unit_price), 'discount': D0})
    if not lines and order.total_amount:
        if not settings.service_item_code:
            errors.append("الطلب مالوش بنود، ومفيش «كود المصنعية العامة» في الإعدادات.")
        lines.append({'description': f"أعمال طباعة — طلب {order.order_number}", 'internal': order.order_number,
                      'code': settings.service_item_code, 'qty': Decimal('1'),
                      'unit': Decimal(order.total_amount), 'discount': D0})
    return lines


def build_document(order, settings=None):
    from inventory.models import ETASettings
    settings = settings or ETASettings.load()
    errors = []
    if not settings.taxpayer_rin:
        errors.append("رقم التسجيل الضريبي للشركة (RIN) مش متسجّل في إعدادات الفاتورة الإلكترونية.")
    if not settings.activity_code:
        errors.append("كود النشاط مش متسجّل في إعدادات الفاتورة الإلكترونية.")
    issuer_addr = _address(settings)
    if not all(issuer_addr[k] for k in ADDR_KEYS):
        errors.append("عنوان الشركة (محافظة/مدينة/شارع/رقم مبنى) ناقص في إعدادات الفاتورة الإلكترونية.")
    if order.status != 'delivered':
        errors.append("الفاتورة لسه ماتسلّمتش — الفاتورة الإلكترونية بتطلع بعد التسليم.")

    lines = _lines(order, settings, errors)
    if not lines:
        errors.append("الفاتورة مافيهاش بنود.")
    _allocate_discount(lines, Decimal(order.discount or 0))
    net_before_tax = sum((_q(l['qty'] * l['unit']) - _q(l['discount']) for l in lines), D0)
    tax_amount = Decimal(order.tax_amount or 0)
    rate = (tax_amount * 100 / net_before_tax).quantize(Decimal('0.01')) if net_before_tax > 0 and tax_amount else D0

    inv_lines, sales_total, disc_total, net_total, tax_total = [], D0, D0, D0, D0
    for l in lines:
        sales = _q(l['qty'] * l['unit'])
        disc = _q(l['discount'])
        net = sales - disc
        tax = _q(net * rate / Decimal('100'))
        item_type, item_code = _code(l['code'])
        inv_lines.append({
            'description': l['description'], 'itemType': item_type, 'itemCode': item_code,
            'internalCode': l['internal'], 'unitType': 'EA', 'quantity': float(l['qty']),
            'unitValue': {'currencySold': 'EGP', 'amountEGP': float(_q(l['unit']))},
            'salesTotal': float(sales), 'discount': {'rate': 0, 'amount': float(disc)},
            'netTotal': float(net),
            'taxableItems': [{'taxType': 'T1', 'subType': 'V009', 'rate': float(rate), 'amount': float(tax)}]
            if rate > 0 else [],
            'itemsDiscount': 0, 'valueDifference': 0, 'totalTaxableFees': 0, 'total': float(net + tax),
        })
        sales_total += sales
        disc_total += disc
        net_total += net
        tax_total += tax
    total = net_total + tax_total

    cust = order.customer
    if cust.tax_id:
        receiver = {'type': 'B', 'id': cust.tax_id, 'name': cust.company or cust.name,
                    'address': _address(cust, 'eta_')}
        if not all(receiver['address'][k] for k in ADDR_KEYS):
            errors.append(f"عنوان العميل «{cust.name}» ناقص (العميل الشركة لازم عنوانه كامل).")
    else:
        receiver = {'type': 'P', 'id': cust.national_id or '', 'name': cust.name,
                    'address': {'country': 'EG', 'governate': '', 'regionCity': '', 'street': '', 'buildingNumber': ''}}
        if total >= PERSON_ID_THRESHOLD and not cust.national_id:
            errors.append("فاتورة فرد بـ ٥٠ ألف أو أكتر لازم يتسجّل فيها الرقم القومي للعميل.")
    if lines and abs(total - Decimal(order.net_total)) > TOTAL_TOLERANCE:
        errors.append(f"إجمالي الفاتورة الإلكترونية ({total:,.2f}) مختلف عن إجمالي الفاتورة ({order.net_total:,.2f}).")

    issued_at = order.date_delivered or order.date_created
    document = {
        'issuer': {'type': 'B', 'id': settings.taxpayer_rin, 'name': settings.taxpayer_name or '',
                   'address': dict(issuer_addr, branchID=settings.branch_id or '0')},
        'receiver': receiver,
        'documentType': 'i', 'documentTypeVersion': '1.0',
        'dateTimeIssued': issued_at.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'taxpayerActivityCode': settings.activity_code,
        'internalID': order.order_number,
        'invoiceLines': inv_lines,
        'totalSalesAmount': float(sales_total), 'totalDiscountAmount': float(disc_total),
        'netAmount': float(net_total),
        'taxTotals': [{'taxType': 'T1', 'amount': float(tax_total)}] if rate > 0 else [],
        'extraDiscountAmount': 0, 'totalItemsDiscountAmount': 0, 'totalAmount': float(total),
    }
    return document, errors
