"""Re-seed the catalog to add the ECU Flashing feature.

Adds the single saleable 'ecu_flashing' feature (guided, backup-enforced,
auto-rollback firmware update for DME / FEM-BDC / KOMBI / EGS) and
re-links it into the repair-specialist and full-suite packages. The
seed_catalog is idempotent (update_or_create + features.set) so tenants
already on 0011 pick it up without disturbing hand-added rows.
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
    Feature.objects.filter(code="ecu_flashing").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("bmw_ecu", "0011_seed_ai_repair_feature"),
    ]

    operations = [
        migrations.RunPython(seed_forward, seed_backward),
    ]
