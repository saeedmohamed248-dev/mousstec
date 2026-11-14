"""🌱 زرع كتالوج السوق المبدئي (متاجر Mouss Tec للمطابع وقطع الغيار) — idempotent."""
from django.db import migrations


def seed(apps, schema_editor):
    from django.db import connection
    if getattr(connection, 'schema_name', 'public') != 'public':
        return  # الجداول في public بس
    from clients.catalog_seed import seed_catalog
    seed_catalog(apps.get_model('clients', 'CatalogStore'), apps.get_model('clients', 'CatalogItem'))


class Migration(migrations.Migration):
    dependencies = [('clients', '0113_marketplace_catalog')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
