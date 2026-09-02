from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omnichannel', '0004_tenantchannelconfig_standalone_mode'),
    ]

    operations = [
        migrations.AddField(
            model_name='channelmessagelog',
            name='is_human',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='tenantchannelconfig',
            name='notify_on_handoff',
            field=models.BooleanField(
                default=False,
                help_text='يُرسَل إيميل عند تعذّر رد المساعد آلياً (يحتاج ضبط SMTP).',
                verbose_name='تنبيهي عند حاجة العميل لتدخّل بشري؟',
            ),
        ),
        migrations.AddField(
            model_name='tenantchannelconfig',
            name='notify_email',
            field=models.EmailField(
                blank=True, default='', max_length=254,
                help_text='اتركه فارغاً لاستخدام بريد إدارة الشركة.',
                verbose_name='بريد الإشعارات',
            ),
        ),
    ]
