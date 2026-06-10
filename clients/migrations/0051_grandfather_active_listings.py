"""
Grandfather data migration.

When moderation was retrofitted onto the marketplace, every existing
PartListing defaulted to ``moderation_status='pending_approval'`` — which
would silently disappear every live seller's inventory from the public
feed. Global marketplaces (eBay, Amazon, Etsy) all handle this the same
way when adding new gates to existing inventory: grandfather what was
already public.

Policy:
  * status='active'  → moderation_status='approved'     (was live, stays live)
  * status='draft'   → moderation_status='pending_approval' (not yet listed)
  * anything else (reserved/sold/removed) → 'approved'  (historical orders rely
                                            on listings being findable by id)
"""
from django.db import migrations
from django.utils import timezone


def grandfather_listings(apps, schema_editor):
    PartListing = apps.get_model('clients', 'PartListing')
    now = timezone.now()
    PartListing.objects.exclude(status='draft').update(
        moderation_status='approved',
        moderated_at=now,
        rejection_reason='',
    )


def reverse_grandfather(apps, schema_editor):
    # Re-pending everything is destructive and wrong. The reverse is a no-op —
    # if you really need to roll back, do it manually.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0050_marketplacecustomer_deleted_at_and_more'),
    ]

    operations = [
        migrations.RunPython(grandfather_listings, reverse_grandfather),
    ]
