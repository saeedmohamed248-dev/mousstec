from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("social_ads", "0002_socialpost_source_imported"),
    ]

    operations = [
        migrations.AddField(
            model_name="socialpost",
            name="product_sku",
            field=models.CharField(
                blank=True, db_index=True, default="", max_length=100,
                verbose_name="كود القطعة المروَّجة (SKU)",
            ),
        ),
        migrations.AddField(
            model_name="socialpost",
            name="product_name",
            field=models.CharField(
                blank=True, default="", max_length=200,
                verbose_name="اسم القطعة المروَّجة",
            ),
        ),
        migrations.AddField(
            model_name="socialpost",
            name="attributed_sales_count",
            field=models.PositiveIntegerField(
                default=0, verbose_name="مبيعات منسوبة للبوست",
            ),
        ),
        migrations.AddField(
            model_name="socialpost",
            name="attributed_sales_value",
            field=models.DecimalField(
                decimal_places=2, default=0, max_digits=12,
                verbose_name="قيمة المبيعات المنسوبة",
            ),
        ),
    ]
