from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0044_historicalproduct_extra_attributes_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="historicalproduct",
            name="part_category",
            field=models.CharField(
                blank=True,
                choices=[
                    ("electrical", "كهربا"),
                    ("mechanical", "ميكانيكا"),
                    ("body", "فبر"),
                    ("suspension", "عفشة"),
                ],
                default="",
                max_length=20,
                verbose_name="تصنيف القطعة",
            ),
        ),
        migrations.AddField(
            model_name="product",
            name="part_category",
            field=models.CharField(
                blank=True,
                choices=[
                    ("electrical", "كهربا"),
                    ("mechanical", "ميكانيكا"),
                    ("body", "فبر"),
                    ("suspension", "عفشة"),
                ],
                default="",
                max_length=20,
                verbose_name="تصنيف القطعة",
            ),
        ),
        migrations.AddField(
            model_name="historicalproduct",
            name="description",
            field=models.TextField(
                blank=True, default="", verbose_name="وصف المنتج"
            ),
        ),
        migrations.AddField(
            model_name="product",
            name="description",
            field=models.TextField(
                blank=True, default="", verbose_name="وصف المنتج"
            ),
        ),
    ]
