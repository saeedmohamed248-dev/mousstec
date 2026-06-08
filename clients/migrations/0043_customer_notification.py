from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0042_obd_device_security'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CustomerNotification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=200, verbose_name='العنوان')),
                ('body', models.TextField(verbose_name='النص')),
                ('level', models.CharField(choices=[('info', 'معلومة'), ('success', 'نجاح / هدية'), ('warning', 'تنبيه'), ('danger', 'تحذير')], default='info', max_length=10)),
                ('icon', models.CharField(blank=True, default='fa-bell', help_text='Font Awesome class, e.g. fa-gift', max_length=50)),
                ('action_url', models.CharField(blank=True, help_text='رابط اختياري للعميل ينقر عليه', max_length=300)),
                ('action_label', models.CharField(blank=True, max_length=80)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('read_at', models.DateTimeField(blank=True, null=True)),
                ('customer', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='notifications', to='clients.marketplacecustomer', verbose_name='العميل')),
                ('sent_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='sent_customer_notifications', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'إشعار عميل',
                'verbose_name_plural': '🔔 إشعارات العملاء',
                'ordering': ['-created_at'],
            },
        ),
    ]
