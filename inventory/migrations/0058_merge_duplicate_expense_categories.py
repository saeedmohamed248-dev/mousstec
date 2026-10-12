"""🧹 دمج بنود المصروفات المكررة.

مهاجرتين قديمتين (0010 و0013) زرعوا قائمتين متشابهتين، فكل شركة جديدة كان
عندها «إيجار المحل / الورشة» و«إيجار المحل»، و«أدوات ومستلزمات ورشة»
و«أدوات ومستلزمات». بننقل كل اللي متسجّل على الاسم الطويل للاسم القصير
(نفس أسماء القائمة الافتراضية في لوحة الإدارة) ونشيل المكرر.

آمن: لو الاسم القصير مش موجود، بنغيّر اسم الطويل بس (من غير ما حاجة تتنقل).
"""
from django.db import migrations

PAIRS = (
    ('إيجار المحل / الورشة', 'إيجار المحل'),
    ('أدوات ومستلزمات ورشة', 'أدوات ومستلزمات'),
)


def merge(apps, schema_editor):
    ExpenseCategory = apps.get_model('inventory', 'ExpenseCategory')
    related = [rel for rel in ExpenseCategory._meta.related_objects
               if rel.one_to_many or rel.one_to_one]
    for long_name, short_name in PAIRS:
        dup = ExpenseCategory.objects.filter(name=long_name).first()
        if dup is None:
            continue
        keep = ExpenseCategory.objects.filter(name=short_name).first()
        if keep is None:
            dup.name = short_name
            dup.save(update_fields=['name'])
            continue
        for rel in related:
            rel.related_model._base_manager.filter(**{rel.field.name: dup}).update(**{rel.field.name: keep})
        if dup.system_key and not keep.system_key:
            keep.system_key = dup.system_key
            keep.save(update_fields=['system_key'])
        dup.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0057_grouped_purchase_shipment'),
    ]

    operations = [
        migrations.RunPython(merge, migrations.RunPython.noop),
    ]
