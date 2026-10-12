# ✍️ PurchaseInvoiceItem.manual_extra_unit: مصاريف القطعة متحدّدة يدوي على بند
#   (فاضي = تلقائي) — والباقي من مصاريف الفاتورة بيتوزّع على باقي البنود.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0059_purchase_cost_allocation"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaseinvoiceitem",
            name="manual_extra_unit",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True,
                                      verbose_name="مصاريف القطعة (يدوي)"),
        ),
    ]
