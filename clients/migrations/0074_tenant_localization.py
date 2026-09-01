# 🌍 توطين المستأجر — إضافة حقول الدولة/العملة/الضريبة/التوقيت/اللغة.
# additive + defaulted ⇒ آمن على الصفوف الموجودة (كلها تبقى EGP/مصر افتراضياً).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0073_rename_clients_bro_user_id_campaign_idx_clients_bro_user_id_52d88f_idx'),
    ]

    operations = [
        migrations.AddField(
            model_name='client',
            name='country',
            field=models.CharField(db_index=True, default='EG', help_text='تحدد العملة والضريبة والتوقيت الافتراضية للمستأجر', max_length=2, verbose_name='الدولة'),
        ),
        migrations.AddField(
            model_name='client',
            name='currency',
            field=models.CharField(blank=True, default='', help_text='اتركها فارغة لاشتقاقها من الدولة (EGP/AED/SAR...)', max_length=3, verbose_name='العملة'),
        ),
        migrations.AddField(
            model_name='client',
            name='vat_rate',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='اتركها فارغة لاستخدام النسبة القانونية للدولة', max_digits=5, null=True, verbose_name='نسبة ضريبة القيمة المضافة %'),
        ),
        migrations.AddField(
            model_name='client',
            name='timezone',
            field=models.CharField(blank=True, default='', help_text='اتركها فارغة لاشتقاقها من الدولة (Africa/Cairo, Asia/Dubai...)', max_length=40, verbose_name='المنطقة الزمنية'),
        ),
        migrations.AddField(
            model_name='client',
            name='default_language',
            field=models.CharField(blank=True, default='', help_text='ar أو en — اتركها فارغة لاشتقاقها من الدولة', max_length=5, verbose_name='اللغة الافتراضية'),
        ),
    ]
