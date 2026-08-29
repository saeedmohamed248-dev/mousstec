from decimal import Decimal

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0029_recursive_disassembly'),
    ]

    operations = [
        migrations.CreateModel(
            name='DisassemblyTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(help_text='مثال: تفكيك محرك N20 القياسي', max_length=200, unique=True, verbose_name='اسم القالب')),
                ('engine_code', models.CharField(blank=True, db_index=True, help_text='اختياري — لتصفية القوالب حسب المحرك (N20, B48...).', max_length=100, verbose_name='كود المحرك / الموديل')),
                ('default_scrap_revenue', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14, validators=[django.core.validators.MinValueValidator(Decimal('0.00'))], verbose_name='إيراد الخردة الافتراضي')),
                ('is_active', models.BooleanField(default=True, verbose_name='نشط')),
                ('notes', models.TextField(blank=True, verbose_name='ملاحظات')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'قالب فك',
                'verbose_name_plural': '📋 قوالب الفك (Reverse BOM)',
                'ordering': ('name',),
            },
        ),
        migrations.CreateModel(
            name='TemplateItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('part_name', models.CharField(max_length=200, verbose_name='اسم القطعة')),
                ('default_estimated_sales_price', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14, validators=[django.core.validators.MinValueValidator(Decimal('0.00'))], verbose_name='سعر البيع التقديري الافتراضي')),
                ('weight_percentage', models.DecimalField(decimal_places=3, default=Decimal('0.00'), help_text='بديل للسعر — لو > 0 تُستخدم لتقدير السعر من تكلفة الأب.', max_digits=6, validators=[django.core.validators.MinValueValidator(Decimal('0.00'))], verbose_name='نسبة الوزن %')),
                ('sku_prefix', models.CharField(blank=True, help_text='اختياري — يُدمج مع مرجع الأب لتوليد SKU فريد للابن.', max_length=60, verbose_name='بادئة SKU للأبناء')),
                ('sort_order', models.PositiveIntegerField(default=0, verbose_name='الترتيب')),
                ('product', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='template_items', to='inventory.product', verbose_name='قطعة الكتالوج')),
                ('template', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='inventory.disassemblytemplate', verbose_name='القالب')),
            ],
            options={
                'verbose_name': 'بند قالب',
                'verbose_name_plural': 'بنود القوالب',
                'ordering': ('sort_order', 'id'),
            },
        ),
    ]
