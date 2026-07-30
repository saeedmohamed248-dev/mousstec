from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('inventory', '0027_alter_usermfa_id'),
    ]

    operations = [
        # 📍 Backlog #4 — Branch geofence pin (null = geofence off)
        migrations.AddField(
            model_name='branch',
            name='lat',
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True, verbose_name='Latitude الفرع'),
        ),
        migrations.AddField(
            model_name='branch',
            name='lng',
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True, verbose_name='Longitude الفرع'),
        ),
        migrations.AddField(
            model_name='branch',
            name='geofence_radius_m',
            field=models.PositiveIntegerField(default=200, verbose_name='نصف قطر نطاق الحضور (متر)'),
        ),
        # 💰 Backlog #5 — Commission settlement ledger
        migrations.CreateModel(
            name='CommissionPayout',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('amount', models.DecimalField(decimal_places=2, max_digits=10, verbose_name='المبلغ المصروف')),
                ('period_start', models.DateField(blank=True, null=True, verbose_name='بداية الفترة')),
                ('period_end', models.DateField(blank=True, null=True, verbose_name='نهاية الفترة')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('employee', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='commission_payouts',
                    to='inventory.employeeprofile',
                    verbose_name='الموظف',
                )),
                ('paid_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='commission_payouts_made',
                    to=settings.AUTH_USER_MODEL,
                    verbose_name='صُرفت بواسطة',
                )),
                ('transaction', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='commission_payouts',
                    to='inventory.financialtransaction',
                    verbose_name='الحركة المالية',
                )),
            ],
            options={
                'verbose_name': 'تسوية عمولة',
                'verbose_name_plural': 'دفتر تسويات العمولات',
                'ordering': ['-created_at'],
            },
        ),
    ]
