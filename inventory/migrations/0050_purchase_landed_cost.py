from decimal import Decimal

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


# 🚢 تكلفة الوصول (Landed Cost) على فواتير الشراء:
#   - بند مصاريف وصول لكل فاتورة (تحميل/جمارك/شحن/تأمين…) — PurchaseInvoiceExtraCost
#   - تكلفة الوصول للوحدة على كل بند شراء — بتتخزّن وقت الاعتماد عشان العكس يطابقه
class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0049_product_additional_part_numbers"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaseinvoiceitem",
            name="landed_unit_cost",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                max_digits=10,
                verbose_name="تكلفة الوصول للوحدة",
            ),
        ),
        migrations.CreateModel(
            name="PurchaseInvoiceExtraCost",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("loading", "تحميل"),
                            ("customs", "جمارك"),
                            ("shipping", "شحن"),
                            ("insurance", "تأمين"),
                            ("other", "مصاريف أخرى"),
                        ],
                        default="shipping",
                        max_length=20,
                        verbose_name="نوع المصروف",
                    ),
                ),
                ("label", models.CharField(blank=True, default="", max_length=120, verbose_name="وصف (اختياري)")),
                (
                    "amount",
                    models.DecimalField(
                        decimal_places=2,
                        default=Decimal("0.00"),
                        max_digits=12,
                        validators=[django.core.validators.MinValueValidator(Decimal("0.00"))],
                        verbose_name="المبلغ",
                    ),
                ),
                (
                    "invoice",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="extra_costs",
                        to="inventory.purchaseinvoice",
                    ),
                ),
            ],
            options={
                "verbose_name": "بند مصاريف وصول",
                "verbose_name_plural": "🚢 مصاريف الوصول (تحميل/جمارك/شحن)",
            },
        ),
    ]
