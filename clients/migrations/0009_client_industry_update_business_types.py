"""
Add industry field and new printing business types to Client model.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0008_update_plan_names_silver_gold_empire'),
    ]

    operations = [
        migrations.AddField(
            model_name='client',
            name='industry',
            field=models.CharField(
                choices=[
                    ('automotive', '🚗 سيارات — صيانة وقطع غيار'),
                    ('printing', '🎨 طباعة وتصميم جرافيك'),
                ],
                default='automotive',
                max_length=20,
                verbose_name='القطاع',
            ),
        ),
        migrations.AlterField(
            model_name='client',
            name='business_type',
            field=models.CharField(
                choices=[
                    ('service_center', 'مركز صيانة متكامل'),
                    ('parts_dealer', 'تاجر قطع غيار (مبيعات تجزئة وجملة)'),
                    ('scrap_importer', 'مستورد تقطيع وأنصاف (محرك الـ Scrap)'),
                    ('both', 'توكيل شامل (صيانة + تجارة + استيراد)'),
                    ('print_shop', 'مطبعة (طباعة رقمية وأوفست)'),
                    ('design_studio', 'استوديو تصميم جرافيك'),
                    ('print_and_design', 'مطبعة + تصميم (شامل)'),
                ],
                default='service_center',
                max_length=20,
                verbose_name='نوع النشاط',
            ),
        ),
    ]
