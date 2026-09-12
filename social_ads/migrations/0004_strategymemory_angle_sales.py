from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("social_ads", "0003_socialpost_product_linkage"),
    ]

    operations = [
        migrations.AddField(
            model_name="strategymemory",
            name="angle_sales",
            field=models.JSONField(
                blank=True, default=dict, verbose_name="مبيعات زوايا المحتوى",
            ),
        ),
    ]
