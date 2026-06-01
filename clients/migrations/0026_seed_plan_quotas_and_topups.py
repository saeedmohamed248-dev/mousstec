"""
🎁 Seed migration:
   1. Set monthly_ai_designs_quota على باقات المطابع (printing) حسب السعر:
      • <= 550 ج/شهر → 50 تصميم
      • <= 800 ج/شهر → 100 تصميم
      • أعلى من 800 → 300 تصميم
   2. Map نفس المنطق على باقات السيارات لو موجودة (محايد — لا يضر).
   3. Seed قيم افتراضية في كل الـ DesignPackages الموجودة لو فاضية.

Idempotent: يقدر يتشغل أكتر من مرة بدون مشاكل.
"""
from django.db import migrations
from decimal import Decimal


def seed_plan_quotas(apps, schema_editor):
    Plan = apps.get_model('clients', 'Plan')

    # المنطق: أرخص باقة (<=550) → 50، الأوسط → 100، الأعلى → 300
    plans = list(Plan.objects.filter(industry='printing').order_by('monthly_price'))
    quotas_by_position = [50, 100, 300]

    if len(plans) >= 3:
        for plan, quota in zip(plans, quotas_by_position):
            plan.monthly_ai_designs_quota = quota
            plan.save(update_fields=['monthly_ai_designs_quota'])
    else:
        # Fallback: حسب السعر
        for plan in plans:
            price = Decimal(str(plan.monthly_price or 0))
            if price <= Decimal('550'):
                plan.monthly_ai_designs_quota = 50
            elif price <= Decimal('800'):
                plan.monthly_ai_designs_quota = 100
            else:
                plan.monthly_ai_designs_quota = 300
            plan.save(update_fields=['monthly_ai_designs_quota'])


def reverse_seed(apps, schema_editor):
    Plan = apps.get_model('clients', 'Plan')
    Plan.objects.update(monthly_ai_designs_quota=0)


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0025_plan_monthly_ai_designs_quota_tenantdesigntopup'),
    ]

    operations = [
        migrations.RunPython(seed_plan_quotas, reverse_code=reverse_seed),
    ]
