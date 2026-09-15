from django.db import migrations, models


# 🏷️ توسعة تصنيفات القطعة لتطابق فئات موقع FixIt الـ9 (فرامل/تبريد/وقود/محرك/
#    فلاتر...) عشان القطعة تتحط في مكانها الصح على المتجر. القيم القديمة اتسابت
#    زي ما هي فمفيش أي بيانات بتتكسر — ده تغيير choices بس (مفيش تعديل داتا).
CHOICES = [
    ("electrical", "كهرباء وإشعال"),
    ("engine", "محرك"),
    ("mechanical", "ميكانيكا عامة"),
    ("brakes", "فرامل"),
    ("suspension", "عفشة وتعليق"),
    ("cooling", "تبريد"),
    ("fuel", "وقود"),
    ("filters", "فلاتر وصيانة"),
    ("body", "هيكل وإكسسوارات"),
    ("other", "أخرى"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0047_tag_salary_expense_categories"),
    ]

    operations = [
        migrations.AlterField(
            model_name="product",
            name="part_category",
            field=models.CharField(
                blank=True,
                choices=CHOICES,
                default="",
                max_length=20,
                verbose_name="تصنيف القطعة",
            ),
        ),
        migrations.AlterField(
            model_name="historicalproduct",
            name="part_category",
            field=models.CharField(
                blank=True,
                choices=CHOICES,
                default="",
                max_length=20,
                verbose_name="تصنيف القطعة",
            ),
        ),
    ]
