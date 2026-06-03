"""Base test case for smart_diagnostics — adapted from inventory.tests.base."""
from django.db import connection
from django.test import TransactionTestCase
from django_tenants.utils import get_tenant_model, get_tenant_domain_model


def _build_tenant(schema_name: str, domain: str):
    TenantModel = get_tenant_model()
    DomainModel = get_tenant_domain_model()
    t = TenantModel(
        schema_name=schema_name,
        name=f'Test {schema_name}',
        owner_name='Tester',
        phone='01000000000',
    )
    t.auto_create_schema = True
    t.save(verbosity=0)
    d = DomainModel(tenant=t, domain=domain, is_primary=True)
    d.save()
    return t, d


def _teardown_tenant(tenant, domain):
    connection.set_schema_to_public()
    try:
        from clients.models import EscrowLedger
        EscrowLedger.objects.filter(client=tenant).delete()
    except Exception:
        pass
    try:
        from clients.models import AILimitTracker, AIBonusGrant
        AILimitTracker.objects.filter(tenant=tenant).delete()
        AIBonusGrant.objects.filter(tenant=tenant).delete()
    except Exception:
        pass
    try:
        from clients.models import TenantSubscription
        TenantSubscription.objects.filter(tenant=tenant).delete()
    except Exception:
        pass
    if domain:
        domain.delete()
    if tenant:
        tenant.delete(force_drop=True)


def _ensure_tenant_row(tenant):
    """TransactionTestCase flushes auth/client tables between tests, deleting
    the tenant row (the schema survives). Re-INSERT the row preserving PK
    so FK references on cached Python objects remain valid."""
    from clients.models import Client
    if Client.objects.filter(pk=tenant.pk).exists():
        return
    # force INSERT with the same PK
    tenant.auto_create_schema = False
    tenant._state.adding = True
    tenant.save(force_insert=True, verbosity=0)


class DiagnosticsTenantTestCase(TransactionTestCase):
    """Single-tenant base. Sets up tenant 'test_diag' and switches connection."""
    tenant = None
    domain = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant, cls.domain = _build_tenant('test_diag', 'test-diag.test.com')
        connection.set_tenant(cls.tenant)

    def setUp(self):
        super().setUp()
        connection.set_schema_to_public()
        _ensure_tenant_row(self.tenant)
        connection.set_tenant(self.tenant)

    @classmethod
    def tearDownClass(cls):
        _teardown_tenant(cls.tenant, cls.domain)
        super().tearDownClass()


class TwoTenantTestCase(TransactionTestCase):
    """Sets up two tenants for isolation tests."""
    tenant_a = None
    tenant_b = None
    domain_a = None
    domain_b = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_a, cls.domain_a = _build_tenant('test_diag_a', 'a.test.com')
        cls.tenant_b, cls.domain_b = _build_tenant('test_diag_b', 'b.test.com')

    def setUp(self):
        super().setUp()
        connection.set_schema_to_public()
        _ensure_tenant_row(self.tenant_a)
        _ensure_tenant_row(self.tenant_b)

    @classmethod
    def tearDownClass(cls):
        _teardown_tenant(cls.tenant_a, cls.domain_a)
        _teardown_tenant(cls.tenant_b, cls.domain_b)
        super().tearDownClass()
