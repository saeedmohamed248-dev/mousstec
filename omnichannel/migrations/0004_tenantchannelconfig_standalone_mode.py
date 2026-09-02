from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omnichannel', '0003_channelmessagelog_contact_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenantchannelconfig',
            name='standalone_mode',
            field=models.BooleanField(
                default=False,
                help_text='عند التفعيل، يهبط مستخدمو هذه الشركة على لوحة تحكم الأتمتة مباشرة بعد الدخول بدلاً من بوابة النظام الكامل.',
                verbose_name='شركة أتمتة فقط (بدون ERP)',
            ),
        ),
    ]
