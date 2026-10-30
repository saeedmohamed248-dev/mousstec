"""
👥 Silver = مستخدمين بدل واحد

باقة Silver كانت مستخدم واحد بس — يعني صاحب المحل نفسه، ومايقدرش يضيف كاشير من غير إضافة
مدفوعة. المنافسين (دفترة / أودو) أغلى بكتير وفيهم أكتر من مستخدم، فالمستخدم التاني بقى جوه
الباقة بنفس السعر (550). المشتركين الحاليين على Silver بياخدوه فوراً.
"""
from django.db import migrations


def forward(apps, schema_editor):
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
