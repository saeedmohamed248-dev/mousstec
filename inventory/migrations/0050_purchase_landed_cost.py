from decimal import Decimal

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


# 🚢 تكلفة الوصول + الدورة المالية الكاملة لمصاريف الشحنة على فواتير الشراء:
#   - PurchaseInvoiceItem.landed_unit_cost: تكلفة الوصول للوحدة (تتخزّن وقت الاعتماد)
#   - PurchaseInvoiceExtraCost: بند مصاريف شحنة (جمارك/شحن/سفر/إعاشة…) بسلوك
#     محاسبي (على التكلفة أو مصروف) وخزنة دفع اختيارية
#   - FinancialTransaction.purchase_extra_cost: ربط حركة الصرف بالبند لتوجيه القيد
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
                            ("travel", "سفر وتنقلات"),
                            ("food", "إعاشة / أكل"),
                            ("other", "مصاريف أخرى"),
                        ],
                        default="shipping",
                        max_length=20,
                        verbose_name="نوع المصروف",
                    ),
                ),
                (
                    "behavior",
                    models.CharField(
                        blank=True,
                        choices=[("landed", "على تكلفة المخزون"), ("expense", "مصروف على الفترة")],
                        default="",
                        help_text="على تكلفة المخزون (Landed) أو مصروف على الفترة",
                        max_length=10,
                        verbose_name="المعالجة المحاسبية",
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
                (
                    "treasury",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="inventory.treasury",
                        help_text="لو فاضي بيتسجّل كمستحق (آجل) بدل صرف نقدي فوري",
                        verbose_name="مدفوع من خزنة",
                    ),
                ),
                (
                    "expense_category",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="inventory.expensecategory",
                        verbose_name="بند المصروف (للنوع مصروف)",
                    ),
                ),
            ],
            options={
                "verbose_name": "بند مصاريف شحنة",
                "verbose_name_plural": "🚢 مصاريف الشحنة (جمارك/شحن/سفر/إعاشة)",
            },
        ),
        migrations.AddField(
            model_name="financialtransaction",
            name="purchase_extra_cost",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="payments",
                to="inventory.purchaseinvoiceextracost",
                verbose_name="بند مصاريف شحنة",
            ),
        ),
        migrations.AddField(
            model_name="historicalfinancialtransaction",
            name="purchase_extra_cost",
            field=models.ForeignKey(
                blank=True,
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.DO_NOTHING,
                related_name="+",
                to="inventory.purchaseinvoiceextracost",
                verbose_name="بند مصاريف شحنة",
            ),
        ),
    ]
