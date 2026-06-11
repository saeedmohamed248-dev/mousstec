from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('inventory', '0025_seed_service_rules'),
    ]

    operations = [
        migrations.CreateModel(
            name='UserMFA',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('secret', models.CharField(max_length=64, verbose_name='TOTP Secret (base32)')),
                ('is_enabled', models.BooleanField(default=False, verbose_name='مفعّل؟')),
                ('backup_codes', models.JSONField(blank=True, default=list, verbose_name='أكواد الاسترجاع')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('enabled_at', models.DateTimeField(blank=True, null=True)),
                ('last_used_at', models.DateTimeField(blank=True, null=True)),
                ('user', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='mfa',
                    to=settings.AUTH_USER_MODEL,
                    verbose_name='المستخدم',
                )),
            ],
            options={
                'verbose_name': 'مصادقة ثنائية',
                'verbose_name_plural': 'المصادقة الثنائية',
            },
        ),
    ]
