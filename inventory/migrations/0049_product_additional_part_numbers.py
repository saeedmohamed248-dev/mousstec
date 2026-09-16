from django.db import migrations, models


# 🔢 بارت نمبرات إضافية لنفس القطعة (نفس المخزون/السعر) — القطعة الواحدة ممكن
#    يكون ليها أكتر من رقم بارت، والعميل يختار رقمه على الموقع.
class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0048_expand_part_category_choices"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="additional_part_numbers",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="أرقام بارت إضافية لنفس القطعة — العميل يختار رقمه على الموقع",
                verbose_name="بارت نمبرات إضافية",
            ),
        ),
        migrations.AddField(
            model_name="historicalproduct",
            name="additional_part_numbers",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="أرقام بارت إضافية لنفس القطعة — العميل يختار رقمه على الموقع",
                verbose_name="بارت نمبرات إضافية",
            ),
        ),
    ]
