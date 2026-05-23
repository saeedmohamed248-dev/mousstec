"""
Initial migration for the Printing & Design module.
"""
import django.db.models.deletion
import django.utils.timezone
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # =====================================================================
        # 🏢 PrintBranch
        # =====================================================================
        migrations.CreateModel(
            name='PrintBranch',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, verbose_name='اسم الفرع')),
                ('address', models.TextField(blank=True, verbose_name='العنوان')),
                ('phone', models.CharField(blank=True, max_length=20, verbose_name='الهاتف')),
                ('is_active', models.BooleanField(default=True, verbose_name='نشط')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'verbose_name': 'فرع',
                'verbose_name_plural': 'الفروع',
                'ordering': ['name'],
            },
        ),
        # =====================================================================
        # 👤 PrintCustomer
        # =====================================================================
        migrations.CreateModel(
            name='PrintCustomer',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=150, verbose_name='اسم العميل')),
                ('phone', models.CharField(blank=True, max_length=20, verbose_name='الهاتف')),
                ('whatsapp', models.CharField(blank=True, max_length=20, verbose_name='واتساب')),
                ('email', models.EmailField(blank=True, max_length=254, verbose_name='البريد الإلكتروني')),
                ('company', models.CharField(blank=True, max_length=150, verbose_name='اسم الشركة')),
                ('notes', models.TextField(blank=True, verbose_name='ملاحظات')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'verbose_name': 'عميل',
                'verbose_name_plural': 'العملاء',
                'ordering': ['-created_at'],
            },
        ),
        # =====================================================================
        # 🖨️ MachineProfile
        # =====================================================================
        migrations.CreateModel(
            name='MachineProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=150, verbose_name='اسم الماكينة')),
                ('machine_type', models.CharField(choices=[('digital', 'طابعة رقمية (Digital)'), ('offset', 'طابعة أوفست (Offset)'), ('large_format', 'طابعة لارج فورمات (Wide/Large Format)'), ('dtf', 'طابعة DTF'), ('uv', 'طابعة UV'), ('sublimation', 'طباعة حرارية (Sublimation)'), ('cutter', 'ماكينة قص (Cutting Plotter)'), ('laminator', 'ماكينة تغليف (Laminator)'), ('other', 'أخرى')], default='digital', max_length=20, verbose_name='نوع الماكينة')),
                ('brand', models.CharField(blank=True, max_length=100, verbose_name='الماركة / الشركة المصنعة')),
                ('model_number', models.CharField(blank=True, max_length=100, verbose_name='رقم الموديل')),
                ('is_active', models.BooleanField(default=True, verbose_name='تعمل حالياً')),
                ('power_consumption_kwh', models.DecimalField(decimal_places=2, default=0, max_digits=8, verbose_name='استهلاك الكهرباء (kWh/ساعة)')),
                ('electricity_rate_per_kwh', models.DecimalField(decimal_places=4, default=Decimal('2.50'), max_digits=8, verbose_name='سعر الكيلو وات (ج.م)')),
                ('hourly_labor_cost', models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name='تكلفة العامل/الساعة (ج.م)')),
                ('ink_cyan_cost_per_ml', models.DecimalField(decimal_places=4, default=0, max_digits=8, verbose_name='تكلفة حبر Cyan (ج.م/مل)')),
                ('ink_magenta_cost_per_ml', models.DecimalField(decimal_places=4, default=0, max_digits=8, verbose_name='تكلفة حبر Magenta (ج.م/مل)')),
                ('ink_yellow_cost_per_ml', models.DecimalField(decimal_places=4, default=0, max_digits=8, verbose_name='تكلفة حبر Yellow (ج.م/مل)')),
                ('ink_black_cost_per_ml', models.DecimalField(decimal_places=4, default=0, max_digits=8, verbose_name='تكلفة حبر Black (ج.م/مل)')),
                ('total_print_hours', models.DecimalField(decimal_places=2, default=0, max_digits=12, verbose_name='إجمالي ساعات التشغيل')),
                ('maintenance_due_date', models.DateField(blank=True, null=True, verbose_name='موعد الصيانة القادمة')),
                ('notes', models.TextField(blank=True, verbose_name='ملاحظات')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('branch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='printing.printbranch', verbose_name='الفرع')),
            ],
            options={
                'verbose_name': 'ماكينة طباعة',
                'verbose_name_plural': 'ماكينات الطباعة',
                'ordering': ['name'],
            },
        ),
        # =====================================================================
        # 🎨 Designer
        # =====================================================================
        migrations.CreateModel(
            name='Designer',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('specialization', models.CharField(blank=True, max_length=100, verbose_name='التخصص')),
                ('hourly_rate', models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name='أجر الساعة (ج.م)')),
                ('is_active', models.BooleanField(default=True, verbose_name='نشط')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='designer_profile', to=settings.AUTH_USER_MODEL, verbose_name='المستخدم')),
                ('branch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='printing.printbranch', verbose_name='الفرع')),
            ],
            options={
                'verbose_name': 'مصمم',
                'verbose_name_plural': 'المصممين',
            },
        ),
        # =====================================================================
        # 📝 DesignerWorkLog
        # =====================================================================
        migrations.CreateModel(
            name='DesignerWorkLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField(default=django.utils.timezone.now, verbose_name='التاريخ')),
                ('title', models.CharField(max_length=200, verbose_name='عنوان العمل')),
                ('description', models.TextField(blank=True, verbose_name='تفاصيل')),
                ('execution_type', models.CharField(choices=[('manual', '⌨️ يدوي بالكامل'), ('ai_generated', '🤖 مُنشأ بالذكاء الاصطناعي'), ('ai_assisted', '🧠 مساعد بالذكاء الاصطناعي (AI + تعديل يدوي)')], default='manual', max_length=15, verbose_name='نوع التنفيذ')),
                ('duration_hours', models.DecimalField(decimal_places=2, default=0, max_digits=5, verbose_name='مدة العمل (ساعات)')),
                ('client_rating', models.PositiveSmallIntegerField(blank=True, null=True, verbose_name='تقييم العميل (1-5)')),
                ('client_feedback', models.TextField(blank=True, verbose_name='ملاحظات العميل')),
                ('preview_image', models.ImageField(blank=True, upload_to='designer_works/%Y/%m/', verbose_name='صورة العمل')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('designer', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='work_logs', to='printing.designer', verbose_name='المصمم')),
                ('customer', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='printing.printcustomer', verbose_name='العميل')),
            ],
            options={
                'verbose_name': 'سجل عمل مصمم',
                'verbose_name_plural': 'سجلات أعمال المصممين',
                'ordering': ['-date', '-created_at'],
            },
        ),
        # =====================================================================
        # 📋 PrintOrder
        # =====================================================================
        migrations.CreateModel(
            name='PrintOrder',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('order_number', models.CharField(max_length=30, unique=True, verbose_name='رقم الطلب')),
                ('status', models.CharField(choices=[('draft', 'مسودة'), ('confirmed', 'مؤكد'), ('in_progress', 'قيد التنفيذ'), ('ready', 'جاهز للتسليم'), ('delivered', 'تم التسليم'), ('cancelled', 'ملغي')], default='draft', max_length=15, verbose_name='الحالة')),
                ('date_created', models.DateTimeField(auto_now_add=True, verbose_name='تاريخ الإنشاء')),
                ('date_due', models.DateTimeField(blank=True, null=True, verbose_name='موعد التسليم')),
                ('date_delivered', models.DateTimeField(blank=True, null=True, verbose_name='تاريخ التسليم الفعلي')),
                ('total_amount', models.DecimalField(decimal_places=2, default=0, max_digits=12, verbose_name='الإجمالي')),
                ('discount', models.DecimalField(decimal_places=2, default=0, max_digits=12, verbose_name='الخصم')),
                ('paid_amount', models.DecimalField(decimal_places=2, default=0, max_digits=12, verbose_name='المدفوع')),
                ('notes', models.TextField(blank=True, verbose_name='ملاحظات')),
                ('customer', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='printing.printcustomer', verbose_name='العميل')),
                ('branch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='printing.printbranch', verbose_name='الفرع')),
            ],
            options={
                'verbose_name': 'طلب طباعة',
                'verbose_name_plural': 'طلبات الطباعة',
                'ordering': ['-date_created'],
            },
        ),
        # =====================================================================
        # 🖨️ PrintJob
        # =====================================================================
        migrations.CreateModel(
            name='PrintJob',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('description', models.CharField(max_length=300, verbose_name='وصف المهمة')),
                ('paper_size', models.CharField(choices=[('a0', 'A0'), ('a1', 'A1'), ('a2', 'A2'), ('a3', 'A3'), ('a4', 'A4'), ('a5', 'A5'), ('b1', 'B1'), ('b2', 'B2'), ('roll_60', 'رول 60 سم'), ('roll_90', 'رول 90 سم'), ('roll_120', 'رول 120 سم'), ('roll_150', 'رول 150 سم'), ('custom', 'مقاس مخصص')], default='a4', max_length=10, verbose_name='مقاس الورق')),
                ('custom_width_cm', models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True, verbose_name='العرض (سم)')),
                ('custom_height_cm', models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True, verbose_name='الارتفاع (سم)')),
                ('quantity', models.PositiveIntegerField(default=1, verbose_name='الكمية')),
                ('copies', models.PositiveIntegerField(default=1, verbose_name='عدد النسخ')),
                ('unit_price', models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name='سعر الوحدة')),
                ('total_price', models.DecimalField(decimal_places=2, default=0, max_digits=12, verbose_name='الإجمالي')),
                ('actual_time_hours', models.DecimalField(decimal_places=2, default=0, max_digits=6, verbose_name='وقت التنفيذ الفعلي (ساعات)')),
                ('ink_cyan_ml', models.DecimalField(decimal_places=2, default=0, max_digits=8, verbose_name='حبر Cyan (مل)')),
                ('ink_magenta_ml', models.DecimalField(decimal_places=2, default=0, max_digits=8, verbose_name='حبر Magenta (مل)')),
                ('ink_yellow_ml', models.DecimalField(decimal_places=2, default=0, max_digits=8, verbose_name='حبر Yellow (مل)')),
                ('ink_black_ml', models.DecimalField(decimal_places=2, default=0, max_digits=8, verbose_name='حبر Black (مل)')),
                ('design_file', models.FileField(blank=True, upload_to='print_jobs/%Y/%m/', verbose_name='ملف التصميم')),
                ('notes', models.TextField(blank=True, verbose_name='ملاحظات')),
                ('is_complete', models.BooleanField(default=False, verbose_name='مكتملة')),
                ('completed_at', models.DateTimeField(blank=True, null=True, verbose_name='تاريخ الإكمال')),
                ('order', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='jobs', to='printing.printorder', verbose_name='الطلب')),
                ('machine', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='printing.machineprofile', verbose_name='الماكينة')),
                ('designer', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='printing.designer', verbose_name='المصمم')),
            ],
            options={
                'verbose_name': 'مهمة طباعة',
                'verbose_name_plural': 'مهام الطباعة',
            },
        ),
        # =====================================================================
        # 📦 PrintMaterial
        # =====================================================================
        migrations.CreateModel(
            name='PrintMaterial',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200, verbose_name='اسم الخامة')),
                ('category', models.CharField(choices=[('paper', 'ورق'), ('ink', 'حبر'), ('vinyl', 'فينيل'), ('banner', 'بنر / فليكس'), ('lamination', 'تغليف / لامينيشن'), ('packaging', 'تغليف وتعبئة'), ('other', 'خامات أخرى')], default='paper', max_length=15, verbose_name='التصنيف')),
                ('sku', models.CharField(blank=True, max_length=50, verbose_name='كود الخامة')),
                ('unit', models.CharField(default='قطعة', max_length=30, verbose_name='وحدة القياس')),
                ('quantity', models.DecimalField(decimal_places=2, default=0, max_digits=12, verbose_name='الكمية الحالية')),
                ('min_stock', models.DecimalField(decimal_places=2, default=5, max_digits=12, verbose_name='الحد الأدنى')),
                ('cost_per_unit', models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name='تكلفة الوحدة (ج.م)')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('branch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='printing.printbranch', verbose_name='الفرع')),
            ],
            options={
                'verbose_name': 'خامة طباعة',
                'verbose_name_plural': 'خامات الطباعة',
                'ordering': ['category', 'name'],
            },
        ),
        # =====================================================================
        # 💰 PrintTreasury
        # =====================================================================
        migrations.CreateModel(
            name='PrintTreasury',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, verbose_name='اسم الخزينة')),
                ('balance', models.DecimalField(decimal_places=2, default=0, max_digits=15, verbose_name='الرصيد')),
                ('is_active', models.BooleanField(default=True, verbose_name='نشطة')),
                ('branch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='printing.printbranch', verbose_name='الفرع')),
            ],
            options={
                'verbose_name': 'خزينة',
                'verbose_name_plural': 'الخزائن',
            },
        ),
        # =====================================================================
        # 💳 PrintTransaction
        # =====================================================================
        migrations.CreateModel(
            name='PrintTransaction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('transaction_type', models.CharField(choices=[('in', 'إيداع / إيراد'), ('out', 'سحب / مصروف')], max_length=3, verbose_name='النوع')),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12, verbose_name='المبلغ')),
                ('description', models.CharField(blank=True, max_length=300, verbose_name='الوصف')),
                ('date', models.DateTimeField(default=django.utils.timezone.now, verbose_name='التاريخ')),
                ('treasury', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='printing.printtreasury', verbose_name='الخزينة')),
                ('order', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='printing.printorder', verbose_name='الطلب المرتبط')),
                ('created_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL, verbose_name='بواسطة')),
            ],
            options={
                'verbose_name': 'حركة مالية',
                'verbose_name_plural': 'الحركات المالية',
                'ordering': ['-date'],
            },
        ),
    ]
