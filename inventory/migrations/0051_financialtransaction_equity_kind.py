from django.db import migrations, models


# 💼 حركة حقوق ملكية على الخزنة: رأس مال (إيداع مالك) / مسحوبات — بتتقيّد على
#    حساب رأس المال (٣٠٠١) بدل الإيرادات/المصروفات فمتأثّرش على الأرباح.
class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0050_purchase_landed_cost"),
    ]

    operations = [
        migrations.AddField(
            model_name="financialtransaction",
            name="equity_kind",
            field=models.CharField(
                blank=True,
                choices=[("capital", "رأس مال / إيداع مالك"), ("drawings", "مسحوبات المالك")],
                default="",
                max_length=12,
                verbose_name="حركة حقوق ملكية",
            ),
        ),
        migrations.AddField(
            model_name="historicalfinancialtransaction",
            name="equity_kind",
            field=models.CharField(
                blank=True,
                choices=[("capital", "رأس مال / إيداع مالك"), ("drawings", "مسحوبات المالك")],
                default="",
                max_length=12,
                verbose_name="حركة حقوق ملكية",
            ),
        ),
    ]
