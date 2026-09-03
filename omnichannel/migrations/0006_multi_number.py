import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omnichannel', '0005_manual_reply_and_notifications'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenantchannelconfig',
            name='extra_numbers',
            field=models.PositiveSmallIntegerField(default=0, verbose_name='أرقام إضافية مشتراة'),
        ),
        migrations.CreateModel(
            name='TenantChannelNumber',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('label', models.CharField(blank=True, default='', max_length=120, verbose_name='اسم الرقم/الصفحة')),
                ('channel', models.CharField(choices=[('whatsapp', 'WhatsApp'), ('messenger', 'Messenger')], default='whatsapp', max_length=16)),
                ('whatsapp_phone_number_id', models.CharField(blank=True, db_index=True, default='', max_length=64, verbose_name='WhatsApp Phone Number ID')),
                ('whatsapp_business_account_id', models.CharField(blank=True, default='', max_length=64, verbose_name='WABA ID')),
                ('facebook_page_id', models.CharField(blank=True, db_index=True, default='', max_length=64, verbose_name='Facebook Page ID')),
                ('_meta_access_token', models.TextField(blank=True, db_column='meta_access_token_enc', default='')),
                ('_app_secret', models.TextField(blank=True, db_column='app_secret_enc', default='')),
                ('is_active', models.BooleanField(default=True, verbose_name='مفعّل؟')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('config', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='extra_channel_numbers', to='omnichannel.tenantchannelconfig')),
            ],
            options={
                'verbose_name': 'رقم/صفحة إضافية',
                'verbose_name_plural': 'أرقام/صفحات إضافية',
            },
        ),
    ]
