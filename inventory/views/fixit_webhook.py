"""
🛒 استقبال طلبات موقع FixIt الإلكتروني
=======================================
لما عميل يعمل أوردر على الموقع، الموقع يبعت الطلب هنا فيتسجل
كفاتورة بيع (مسودة/عرض سعر) باسم العميل — تراجعها وتعتمدها من الداشبورد،
وأول ما تعتمدها المخزون يتخصم هنا ويتبعت تلقائياً للموقع (signal المزامنة).

الرابط: POST /inventory/webhooks/fixit/order/
الحماية: هيدر X-Sync-Secret لازم يساوي FIXIT_SYNC_SECRET.
"""
import json
import logging
import os

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from ..models import Branch, Customer, Product, SaleInvoice, SaleInvoiceItem

logger = logging.getLogger('mouss_tec_core')


def _secret():
    return getattr(settings, 'FIXIT_SYNC_SECRET', None) or os.environ.get('FIXIT_SYNC_SECRET')


@csrf_exempt
@require_POST
def fixit_order_webhook(request):
    secret = _secret()
    if not secret or request.headers.get('X-Sync-Secret') != secret:
        return JsonResponse({'error': 'unauthorized'}, status=401)

    try:
        order = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'error': 'invalid json'}, status=400)

    number = str(order.get('number') or '').strip()
    customer_data = order.get('customer') or {}
    items = order.get('items') or []
    if not number or not customer_data.get('phone') or not items:
        return JsonResponse({'error': 'missing number/customer/items'}, status=400)

    # Idempotency: نفس الطلب ميتسجلش مرتين
    marker = f"[FixIt {number}]"
    if SaleInvoice.objects.filter(notes__contains=marker).exists():
        return JsonResponse({'ok': True, 'duplicate': True})

    branch_id = getattr(settings, 'FIXIT_BRANCH_ID', None) or os.environ.get('FIXIT_BRANCH_ID')
    branch = Branch.objects.filter(pk=branch_id).first() if branch_id else Branch.objects.first()
    if not branch:
        return JsonResponse({'error': 'no branch configured'}, status=500)

    with transaction.atomic():
        customer, _ = Customer.objects.get_or_create(
            phone=str(customer_data['phone']).strip(),
            defaults={'name': customer_data.get('name') or 'عميل الموقع'},
        )

        missing = []
        note_lines = [
            f"{marker} طلب من الموقع الإلكتروني 🛒",
            f"الدفع: {order.get('payment', 'cod')} — الإجمالي بالموقع: {order.get('total', '?')} ج.م",
            f"العنوان: {customer_data.get('city', '')} {customer_data.get('address', '')}".strip(),
        ]
        if order.get('coupon'):
            note_lines.append(f"كوبون: {order['coupon']} (خصم {order.get('discount', 0)} ج.م)")
        if order.get('notes'):
            note_lines.append(f"ملاحظات العميل: {order['notes']}")

        invoice = SaleInvoice.objects.create(
            invoice_type='sale',
            status='quotation',  # مسودة — تُعتمد يدوياً بعد المراجعة فيخصم المخزون
            customer=customer,
            branch=branch,
            notes='',  # بتتحدث تحت بعد معرفة النواقص
        )

        for line in items:
            product = Product.objects.filter(part_number=str(line.get('sku') or '').strip()).first()
            if not product:
                missing.append(f"{line.get('name', '?')} ({line.get('sku', '?')}) ×{line.get('qty', 1)}")
                continue
            SaleInvoiceItem.objects.create(
                invoice=invoice,
                product=product,
                quantity=int(line.get('qty') or 1),
                unit_price=line.get('price') or product.retail_price,
            )

        if missing:
            note_lines.append("⚠️ قطع مش متسجلة بنفس الـ SKU هنا: " + " | ".join(missing))
        invoice.notes = "\n".join(note_lines)
        invoice.save(update_fields=['notes'])
        invoice.update_total()

    logger.info("FixIt order %s received as invoice #%s (%d items, %d missing)",
                number, invoice.pk, len(items) - len(missing), len(missing))
    return JsonResponse({'ok': True, 'invoice_id': invoice.pk, 'missing_skus': missing})
