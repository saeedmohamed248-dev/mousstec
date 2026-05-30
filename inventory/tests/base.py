"""
Base test case for Mouss Tec ERP multi-tenant tests.
Handles tenant schema creation/destruction with protected FK cleanup.
"""
from django.test import TransactionTestCase
from django.db import connection
from django_tenants.utils import get_tenant_model, get_tenant_domain_model


class ERPTenantTestCase(TransactionTestCase):
    """
    Base test case for multi-tenant ERP tests.
    Creates a tenant schema with migrations, switches connection to it,
    and cleans up after all tests in the class.

    Uses TransactionTestCase to avoid wrapping tests in a single
    transaction (which causes issues with schema_context switches
    and transaction.atomic blocks in services).
    """
    tenant = None
    domain = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Create tenant
        TenantModel = get_tenant_model()
        DomainModel = get_tenant_domain_model()

        cls.tenant = TenantModel(
            schema_name='test_erp',
            name='Test Company',
            owner_name='Test Owner',
            phone='01000000000',
        )
        cls.tenant.auto_create_schema = True
        cls.tenant.save(verbosity=0)

        # Create domain
        cls.domain = DomainModel(
            tenant=cls.tenant,
            domain='test-erp.test.com',
            is_primary=True,
        )
        cls.domain.save()

        # Switch connection to tenant schema
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()

        # Clean up protected FK objects that block tenant deletion
        try:
            from clients.models import EscrowLedger
            EscrowLedger.objects.filter(client=cls.tenant).delete()
        except Exception:
            pass
        try:
            from clients.models import AILimitTracker
            AILimitTracker.objects.filter(tenant=cls.tenant).delete()
        except Exception:
            pass
        try:
            from clients.models import AIBonusGrant
            AIBonusGrant.objects.filter(tenant=cls.tenant).delete()
        except Exception:
            pass

        if cls.domain:
            cls.domain.delete()
        if cls.tenant:
            cls.tenant.delete(force_drop=True)

        super().tearDownClass()
