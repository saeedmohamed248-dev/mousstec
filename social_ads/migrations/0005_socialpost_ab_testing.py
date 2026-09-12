from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("social_ads", "0004_strategymemory_angle_sales"),
    ]

    operations = [
        migrations.AddField(
            model_name="socialpost",
            name="experiment_id",
            field=models.CharField(
                blank=True, db_index=True, default="", max_length=40,
                verbose_name="معرّف تجربة A/B",
            ),
        ),
        migrations.AddField(
            model_name="socialpost",
            name="variant",
            field=models.CharField(
                blank=True, default="", max_length=1,
                verbose_name="نسخة التجربة (A/B)",
            ),
        ),
        migrations.AddField(
            model_name="socialpost",
            name="ab_winner",
            field=models.BooleanField(
                default=False, verbose_name="النسخة الفائزة في التجربة؟",
            ),
        ),
    ]
