from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0074_saved_card'),
    ]

    operations = [
        migrations.CreateModel(
            name='PushSubscription',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('endpoint', models.URLField(max_length=500, unique=True, verbose_name='Push Endpoint')),
                ('p256dh', models.CharField(max_length=255, verbose_name='مفتاح P-256')),
                ('auth', models.CharField(max_length=255, verbose_name='Auth Secret')),
                ('tenant_schema', models.CharField(blank=True, db_index=True, default='', max_length=63, verbose_name='Schema')),
                ('user_id', models.IntegerField(blank=True, null=True, verbose_name='User ID (داخل الـ schema)')),
                ('user_agent', models.CharField(blank=True, max_length=255)),
                ('is_active', models.BooleanField(db_index=True, default=True, verbose_name='نشط')),
                ('failure_count', models.PositiveIntegerField(default=0, verbose_name='مرات الفشل المتتالية')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('last_success_at', models.DateTimeField(blank=True, null=True, verbose_name='آخر إرسال ناجح')),
                ('marketplace_customer', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='push_subscriptions',
                    to='clients.marketplacecustomer',
                    verbose_name='عميل السوق',
                )),
            ],
            options={
                'verbose_name': 'اشتراك إشعارات',
                'verbose_name_plural': '🔔 اشتراكات الإشعارات (Web Push)',
            },
        ),
        migrations.AddIndex(
            model_name='pushsubscription',
            index=models.Index(fields=['tenant_schema', 'user_id'], name='clients_pus_tenant__idx'),
        ),
    ]
