from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0028_branch_geofence_commission_payout'),
    ]

    operations = [
        migrations.CreateModel(
            name='ETAInvoiceSubmission',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(
                    choices=[('pending', 'في الانتظار'),
                             ('submitted', 'تم الرفع — بانتظار الاعتماد'),
                             ('valid', 'معتمدة'),
                             ('invalid', 'مرفوضة من المنظومة'),
                             ('error', 'خطأ')],
                    db_index=True, default='pending', max_length=12, verbose_name='الحالة')),
                ('eta_uuid', models.CharField(blank=True, db_index=True, max_length=64, verbose_name='ETA UUID')),
                ('eta_long_id', models.CharField(blank=True, max_length=100, verbose_name='ETA Long ID')),
                ('submission_uuid', models.CharField(blank=True, max_length=64, verbose_name='Submission ID')),
                ('response_json', models.JSONField(blank=True, default=dict, verbose_name='رد المنظومة')),
                ('error_json', models.JSONField(blank=True, default=dict, verbose_name='تفاصيل الخطأ')),
                ('attempts', models.PositiveIntegerField(default=0, verbose_name='عدد المحاولات')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('submitted_at', models.DateTimeField(blank=True, null=True, verbose_name='وقت الرفع')),
                ('sale_invoice', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='eta_submission',
                    to='inventory.saleinvoice',
                    verbose_name='الفاتورة',
                )),
            ],
            options={
                'verbose_name': 'رفع فاتورة إلكترونية (ETA)',
                'verbose_name_plural': '🇪🇬 الفواتير الإلكترونية (ETA)',
                'ordering': ['-created_at'],
            },
        ),
    ]
