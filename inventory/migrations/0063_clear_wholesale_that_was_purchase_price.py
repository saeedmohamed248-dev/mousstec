# 🏷️ «سعر الجملة» على الأصناف اللي اترفعت من ملف فاتورة شراء كان فيه سعر الشراء
#   (عمود «سعر الجملة» بتاع الشركة اللي باعت = سعر الشراء عندنا) — فكان البيع للتجار
#   والشركات بيطلع بسعر الشراء. سعر بيع جملة مش أعلى من تكلفة القطعة مالوش معنى،
#   فبنمسحه: البيع للتجار يرجع بسعر البيع العادي لحد ما يتكتب سعر جملة صح.
from decimal import Decimal

from django.db import migrations
from django.db.models import Case, DecimalField, F, When


def clear(apps, schema_editor):
    Product = apps.get_model('inventory', 'Product')
    unit_cost = Case(When(average_cost__gt=0, then=F('average_cost')), default=F('purchase_price'),
                     output_field=DecimalField(max_digits=12, decimal_places=2))
    (Product.objects.filter(b2b_wholesale_price__gt=0).annotate(_uc=unit_cost)
     .filter(b2b_wholesale_price__lte=F('_uc')).update(b2b_wholesale_price=Decimal('0')))


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0062_backfill_contract_invoice_cogs"),
    ]

    operations = [
        migrations.RunPython(clear, migrations.RunPython.noop),
    ]
