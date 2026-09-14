from django.db import migrations

# كلمات دلالية للتعرّف على بنود المرتبات/الأجور/السلف (نفس منطق الـ view)
_SALARY_KEYWORDS = ('مرتب', 'رات', 'أجور', 'اجور', 'سلف', 'سلفة', 'معاش',
                    'salar', 'wage', 'payroll')


def tag_salary_categories(apps, schema_editor):
    """يوسم أي بند مصروف موجود اسمه يدل على مرتبات بمفتاح 'salaries' —
    عشان قائمة الموظفين تشتغل تلقائياً على البنود القديمة كمان."""
    ExpenseCategory = apps.get_model('inventory', 'ExpenseCategory')
    for cat in ExpenseCategory.objects.filter(system_key=''):
        low = (cat.name or '').strip().lower()
        if any(k in low for k in _SALARY_KEYWORDS):
            cat.system_key = 'salaries'
            cat.save(update_fields=['system_key'])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0046_branchaccess_price_view_and_roles'),
    ]

    operations = [
        migrations.RunPython(tag_salary_categories, noop_reverse),
    ]
