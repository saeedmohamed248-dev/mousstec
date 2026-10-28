"""🎁 نقاط الولاء من الفواتير نفسها (تصحيح البيانات القديمة).

نقطة البيع وأمر الشغل كانوا بيعتمدوا الفاتورة وهي لسه فاضية، فالعميل ماكانش
بياخد نقاط على أغلب مشترياته، والمرتجع/حذف الفاتورة ماكانوش بيشيلوا النقاط.
القاعدة (زي InvoiceService.recalc_loyalty_points): نقطة لكل 100 من كل فاتورة بيع
معتمدة فيها أصناف مفوترة، ناقص نفس الحساب لمرتجعاتها — ومابتنزلش تحت الصفر.
"""
from decimal import Decimal

from django.db import migrations
from django.db.models import Exists, OuterRef


def recalc(apps, schema_editor):
    Customer = apps.get_model('inventory', 'Customer')
    SaleInvoice = apps.get_model('inventory', 'SaleInvoice')
    SaleInvoiceItem = apps.get_model('inventory', 'SaleInvoiceItem')
    billable = SaleInvoiceItem.objects.filter(invoice=OuterRef('pk'), is_billable=True)
    net = {}
    rows = (SaleInvoice.objects.filter(status='posted', total_amount__gt=0)
            .filter(Exists(billable)).values_list('customer_id', 'total_amount', 'is_return'))
    for cid, total, is_ret in rows.iterator():
        pts = int(Decimal(str(total)) / Decimal('100.0'))
        net[cid] = net.get(cid, 0) + (-pts if is_ret else pts)
    # «عميل نقدي» (مشترك بين البيع الكاش) مالوش نقاط — رقمه بعد التطبيع زي Customer.normalize_phone
    walk_in_ids = set(Customer.objects.filter(phone__in=('0000000000', '+200000000000', '+00000000'))
                      .values_list('pk', flat=True))
    for cid, current in Customer.objects.values_list('pk', 'loyalty_points').iterator():
        if cid in walk_in_ids:
            continue
        target = max(net.get(cid, 0), 0)
        if current != target:
            Customer.objects.filter(pk=cid).update(loyalty_points=target)


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0064_vat_payment_kind"),
    ]

    operations = [
        migrations.RunPython(recalc, migrations.RunPython.noop),
    ]
