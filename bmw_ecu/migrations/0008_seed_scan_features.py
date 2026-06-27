"""Re-seed the catalog to add the Full-System Scan + Live Data features.

The feature definitions live in bmw_ecu/_seed_data.py (single source of
truth). 0006 seeded the original 12 features; this migration re-runs the
same idempotent seed_catalog so tenants that already applied 0006 pick up
the two new diagnostic features ('full_system_scan', 'live_data_stream')
and the refreshed package → feature links — without disturbing any rows
an admin added by hand (update_or_create / features.set).
"""
from __future__ import annotations

from django.db import migrations


def seed_forward(apps, schema_editor):
    Feature = apps.get_model("bmw_ecu", "Feature")
    SubscriptionPackage = apps.get_model("bmw_ecu", "SubscriptionPackage")
    from bmw_ecu._seed_data import seed_catalog
    seed_catalog(Feature=Feature, SubscriptionPackage=SubscriptionPackage)


def seed_backward(apps, schema_editor):
    """Remove only the two features this migration introduced. The
    package links to them disappear automatically with the M2M rows."""
    Feature = apps.get_model("bmw_ecu", "Feature")
    Feature.objects.filter(
        code__in=["full_system_scan", "live_data_stream"]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("bmw_ecu", "0007_unique_consume_per_op_ref"),
    ]

    operations = [
        migrations.RunPython(seed_forward, seed_backward),
    ]
