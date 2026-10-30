"""
🐛 [FIX]: باقة Premium Diagnostics (6,000 ج.م) اتقفلت في إعادة التسعير (0064) — بس
diagnostics_catalog.0002 بيعملها update_or_create(is_active=True) ومعتمد على
clients.0037 بس، فعلى أي قاعدة جديدة (سيرفر جديد / CI / e2e) ممكن يشتغل بعد 0064
ويرجّعها: تظهر باقة رابعة في صفحة الأسعار (و«6,000 د.إ» على الموقع الإماراتي لأن
سعر الدرهم صفر).

بنقفلها بس (مش بنمسحها) — الاشتراكات القديمة المربوطة بيها فاضلة زي ما هي.
"""
from django.db import migrations


def _on_public_schema(schema_editor):
    """🛡️ مهاجرة بيانات المنصة — تشتغل مرة على السكيمة العامة بس. migrate_schemas --tenant (وأي شركة
    بتتأسس من غير القالب) بيشغّل نفس المهاجرة جوه سكيمة كل شركة، والـ search_path بيوصل لجداول
    public: كانت هتتعاد مع كل شركة جديدة (تمسح أسعار/باقات المالك، أو تدّي المزايا المقفولة للكل)."""
    if schema_editor is None:   # نداء مباشر من اختبار
        return True
    from django_tenants.utils import get_public_schema_name
    return getattr(schema_editor.connection, 'schema_name', get_public_schema_name()) == get_public_schema_name()


def deactivate(apps, schema_editor):
    if not _on_public_schema(schema_editor):
        return
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
