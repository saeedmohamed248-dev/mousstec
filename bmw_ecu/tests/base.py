"""Base test case for bmw_ecu tests that need to hit the tenant DB.

bmw_ecu lives in TENANT_APPS, so its tables exist only inside per-workshop
schemas. The existing 70 unit tests never touch the DB (they test pure
logic + mocks), so they get away with a plain `unittest.TestCase`. Tests
that exercise the granular billing tables MUST run against an actual
tenant schema, otherwise every Feature.objects.get() raises
`relation "bmw_ecu_feature" does not exist`.

Pattern intentionally mirrors smart_diagnostics.tests.base — same
TransactionTestCase + auto_create_schema flow so the data migration
(0006_seed_initial_features) populates the Feature catalog automatically
on schema creation.
"""
from __future__ import annotations

from django.db import connection
from django.test import TransactionTestCase
from django_tenants.utils import get_tenant_domain_model, get_tenant_model


def _build_tenant(schema_name: str, domain: str):
    TenantModel = get_tenant_model()
    DomainModel = get_tenant_domain_model()
    t = TenantModel(
        schema_name=schema_name,
        name=f"Test {schema_name}",
        owner_name="Tester",
        phone="01000000000",
    )
    t.auto_create_schema = True
    t.save(verbosity=0)
    d = DomainModel(tenant=t, domain=domain, is_primary=True)
    d.save()
    return t, d


def _teardown_tenant(tenant, domain) -> None:
    connection.set_schema_to_public()
    # Cascading deletes on the public-schema rows that block tenant drop.
    for label, model_path in [
        ("EscrowLedger", "clients.models.EscrowLedger"),
        ("AILimitTracker", "clients.models.AILimitTracker"),
        ("AIBonusGrant", "clients.models.AIBonusGrant"),
        ("TenantSubscription", "clients.models.TenantSubscription"),
    ]:
        try:
            module, attr = model_path.rsplit(".", 1)
            from importlib import import_module
            Model = getattr(import_module(module), attr)
            field = "client" if label == "EscrowLedger" else "tenant"
            Model.objects.filter(**{field: tenant}).delete()
        except Exception:
            pass
    if domain:
        domain.delete()
    if tenant:
        tenant.delete(force_drop=True)


def _ensure_tenant_row(tenant) -> None:
    """TransactionTestCase flushes auth/client tables between tests, deleting
    the tenant row (the schema survives). Re-INSERT the row preserving PK so
    FK references on cached Python objects remain valid."""
    from clients.models import Client
    if Client.objects.filter(pk=tenant.pk).exists():
        return
    tenant.auto_create_schema = False
    tenant._state.adding = True
    tenant.save(force_insert=True, verbosity=0)


# ─────────────────────────────────────────────────────────────────────
# Module-level tenant — created ONCE per test module by setUpModule(),
# torn down ONCE by tearDownModule(). Avoids the per-class 40-second
# schema-migration cost when several test classes live in one file.
# ─────────────────────────────────────────────────────────────────────
_MODULE_TENANT = None
_MODULE_DOMAIN = None


def setup_module_tenant(schema_name: str = "test_bmw_ecu",
                        domain: str = "test-bmw-ecu.test.com"):
    """Call from a test module's setUpModule() to provision the tenant once."""
    global _MODULE_TENANT, _MODULE_DOMAIN
    connection.set_schema_to_public()
    _MODULE_TENANT, _MODULE_DOMAIN = _build_tenant(schema_name, domain)
    connection.set_tenant(_MODULE_TENANT)
    return _MODULE_TENANT, _MODULE_DOMAIN


def teardown_module_tenant() -> None:
    """Call from a test module's tearDownModule()."""
    global _MODULE_TENANT, _MODULE_DOMAIN
    _teardown_tenant(_MODULE_TENANT, _MODULE_DOMAIN)
    _MODULE_TENANT = None
    _MODULE_DOMAIN = None


class BmwEcuTenantTestCase(TransactionTestCase):
    """Per-test-class base. Assumes the module already provisioned the
    tenant via setup_module_tenant() — re-seeds the catalog between
    tests because TransactionTestCase flushes every table.
    """

    @classmethod
    def tenant(cls):
        return _MODULE_TENANT

    def setUp(self):
        super().setUp()
        connection.set_schema_to_public()
        _ensure_tenant_row(_MODULE_TENANT)
        connection.set_tenant(_MODULE_TENANT)
        # Re-seed the Feature + SubscriptionPackage catalog after each flush.
        from bmw_ecu._seed_data import seed_catalog
        from bmw_ecu.models import Feature, SubscriptionPackage
        seed_catalog(Feature=Feature, SubscriptionPackage=SubscriptionPackage)
