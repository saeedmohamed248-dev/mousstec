from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omnichannel', '0006_multi_number'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenantchannelconfig',
            name='instagram_account_id',
            field=models.CharField(blank=True, db_index=True, default='', help_text='معرّف حساب إنستجرام المرتبط بالصفحة — لتوجيه رسائل إنستجرام.', max_length=64, verbose_name='Instagram Account ID'),
        ),
        migrations.AddField(
            model_name='tenantchannelconfig',
            name='instagram_enabled',
            field=models.BooleanField(default=True, verbose_name='قناة إنستجرام مفعّلة؟'),
        ),
        migrations.AddField(
            model_name='tenantchannelconfig',
            name='web_widget_enabled',
            field=models.BooleanField(default=False, verbose_name='شات الموقع الإلكتروني مفعّل؟'),
        ),
        migrations.AddField(
            model_name='tenantchannelconfig',
            name='web_widget_key',
            field=models.CharField(blank=True, db_index=True, default='', help_text='مفتاح عام يُضمَّن في كود الـ Widget على موقع الشركة.', max_length=48, verbose_name='مفتاح شات الموقع'),
        ),
        migrations.AddField(
            model_name='tenantchannelnumber',
            name='instagram_account_id',
            field=models.CharField(blank=True, db_index=True, default='', max_length=64, verbose_name='Instagram Account ID'),
        ),
        migrations.AlterField(
            model_name='tenantchannelnumber',
            name='channel',
            field=models.CharField(choices=[('whatsapp', 'WhatsApp'), ('messenger', 'Messenger'), ('instagram', 'Instagram')], default='whatsapp', max_length=16),
        ),
        migrations.AlterField(
            model_name='channelmessagelog',
            name='channel',
            field=models.CharField(choices=[('whatsapp', 'WhatsApp'), ('messenger', 'Messenger'), ('instagram', 'Instagram'), ('website', 'Website')], max_length=16),
        ),
    ]
