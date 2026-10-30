"""
🐛 [FIX]: باقة Premium Diagnostics (6,000 ج.م) اتقفلت في إعادة التسعير (0064) — بس
diagnostics_catalog.0002 بيعملها update_or_create(is_active=True) ومعتمد على
clients.0037 بس، فعلى أي قاعدة جديدة (سيرفر جديد / CI / e2e) ممكن يشتغل بعد 0064
ويرجّعها: تظهر باقة رابعة في صفحة الأسعار (و«6,000 د.إ» على الموقع الإماراتي لأن
سعر الدرهم صفر).

بنقفلها بس (مش بنمسحها) — الاشتراكات القديمة المربوطة بيها فاضلة زي ما هي.
"""
from django.db import migrations


def deactivate(apps, schema_editor):
    Plan = apps.get_model('clients', 'Plan')
    Plan.objects.filter(slug='premium_diagnostics', is_active=True).update(is_active=False)


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0085_platformevent_workspace_events'),
        # لازم بعد البذرة اللي بترجّعها
        ('diagnostics_catalog', '0002_seed_features_plan_costs'),
    ]

    operations = [
        migrations.RunPython(deactivate, migrations.RunPython.noop),
    ]
