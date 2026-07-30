from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0073_rename_clients_bro_user_id_campaign_idx_clients_bro_user_id_52d88f_idx'),
    ]

    operations = [
        migrations.CreateModel(
            name='SavedCard',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('token', models.CharField(max_length=255, verbose_name='Paymob Card Token')),
                ('masked_pan', models.CharField(blank=True, max_length=32, verbose_name='آخر أرقام الكارت')),
                ('brand', models.CharField(blank=True, max_length=32, verbose_name='نوع الكارت')),
                ('is_default', models.BooleanField(default=True, verbose_name='الكارت الافتراضي')),
                ('auto_renew', models.BooleanField(default=False, verbose_name='تجديد تلقائي مفعّل')),
                ('is_active', models.BooleanField(default=True, verbose_name='نشط')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('last_used_at', models.DateTimeField(blank=True, null=True, verbose_name='آخر استخدام')),
                ('client', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='saved_cards',
                    to='clients.client',
                    verbose_name='الشركة',
                )),
            ],
            options={
                'verbose_name': 'كارت محفوظ',
                'verbose_name_plural': '💳 الكروت المحفوظة (تجديد تلقائي)',
            },
        ),
        migrations.AddConstraint(
            model_name='savedcard',
            constraint=models.UniqueConstraint(fields=('client', 'token'), name='savedcard_client_token_uniq'),
        ),
    ]
