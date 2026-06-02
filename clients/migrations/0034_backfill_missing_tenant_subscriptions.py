"""
Backfill: every Client must have a TenantSubscription row so it appears in
the SaaS admin (TenantSubscription list) and so billing/entitlements logic
has something to read. Clients created via the signup flow before this
fix have none. Created here as inactive — admin activates manually.
"""
from django.db import migrations


def create_missing_subscriptions(apps, schema_editor):
    Client = apps.get_model('clients', 'Client')
    TenantSubscription = apps.get_model('clients', 'TenantSubscription')

    # public schema only — TenantSubscription is a shared model
    for client in Client.objects.exclude(schema_name='public'):
        TenantSubscription.objects.get_or_create(
            tenant=client,
            defaults={'is_active': False},
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0033_platform_invoice'),
    ]

    operations = [
        migrations.RunPython(create_missing_subscriptions, noop_reverse),
    ]
