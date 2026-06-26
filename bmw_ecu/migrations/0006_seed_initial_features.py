"""Seed the initial Feature catalog + a couple of starter SubscriptionPackages.

The actual data + the seeding logic live in bmw_ecu/_seed_data.py so the
same constants stay in sync between this migration and the test setUp
that reseeds the catalog after each TransactionTestCase flush.

Idempotent (update_or_create) so re-running the migration after an admin
edit will refresh shape but never blow away unrelated rows admins added
by hand.
"""
from __future__ import annotations

from django.db import migrations


def seed_forward(apps, schema_editor):
    Feature = apps.get_model("bmw_ecu", "Feature")
    SubscriptionPackage = apps.get_model("bmw_ecu", "SubscriptionPackage")
    from bmw_ecu._seed_data import seed_catalog
    seed_catalog(Feature=Feature, SubscriptionPackage=SubscriptionPackage)


def seed_backward(apps, schema_editor):
    """Remove only the rows this migration seeded — anything an admin
    added by hand stays."""
    Feature = apps.get_model("bmw_ecu", "Feature")
    SubscriptionPackage = apps.get_model("bmw_ecu", "SubscriptionPackage")
    from bmw_ecu._seed_data import INITIAL_FEATURES, INITIAL_PACKAGES
    SubscriptionPackage.objects.filter(
        code__in=[row[0] for row in INITIAL_PACKAGES]
    ).delete()
    Feature.objects.filter(
        code__in=[row[0] for row in INITIAL_FEATURES]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("bmw_ecu", "0005_granular_billing_models"),
    ]
    operations = [
        migrations.RunPython(seed_forward, seed_backward),
    ]
