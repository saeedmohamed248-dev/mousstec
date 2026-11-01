from django.db import migrations, models


def long_plans_only_for_launch_code(apps, schema_editor):
    """🎟️ كود الإطلاق MUSSTEC2026 (50% ليوم واحد): المالك طلب إنه يبقى للاشتراك 3 شهور أو 6 أو سنة
    بس — مش الشهري. لو الكود مش موجود (بيئة تانية) مفيش حاجة بتحصل."""
    Coupon = apps.get_model('clients', 'Coupon')
    Coupon.objects.filter(code='MUSSTEC2026').update(min_months=3)


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0098_signup_attribution'),
    ]

    operations = [
        migrations.AddField(
            model_name='coupon',
            name='min_months',
            field=models.PositiveSmallIntegerField(
                choices=[(1, 'أي مدة (حتى الشهري)'), (3, '3 شهور أو أكتر'), (6, '6 شهور أو أكتر'), (12, 'سنة')],
                default=1, help_text='الكود بيتطبّق بس لو العميل اشترك المدة دي أو أطول',
                verbose_name='أقل مدة اشتراك'),
        ),
        migrations.RunPython(long_plans_only_for_launch_code, migrations.RunPython.noop),
    ]
