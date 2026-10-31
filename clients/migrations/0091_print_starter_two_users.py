"""
👥 Starter المطابع = مستخدمين بدل واحد

باقة Starter (875) كانت مستخدم واحد بس، وهي أغلى من Silver بتاعة السيارات (550) اللي فيها
مستخدمين — صاحب المطبعة مايقدرش يضيف مصمم أو كاشير من غير إضافة مدفوعة. المستخدم التاني بقى
جوه الباقة بنفس السعر، والمشتركين الحاليين بياخدوه فوراً.

🐛 [FIX]: كارت الباقات في صفحة الأسعار كان بيكتب «50 تصميم AI/شهر» مرتين في المطابع — مرة من
حصة الباقة (monthly_ai_designs_quota) ومرة من سطر مكتوب في المميزات. شلنا السطر المكرر بس.
"""
from django.db import migrations


def _on_public_schema(schema_editor):
    """🛡️ مهاجرة بيانات المنصة — تشتغل مرة على السكيمة العامة بس (نفس حارس 0089)."""
    if schema_editor is None:   # نداء مباشر من اختبار
        return True
    from django_tenants.utils import get_public_schema_name
    return getattr(schema_editor.connection, 'schema_name', get_public_schema_name()) == get_public_schema_name()


def forward(apps, schema_editor):
    if not _on_public_schema(schema_editor):
        return
    Plan = apps.get_model('clients', 'Plan')
    Client = apps.get_model('clients', 'Client')
    TenantSubscription = apps.get_model('clients', 'TenantSubscription')

    for plan in Plan.objects.filter(industry='printing'):
        old_features, old_users = list(plan.features or []), plan.max_users
        features = old_features
        if plan.monthly_ai_designs_quota:
            features = [f for f in features if f != f"{plan.monthly_ai_designs_quota} تصميم AI/شهر"]
        if plan.slug == 'print-starter':
            features = ['فرع واحد · مستخدمين' if f == 'فرع واحد · مستخدم واحد' else f for f in features]
            plan.max_users = max(plan.max_users, 2)
        if features != old_features or plan.max_users != old_users:
            plan.features = features
            plan.save(update_fields=['features', 'max_users'])

    starter = Plan.objects.filter(slug='print-starter').first()
    if starter is None:
        return
    tenant_ids = TenantSubscription.objects.filter(plan=starter).values_list('tenant_id', flat=True)
    Client.objects.filter(pk__in=list(tenant_ids), max_users__lt=2).update(max_users=2)


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0090_platform_user_mfa'),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
