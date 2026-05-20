"""
Migration: Add employee field to FinancialTransaction + Seed default expense categories.
"""
from django.db import migrations, models
import django.db.models.deletion


def seed_expense_categories(apps, schema_editor):
    """بذر بنود المصروفات الافتراضية"""
    ExpenseCategory = apps.get_model('inventory', 'ExpenseCategory')

    if ExpenseCategory.objects.exists():
        return

    default_categories = [
        'رواتب وأجور',
        'عمولات موظفين',
        'سلف موظفين',
        'إيجار المحل / الورشة',
        'كهرباء ومياه',
        'إنترنت واتصالات',
        'صيانة معدات وأجهزة',
        'أدوات ومستلزمات ورشة',
        'وقود ومحروقات',
        'نقل وشحن',
        'ضرائب ورسوم حكومية',
        'تأمينات اجتماعية',
        'دعاية وتسويق',
        'ضيافة ونثريات',
        'مصروفات قانونية ومحاسبية',
        'اشتراكات برمجيات',
        'مصروفات متنوعة',
    ]

    for name in default_categories:
        ExpenseCategory.objects.get_or_create(name=name)


def reverse_seed(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0009_auditlog_chartofaccount_accountingentry_inventorymovement_stockalert_importsession'),
    ]

    operations = [
        # Add employee field to FinancialTransaction
        migrations.AddField(
            model_name='financialtransaction',
            name='employee',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='financial_transactions',
                to='inventory.employeeprofile',
                verbose_name='الموظف (للرواتب/السلف)',
            ),
        ),
        # Seed default expense categories
        migrations.RunPython(seed_expense_categories, reverse_seed),
    ]
