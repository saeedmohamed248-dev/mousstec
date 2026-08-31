# 🇦🇪 سعر الباقة بالدرهم للموقع الإماراتي — additive وآمن (default 0 = fallback مصري).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0074_tenant_localization'),
    ]

    operations = [
        migrations.AddField(
            model_name='plan',
            name='monthly_price_aed',
            field=models.DecimalField(
                max_digits=10, decimal_places=2, default=0,
                help_text='سعر الدرهم للموقع الإماراتي. اتركه 0 لاستخدام السعر المصري',
                verbose_name='السعر الشهري (د.إ) — الموقع الإماراتي',
            ),
        ),
    ]
