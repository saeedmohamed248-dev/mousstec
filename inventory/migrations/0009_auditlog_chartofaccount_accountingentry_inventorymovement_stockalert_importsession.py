"""
Migration: Add Audit Trail, Accounting Ledger, Inventory Movements, Stock Alerts, and Safe Import models.
Also seeds default Chart of Accounts.
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import uuid
from decimal import Decimal


def seed_chart_of_accounts(apps, schema_editor):
    """بذر دليل الحسابات الافتراضي"""
    ChartOfAccount = apps.get_model('inventory', 'ChartOfAccount')

    # لا نبذر إذا كانت الحسابات موجودة مسبقاً
    if ChartOfAccount.objects.exists():
        return

    default_accounts = [
        # أصول
        ('1000', 'الأصول', 'asset', None),
        ('1001', 'الخزينة النقدية', 'asset', '1000'),
        ('1002', 'البنك', 'asset', '1000'),
        ('1003', 'المخزون', 'asset', '1000'),
        ('1004', 'ذمم مدينة (عملاء)', 'asset', '1000'),
        # خصوم
        ('2000', 'الخصوم', 'liability', None),
        ('2001', 'ذمم دائنة (موردين)', 'liability', '2000'),
        ('2002', 'ضريبة القيمة المضافة', 'liability', '2000'),
        # حقوق ملكية
        ('3000', 'حقوق الملكية', 'equity', None),
        ('3001', 'رأس المال', 'equity', '3000'),
        ('3002', 'أرباح محتجزة', 'equity', '3000'),
        # إيرادات
        ('4000', 'الإيرادات', 'revenue', None),
        ('4001', 'إيرادات المبيعات', 'revenue', '4000'),
        ('4002', 'إيرادات الخدمات والصيانة', 'revenue', '4000'),
        ('4099', 'إيرادات أخرى', 'revenue', '4000'),
        # مصروفات
        ('5000', 'المصروفات', 'expense', None),
        ('5001', 'تكلفة المشتريات', 'expense', '5000'),
        ('5002', 'رواتب وأجور', 'expense', '5000'),
        ('5003', 'إيجارات', 'expense', '5000'),
        ('5004', 'كهرباء ومياه', 'expense', '5000'),
        ('5005', 'صيانة وإصلاحات', 'expense', '5000'),
        ('5099', 'مصروفات عمومية', 'expense', '5000'),
    ]

    # إنشاء الحسابات بدون parent أولاً
    parent_map = {}
    for code, name, atype, parent_code in default_accounts:
        parent_map[code] = ChartOfAccount.objects.create(
            code=code, name=name, account_type=atype, is_active=True
        )

    # ربط الأبناء بالآباء
    for code, name, atype, parent_code in default_accounts:
        if parent_code and parent_code in parent_map:
            obj = parent_map[code]
            obj.parent = parent_map[parent_code]
            obj.save()


def reverse_seed(apps, schema_editor):
    """لا نحذف الحسابات عند التراجع — نتركها آمنة"""
    pass


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('inventory', '0008_alter_customer_options_remove_customer_company_name_and_more'),
    ]

    operations = [
        # AuditLog
        migrations.CreateModel(
            name='AuditLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('timestamp', models.DateTimeField(db_index=True, default=django.utils.timezone.now, verbose_name='التوقيت')),
                ('action', models.CharField(choices=[('create', 'إنشاء'), ('update', 'تعديل'), ('delete', 'حذف')], max_length=10, verbose_name='نوع العملية')),
                ('model_name', models.CharField(db_index=True, max_length=100, verbose_name='الجدول')),
                ('object_id', models.CharField(max_length=100, verbose_name='معرف السجل')),
                ('object_repr', models.CharField(blank=True, max_length=255, verbose_name='وصف السجل')),
                ('changes_json', models.JSONField(blank=True, default=dict, verbose_name='التغييرات (قبل/بعد)')),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True, verbose_name='عنوان IP')),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL, verbose_name='المستخدم')),
            ],
            options={
                'verbose_name': 'سجل مراجعة',
                'verbose_name_plural': 'سجل المراجعة والتدقيق (Audit Trail)',
                'ordering': ['-timestamp'],
            },
        ),
        migrations.AddIndex(
            model_name='auditlog',
            index=models.Index(fields=['model_name', 'object_id'], name='inventory_a_model_n_idx'),
        ),
        migrations.AddIndex(
            model_name='auditlog',
            index=models.Index(fields=['-timestamp'], name='inventory_a_timesta_idx'),
        ),

        # ChartOfAccount
        migrations.CreateModel(
            name='ChartOfAccount',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=20, unique=True, verbose_name='رقم الحساب')),
                ('name', models.CharField(max_length=200, verbose_name='اسم الحساب')),
                ('account_type', models.CharField(choices=[('asset', 'أصول (Assets)'), ('liability', 'خصوم (Liabilities)'), ('equity', 'حقوق ملكية (Equity)'), ('revenue', 'إيرادات (Revenue)'), ('expense', 'مصروفات (Expenses)')], max_length=20, verbose_name='نوع الحساب')),
                ('is_active', models.BooleanField(default=True, verbose_name='نشط')),
                ('description', models.TextField(blank=True, verbose_name='وصف')),
                ('parent', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='children', to='inventory.chartofaccount', verbose_name='الحساب الأب')),
            ],
            options={
                'verbose_name': 'حساب محاسبي',
                'verbose_name_plural': 'دليل الحسابات (Chart of Accounts)',
                'ordering': ['code'],
            },
        ),

        # AccountingEntry
        migrations.CreateModel(
            name='AccountingEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('entry_date', models.DateTimeField(db_index=True, default=django.utils.timezone.now, verbose_name='تاريخ القيد')),
                ('reference', models.CharField(db_index=True, max_length=100, verbose_name='المرجع')),
                ('description', models.CharField(max_length=255, verbose_name='البيان')),
                ('debit', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=15, verbose_name='مدين')),
                ('credit', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=15, verbose_name='دائن')),
                ('account', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='entries', to='inventory.chartofaccount', verbose_name='الحساب')),
                ('sale_invoice', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='accounting_entries', to='inventory.saleinvoice')),
                ('purchase_invoice', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='accounting_entries', to='inventory.purchaseinvoice')),
                ('financial_transaction', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='accounting_entries', to='inventory.financialtransaction')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'قيد محاسبي',
                'verbose_name_plural': 'القيود المحاسبية (Accounting Ledger)',
                'ordering': ['-entry_date'],
            },
        ),
        migrations.AddIndex(
            model_name='accountingentry',
            index=models.Index(fields=['reference'], name='inventory_ae_ref_idx'),
        ),
        migrations.AddIndex(
            model_name='accountingentry',
            index=models.Index(fields=['account', '-entry_date'], name='inventory_ae_acc_date_idx'),
        ),

        # InventoryMovement
        migrations.CreateModel(
            name='InventoryMovement',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('reason', models.CharField(choices=[('sale', 'بيع'), ('sale_return', 'مرتجع بيع'), ('purchase', 'شراء'), ('purchase_return', 'مرتجع شراء'), ('transfer_out', 'تحويل صادر'), ('transfer_in', 'تحويل وارد'), ('adjustment', 'تسوية جرد'), ('scrap', 'تقطيع / تالف'), ('manual', 'تعديل يدوي')], max_length=20, verbose_name='السبب')),
                ('quantity_change', models.IntegerField(verbose_name='التغيير في الكمية')),
                ('quantity_before', models.IntegerField(verbose_name='الكمية قبل')),
                ('quantity_after', models.IntegerField(verbose_name='الكمية بعد')),
                ('reference_type', models.CharField(blank=True, max_length=50, verbose_name='نوع المرجع')),
                ('reference_id', models.IntegerField(blank=True, null=True, verbose_name='رقم المرجع')),
                ('note', models.CharField(blank=True, max_length=255, verbose_name='ملاحظة')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='movements', to='inventory.product', verbose_name='المنتج')),
                ('branch', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='inventory.branch', verbose_name='الفرع')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'حركة مخزنية',
                'verbose_name_plural': 'سجل حركات المخزون',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='inventorymovement',
            index=models.Index(fields=['product', '-created_at'], name='inventory_im_prod_idx'),
        ),
        migrations.AddIndex(
            model_name='inventorymovement',
            index=models.Index(fields=['branch', '-created_at'], name='inventory_im_branch_idx'),
        ),

        # StockAlert
        migrations.CreateModel(
            name='StockAlert',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('alert_type', models.CharField(choices=[('low_stock', 'مخزون منخفض'), ('out_of_stock', 'نفاد تام')], max_length=20, verbose_name='نوع التنبيه')),
                ('current_quantity', models.IntegerField(verbose_name='الكمية الحالية')),
                ('min_stock_level', models.IntegerField(verbose_name='حد الأمان')),
                ('is_resolved', models.BooleanField(default=False, verbose_name='تم الحل')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='stock_alerts', to='inventory.product', verbose_name='المنتج')),
                ('branch', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='inventory.branch', verbose_name='الفرع')),
            ],
            options={
                'verbose_name': 'تنبيه مخزني',
                'verbose_name_plural': 'تنبيهات نقص المخزون',
                'ordering': ['-created_at'],
            },
        ),

        # ImportSession
        migrations.CreateModel(
            name='ImportSession',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('session_id', models.UUIDField(default=uuid.uuid4, unique=True)),
                ('entity_type', models.CharField(choices=[('customer', 'عملاء'), ('product', 'منتجات'), ('invoice', 'فواتير'), ('vendor', 'موردين')], max_length=20, verbose_name='نوع البيانات')),
                ('status', models.CharField(choices=[('pending', 'في الانتظار'), ('validating', 'جاري الفحص'), ('preview', 'جاهز للمراجعة'), ('importing', 'جاري الاستيراد'), ('completed', 'مكتمل'), ('failed', 'فشل'), ('rolled_back', 'تم التراجع')], default='pending', max_length=20, verbose_name='الحالة')),
                ('uploaded_file', models.FileField(upload_to='imports/', verbose_name='الملف')),
                ('original_filename', models.CharField(max_length=255, verbose_name='اسم الملف')),
                ('total_rows', models.IntegerField(default=0, verbose_name='إجمالي الصفوف')),
                ('valid_rows', models.IntegerField(default=0, verbose_name='صفوف صالحة')),
                ('error_rows', models.IntegerField(default=0, verbose_name='صفوف بها أخطاء')),
                ('conflict_rows', models.IntegerField(default=0, verbose_name='صفوف متعارضة')),
                ('validation_report', models.JSONField(blank=True, default=dict, verbose_name='تقرير الفحص')),
                ('conflict_report', models.JSONField(blank=True, default=dict, verbose_name='تقرير التعارضات')),
                ('imported_ids', models.JSONField(blank=True, default=list, verbose_name='السجلات المستوردة')),
                ('backup_snapshot', models.JSONField(blank=True, default=dict, verbose_name='نسخة احتياطية')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('created_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL, verbose_name='بواسطة')),
            ],
            options={
                'verbose_name': 'جلسة استيراد',
                'verbose_name_plural': 'جلسات الاستيراد الآمن',
                'ordering': ['-created_at'],
            },
        ),

        # Seed default Chart of Accounts
        migrations.RunPython(seed_chart_of_accounts, reverse_seed),
    ]
