"""
👥 Silver = مستخدمين بدل واحد

باقة Silver كانت مستخدم واحد بس — يعني صاحب المحل نفسه، ومايقدرش يضيف كاشير من غير إضافة
مدفوعة. المنافسين (دفترة / أودو) أغلى بكتير وفيهم أكتر من مستخدم، فالمستخدم التاني بقى جوه
الباقة بنفس السعر (550). المشتركين الحاليين على Silver بياخدوه فوراً.
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


def forward(apps, schema_editor):
    if not _on_public_schema(schema_editor):
        return
    Plan = apps.get_model('clients', 'Plan')
    Client = apps.get_model('clients', 'Client')
    TenantSubscription = apps.get_model('clients', 'TenantSubscription')

    silver = Plan.objects.filter(slug='auto-silver').first()
    if silver is None:
        return
    if silver.max_users < 2:
        silver.max_users = 2
        silver.save(update_fields=['max_users'])
    tenant_ids = TenantSubscription.objects.filter(plan=silver).values_list('tenant_id', flat=True)
    Client.objects.filter(pk__in=list(tenant_ids), max_users__lt=2).update(max_users=2)


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0088_platforminvoice_kind'),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
