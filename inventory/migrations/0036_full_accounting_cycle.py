import django.db.models.deletion
import django.utils.timezone
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """🏛️ Full accrual accounting cycle.

    Adds the document layer on top of the flat AccountingEntry ledger:
      • FiscalYear / AccountingPeriod — fiscal calendar with period locking.
      • JournalEntry — balanced journal document; AccountingEntry becomes its
        line via the new journal_entry FK.
      • TaxRate — VAT / tax rate → liability account mapping.
    """

    dependencies = [
        ('inventory', '0035_product_ai_background_studio'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='FiscalYear',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=20, unique=True, verbose_name='رمز السنة المالية')),
                ('name', models.CharField(max_length=100, verbose_name='اسم السنة المالية')),
                ('start_date', models.DateField(verbose_name='بداية السنة')),
                ('end_date', models.DateField(verbose_name='نهاية السنة')),
                ('is_closed', models.BooleanField(db_index=True, default=False, verbose_name='مُقفلة')),
                ('closed_at', models.DateTimeField(blank=True, null=True, verbose_name='تاريخ الإقفال')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('closed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'سنة مالية',
                'verbose_name_plural': 'السنوات المالية',
                'ordering': ['-start_date'],
            },
        ),
        migrations.CreateModel(
            name='AccountingPeriod',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, verbose_name='اسم الفترة')),
                ('start_date', models.DateField(db_index=True, verbose_name='بداية الفترة')),
                ('end_date', models.DateField(db_index=True, verbose_name='نهاية الفترة')),
                ('is_closed', models.BooleanField(db_index=True, default=False, verbose_name='مُقفلة')),
                ('closed_at', models.DateTimeField(blank=True, null=True, verbose_name='تاريخ الإقفال')),
                ('closed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('fiscal_year', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='periods', to='inventory.fiscalyear', verbose_name='السنة المالية')),
            ],
            options={
                'verbose_name': 'فترة محاسبية',
                'verbose_name_plural': 'الفترات المحاسبية',
                'ordering': ['start_date'],
                'unique_together': {('fiscal_year', 'start_date', 'end_date')},
            },
        ),
        migrations.CreateModel(
            name='JournalEntry',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('number', models.CharField(blank=True, db_index=True, max_length=32, verbose_name='رقم القيد')),
                ('date', models.DateField(db_index=True, default=django.utils.timezone.localdate, verbose_name='تاريخ القيد')),
                ('journal_type', models.CharField(choices=[('sales', 'يومية المبيعات'), ('purchase', 'يومية المشتريات'), ('cash_receipt', 'يومية المقبوضات'), ('cash_payment', 'يومية المدفوعات'), ('general', 'يومية عامة'), ('opening', 'قيد افتتاحي'), ('adjustment', 'قيد تسوية'), ('closing', 'قيد إقفال')], db_index=True, default='general', max_length=20, verbose_name='نوع اليومية')),
                ('reference', models.CharField(blank=True, db_index=True, max_length=100, verbose_name='المرجع')),
                ('description', models.CharField(max_length=255, verbose_name='البيان')),
                ('status', models.CharField(choices=[('draft', 'مسودة'), ('posted', 'مُرحَّل'), ('reversed', 'معكوس')], db_index=True, default='posted', max_length=10, verbose_name='الحالة')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('posted_at', models.DateTimeField(blank=True, null=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('financial_transaction', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='journal_entries', to='inventory.financialtransaction')),
                ('period', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='journal_entries', to='inventory.accountingperiod', verbose_name='الفترة المحاسبية')),
                ('purchase_invoice', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='journal_entries', to='inventory.purchaseinvoice')),
                ('reversal_of', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reversed_by', to='inventory.journalentry', verbose_name='عكس للقيد')),
                ('sale_invoice', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='journal_entries', to='inventory.saleinvoice')),
            ],
            options={
                'verbose_name': 'قيد يومية',
                'verbose_name_plural': 'قيود اليومية (Journal)',
                'ordering': ['-date', '-id'],
            },
        ),
        migrations.AddIndex(
            model_name='journalentry',
            index=models.Index(fields=['journal_type', '-date'], name='inv_je_type_date_idx'),
        ),
        migrations.AddIndex(
            model_name='journalentry',
            index=models.Index(fields=['status', '-date'], name='inv_je_status_date_idx'),
        ),
        migrations.CreateModel(
            name='TaxRate',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, verbose_name='اسم الضريبة')),
                ('rate', models.DecimalField(decimal_places=3, default=Decimal('0.000'), max_digits=6, verbose_name='النسبة %')),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('account', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='tax_rates', to='inventory.chartofaccount', verbose_name='حساب الضريبة المستحقة')),
            ],
            options={
                'verbose_name': 'معدل ضريبي',
                'verbose_name_plural': 'المعدلات الضريبية',
                'ordering': ['name'],
            },
        ),
        migrations.AddField(
            model_name='accountingentry',
            name='journal_entry',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='lines', to='inventory.journalentry', verbose_name='قيد اليومية'),
        ),
    ]
