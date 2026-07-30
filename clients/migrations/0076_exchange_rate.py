from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0075_push_subscription'),
    ]

    operations = [
        migrations.CreateModel(
            name='ExchangeRate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('target_currency', models.CharField(db_index=True, help_text='رمز ISO 4217 — USD, EUR, SAR…', max_length=3, verbose_name='العملة')),
                ('rate', models.DecimalField(decimal_places=8, max_digits=18, verbose_name='سعر الصرف (مقابل 1 EGP)')),
                ('source', models.CharField(blank=True, help_text='webhook / manual / api provider name', max_length=50, verbose_name='المصدر')),
                ('fetched_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now, verbose_name='وقت التحديث')),
            ],
            options={
                'verbose_name': 'سعر صرف',
                'verbose_name_plural': '💱 أسعار الصرف',
                'ordering': ['target_currency', '-fetched_at'],
            },
        ),
        migrations.AddIndex(
            model_name='exchangerate',
            index=models.Index(fields=['target_currency', '-fetched_at'], name='clients_exc_target__idx'),
        ),
    ]
