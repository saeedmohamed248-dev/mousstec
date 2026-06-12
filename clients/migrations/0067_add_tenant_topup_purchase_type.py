from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0066_add_diag_topup_purchase_type'),
    ]

    operations = [
        migrations.AlterField(
            model_name='manualpaymentreceipt',
            name='purchase_type',
            field=models.CharField(
                choices=[
                    ('subscription', 'اشتراك SaaS'),
                    ('parts', 'قطع غيار'),
                    ('design', 'باقة تصاميم'),
                    ('diagnostics', 'ترقية تشخيص'),
                    ('addon', 'إضافة (موظف/فرع/خزينة)'),
                    ('diag_topup', 'شحن تشخيص (30 استخدام)'),
                    ('tenant_topup', 'شحن تصاميم للشركة'),
                ],
                db_index=True,
                max_length=20,
            ),
        ),
    ]
