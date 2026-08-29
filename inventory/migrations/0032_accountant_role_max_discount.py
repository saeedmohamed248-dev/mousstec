from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0031_saleinvoice_sales_channel'),
    ]

    operations = [
        migrations.AlterField(
            model_name='employeeprofile',
            name='role',
            field=models.CharField(
                choices=[
                    ('admin', 'مدير عام (أدمن)'), ('manager', 'مدير فرع'),
                    ('sales', 'مبيعات (Sales)'), ('accountant', 'محاسب (Accountant)'),
                    ('engineer', 'مهندس تشخيص (Engineer)'), ('tech', 'فني / ميكانيكي (Technician)'),
                    ('cashier', 'كاشير / استقبال (Cashier)'), ('stock', 'أمين مخزن'),
                    ('hr', 'موارد بشرية (HR)'),
                ],
                default='cashier', max_length=20, verbose_name='الدور الوظيفي'),
        ),
        migrations.AddField(
            model_name='employeeprofile',
            name='max_discount_pct',
            field=models.DecimalField(
                decimal_places=2, default=0.00, max_digits=5,
                help_text='أقصى خصم يقدر البائع يعمله على الفاتورة. 0 = ممنوع الخصم. المدير/الأدمن بلا حد.',
                verbose_name='أقصى نسبة خصم مسموحة %'),
        ),
    ]
