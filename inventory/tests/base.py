"""
Base test case for Mouss Tec ERP multi-tenant tests.
Handles tenant schema creation/destruction with protected FK cleanup.
"""
import uuid

from django.test import TransactionTestCase
from django.db import connection
from django_tenants.utils import get_tenant_model, get_tenant_domain_model


class ERPTenantTestCase(TransactionTestCase):
    """
    Base test case for multi-tenant ERP tests.
    Creates a UNIQUE tenant schema per class with migrations, switches
    connection to it, and cleans up after all tests in the class.

    Uses TransactionTestCase to avoid wrapping tests in a single
    transaction (which causes issues with schema_context switches
    and transaction.atomic blocks in services).
    """
    tenant = None
    domain = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # 🐛 [test-coverage FIX #1]: Multiple test classes in the same process
        # can leave the connection on a previous test's tenant schema.
        # django_tenants refuses to create a tenant outside `public` schema.
        connection.set_schema_to_public()

        # 🐛 [test-coverage FIX #2]: Unique tenant per class — if a previous
        # tearDownClass crashed mid-cleanup (e.g. a new protected FK landed
        # that the cleanup list doesn't know about), the schema_name 'test_erp'
        # row stayed and the NEXT class crashed on the unique constraint.
        # Using a short uuid suffix makes setUpClass robust against partial
        # teardowns from prior classes in the same run.
        cls._schema_suffix = uuid.uuid4().hex[:6]
        schema_name = f'test_{cls._schema_suffix}'
        domain_name = f'test-{cls._schema_suffix}.test.com'

        TenantModel = get_tenant_model()
        DomainModel = get_tenant_domain_model()

        # max_*=0 → unlimited (matches the production "0 = unlimited"
        # convention in tenancy.signals.quota._enforce). Without this,
        # the post_schema_sync seed creates 1 default branch and the
        # default plan's 2-branch cap blocks tests that need 3+ branches.
        cls.tenant = TenantModel(
            schema_name=schema_name,
            name='Test Company',
            owner_name='Test Owner',
            phone='01000000000',
            max_branches=0,
            max_users=0,
            max_treasuries=0,
        )
        cls.tenant.auto_create_schema = True
        cls.tenant.save(verbosity=0)

        cls.domain = DomainModel(
            tenant=cls.tenant,
            domain=domain_name,
            is_primary=True,
        )
        cls.domain.save()

        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()

        # Clean up protected FK objects that block tenant deletion.
        # NB: This list grew over time as new shared apps added FKs to Client.
        # If a future class adds a new protected FK and the test fails with
        # "tenant.delete() failed", add the cleanup here.
        protected_fk_cleaners = [
            ('clients.models', 'EscrowLedger', 'client'),
            ('clients.models', 'AILimitTracker', 'tenant'),
            ('clients.models', 'AIBonusGrant', 'tenant'),
            ('clients.models', 'TenantSubscription', 'tenant'),
            ('clients.models', 'PlatformInvoice', 'tenant'),
            ('clients.models', 'TenantDesignTopUp', 'tenant'),
            ('clients.models', 'GlobalB2BMarketplace', 'tenant'),
        ]
        for module_path, model_name, fk_field in protected_fk_cleaners:
            try:
                import importlib
                model = getattr(importlib.import_module(module_path), model_name)
                model.objects.filter(**{fk_field: cls.tenant}).delete()
            except Exception:
                pass

        if cls.domain:
            try:
                cls.domain.delete()
            except Exception:
                pass
        if cls.tenant:
            try:
                cls.tenant.delete(force_drop=True)
            except Exception:
                pass

        super().tearDownClass()
