from decimal import Decimal

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('inventory', '0028_part_return_guard'),
    ]

    operations = [
        migrations.CreateModel(
            name='InventoryItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sku', models.CharField(max_length=120, unique=True, verbose_name='SKU / رقم الشاسيه (VIN)')),
                ('name', models.CharField(max_length=200, verbose_name='اسم العنصر')),
                ('cost', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14, validators=[django.core.validators.MinValueValidator(Decimal('0.00'))], verbose_name='التكلفة')),
                ('estimated_sales_price', models.DecimalField(decimal_places=2, default=Decimal('0.00'), help_text='يُستخدم كوزن في توزيع تكلفة الأب على الأبناء.', max_digits=14, validators=[django.core.validators.MinValueValidator(Decimal('0.00'))], verbose_name='سعر البيع التقديري')),
                ('status', models.CharField(choices=[('in_stock', 'في المخزن'), ('disassembled', 'تم تفكيكه'), ('sold', 'تم بيعه')], db_index=True, default='in_stock', max_length=20, verbose_name='الحالة')),
                ('notes', models.TextField(blank=True, verbose_name='ملاحظات')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('branch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='disassembly_items', to='inventory.branch', verbose_name='الفرع')),
                ('product', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='disassembly_items', to='inventory.product', verbose_name='قطعة الكتالوج المرتبطة')),
            ],
            options={
                'verbose_name': 'عنصر مخزون (قابل للفك)',
                'verbose_name_plural': '🔩 عناصر الفك التدريجي',
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='DisassemblyEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateTimeField(default=django.utils.timezone.now, verbose_name='تاريخ الفك')),
                ('total_scrap_revenue', models.DecimalField(decimal_places=2, default=Decimal('0.00'), help_text='إيراد بيع البقايا/الحديد الخردة — يُخصم من تكلفة الأب قبل التوزيع.', max_digits=14, validators=[django.core.validators.MinValueValidator(Decimal('0.00'))], verbose_name='إجمالي إيراد الخردة')),
                ('is_executed', models.BooleanField(default=False, verbose_name='تم التنفيذ؟')),
                ('executed_at', models.DateTimeField(blank=True, null=True, verbose_name='وقت التنفيذ')),
                ('parent_cost_snapshot', models.DecimalField(blank=True, decimal_places=2, editable=False, max_digits=14, null=True, verbose_name='تكلفة الأب وقت التنفيذ')),
                ('adjusted_parent_cost', models.DecimalField(blank=True, decimal_places=2, editable=False, max_digits=14, null=True, verbose_name='تكلفة الأب المعدّلة (بعد الخردة)')),
                ('notes', models.TextField(blank=True, verbose_name='ملاحظات')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL, verbose_name='نفّذها')),
                ('parent_item', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='disassembly_events', to='inventory.inventoryitem', verbose_name='العنصر الأب')),
            ],
            options={
                'verbose_name': 'حدث فك',
                'verbose_name_plural': '🔧 أحداث الفك التدريجي',
                'ordering': ('-date',),
            },
        ),
        migrations.CreateModel(
            name='DisassemblyResult',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('estimated_sales_price', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14, validators=[django.core.validators.MinValueValidator(Decimal('0.00'))], verbose_name='سعر البيع التقديري')),
                ('allocated_cost', models.DecimalField(decimal_places=2, default=Decimal('0.00'), editable=False, max_digits=14, verbose_name='التكلفة المخصّصة')),
                ('child_item', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='produced_as', to='inventory.inventoryitem', verbose_name='العنصر الابن')),
                ('event', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='results', to='inventory.disassemblyevent', verbose_name='حدث الفك')),
            ],
            options={
                'verbose_name': 'ناتج فك',
                'verbose_name_plural': 'نواتج الفك',
            },
        ),
        migrations.AddConstraint(
            model_name='disassemblyresult',
            constraint=models.UniqueConstraint(fields=('event', 'child_item'), name='uniq_event_child'),
        ),
    ]
