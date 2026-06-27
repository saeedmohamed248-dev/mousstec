"""Re-seed the catalog to add the Phase 3 service features.

Adds two saleable features — 'bidirectional_tests' (actuator / IO-control
tests) and 'tpms_service' (TPMS sensor read + relearn) — and re-links them
into the starter, lighting and full-suite packages. Idempotent
seed_catalog so tenants already on 0009 pick them up without disturbing
hand-added rows.
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
    Feature.objects.filter(
        code__in=("bidirectional_tests", "tpms_service")).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("bmw_ecu", "0009_seed_service_resets_feature"),
    ]

    operations = [
        migrations.RunPython(seed_forward, seed_backward),
    ]
