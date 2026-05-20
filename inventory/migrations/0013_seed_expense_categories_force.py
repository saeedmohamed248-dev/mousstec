"""
Force-seed default expense categories for all schemas.
"""
from django.db import migrations


def seed_expense_categories(apps, schema_editor):
    ExpenseCategory = apps.get_model('inventory', 'ExpenseCategory')

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


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0010_financialtransaction_employee_seed_expense_categories'),
    ]

    operations = [
        migrations.RunPython(seed_expense_categories, migrations.RunPython.noop),
    ]
