from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omnichannel', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenantchannelconfig',
            name='subscription_started_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='تاريخ بدء الاشتراك'),
        ),
        migrations.AddField(
            model_name='tenantchannelconfig',
            name='subscription_expires_at',
            field=models.DateTimeField(blank=True, help_text='اتركه فارغاً مع تفعيل الاشتراك للوصول مدى الحياة.', null=True, verbose_name='تاريخ انتهاء الاشتراك'),
        ),
    ]
