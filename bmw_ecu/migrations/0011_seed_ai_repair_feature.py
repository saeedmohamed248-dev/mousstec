"""Re-seed the catalog to add the AI Repair Assistant feature.

Adds the single saleable 'ai_repair_assistant' feature (self-verifying
Generator+Verifier repair-plan loop) and re-links it into the lighting,
repair-specialist and full-suite packages. Idempotent seed_catalog so
tenants already on 0010 pick it up without disturbing hand-added rows.
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
    Feature.objects.filter(code="ai_repair_assistant").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("bmw_ecu", "0010_seed_bidirectional_tpms_features"),
    ]

    operations = [
        migrations.RunPython(seed_forward, seed_backward),
    ]
