# 🚢 PurchaseInvoice.cost_allocation: توزيع مصاريف الوصول على الأصناف
#   بالقيمة (نسبة ثابتة — زي ما كان) أو حسب ربح كل صنف (البيع − الشراء).
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0058_merge_duplicate_expense_categories"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaseinvoice",
            name="cost_allocation",
            field=models.CharField(
                choices=[("value", "بالقيمة (نسبة ثابتة)"), ("margin", "حسب ربح كل صنف (البيع − الشراء)")],
                default="value", max_length=10, verbose_name="توزيع مصاريف الوصول"),
        ),
    ]
