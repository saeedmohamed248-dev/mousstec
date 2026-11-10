"""🌱 الدفعة التانية من منتجات المطابع (١٠٩ منتج + ١٠ متاجر + ٣ أقسام) — idempotent.

نفس seed_catalog: القديم بيتعدّى (get_or_create) والجديد بس بيتضاف، ومابيكتبش فوق تعديلات الأدمن.
"""
from django.db import migrations


def seed(apps, schema_editor):
    from django.db import connection
    if getattr(connection, 'schema_name', 'public') != 'public':
        return  # الجداول في public بس
    from clients.catalog_seed import seed_catalog
    seed_catalog(apps.get_model('clients', 'CatalogStore'), apps.get_model('clients', 'CatalogItem'))


class Migration(migrations.Migration):
    dependencies = [('clients', '0117_catalog_request_attachment')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
