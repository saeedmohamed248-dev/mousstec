"""Re-seed the catalog to add the Service Resets feature.

Adds the single saleable 'service_resets' feature (oil / EPB / SAS / DPF
/ throttle) and re-links it into the starter, lighting and full-suite
packages. Idempotent seed_catalog so tenants already on 0008 pick it up
without disturbing hand-added rows.
"""
from __future__ import annotations

from django.db import migrations


def seed_forward(apps, schema_editor):
    Feature = apps.get_model("bmw_ecu", "Feature")
    SubscriptionPackage = apps.get_model("bmw_ecu", "SubscriptionPackage")
    from bmw_ecu._seed_data import seed_catalog
    seed_catalog(Feature=Feature, SubscriptionPackage=SubscriptionPackage)


def seed_backward(apps, schema_editor):
    Feature = apps.get_model("bmw_ecu", "Feature")
    Feature.objects.filter(code="service_resets").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("bmw_ecu", "0008_seed_scan_features"),
    ]

    operations = [
        migrations.RunPython(seed_forward, seed_backward),
    ]
