# 🧾 الرقم الضريبي (TRN) للمستأجر — للفاتورة الضريبية. additive وآمن.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0075_plan_monthly_price_aed'),
    ]

    operations = [
        migrations.AddField(
            model_name='client',
            name='tax_registration_number',
            field=models.CharField(
                blank=True, default='', max_length=30,
                help_text='رقم التسجيل الضريبي — يظهر على الفاتورة الضريبية (إلزامي في الإمارات/السعودية)',
                verbose_name='الرقم الضريبي (TRN)',
            ),
        ),
    ]
