# 🌍 اسم الباقة بالإنجليزية للموقع الإنجليزي. additive وآمن (default '').

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0076_client_tax_registration_number'),
    ]

    operations = [
        migrations.AddField(
            model_name='plan',
            name='name_en',
            field=models.CharField(
                blank=True, default='', max_length=80,
                help_text='يُعرض على الموقع الإنجليزي — اتركه فارغاً لاستخدام الاسم العربي',
                verbose_name='اسم الباقة (إنجليزي)',
            ),
        ),
    ]
