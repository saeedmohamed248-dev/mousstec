"""Backfill ExpenseCategory.system_key for known semantic categories.

Maps the Arabic / English display names of existing categories to their
machine-stable `system_key` so the Quick Expense UI can detect 'salaries'
and surface the employee dropdown automatically.
"""
from django.db import migrations

SALARY_NAMES = {
    'رواتب وأجور', 'رواتب', 'مرتبات', 'salaries', 'salary', 'wages',
    'salaries & wages', 'salaries and wages',
}
RENT_NAMES = {'إيجار', 'ايجار', 'إيجارات', 'rent'}
UTILITY_NAMES = {'مرافق', 'كهرباء', 'مياه', 'utilities', 'electricity', 'water'}


def _set_key(ExpenseCategory, names, key):
    qs = ExpenseCategory.objects.filter(system_key='')
    for cat in qs:
        if cat.name and cat.name.strip().lower() in {n.lower() for n in names}:
            cat.system_key = key
            cat.save(update_fields=['system_key'])


def backfill(apps, schema_editor):
    ExpenseCategory = apps.get_model('inventory', 'ExpenseCategory')
    _set_key(ExpenseCategory, SALARY_NAMES,  'salaries')
    _set_key(ExpenseCategory, RENT_NAMES,    'rent')
    _set_key(ExpenseCategory, UTILITY_NAMES, 'utilities')

    # If no salaries row exists at all, create one — the Quick Expense UI
    # depends on it being available the first time payroll is recorded.
    if not ExpenseCategory.objects.filter(system_key='salaries').exists():
        ExpenseCategory.objects.get_or_create(
            name='رواتب وأجور',
            defaults={'system_key': 'salaries'},
        )


def unbackfill(apps, schema_editor):
    ExpenseCategory = apps.get_model('inventory', 'ExpenseCategory')
    ExpenseCategory.objects.filter(
        system_key__in=['salaries', 'rent', 'utilities']
    ).update(system_key='')


class Migration(migrations.Migration):
    dependencies = [
        ('inventory', '0019_dms_tier1_pillars'),
    ]
    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
