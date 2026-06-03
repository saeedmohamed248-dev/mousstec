"""The cache rule: a second resolve() of the same (dtc, vehicle_signature) NEVER hits the external provider."""
from django.db import connection

from smart_diagnostics.tests.base import DiagnosticsTenantTestCase
from smart_diagnostics.tests.factories import (
    get_or_create_premium_plan,
    attach_subscription,
)
from smart_diagnostics.services.adapters import MockDTCProvider
from smart_diagnostics.services.dtc_resolver import DTCResolver


class CacheBlocksDuplicateExternalCallTest(DiagnosticsTenantTestCase):

    def test_external_called_once_then_served_from_cache(self):
        from diagnostics_catalog.models import DTCExternalLookupCache
        from clients.models import TenantSubscription

        connection.set_schema_to_public()
        plan = get_or_create_premium_plan()
        attach_subscription(self.tenant, plan, quota_remaining=10)
        connection.set_tenant(self.tenant)

        # Use a code NOT present in the seeded catalog so it must hit external
        provider = MockDTCProvider(fixed_response={
            'short': 'Custom mock dtc',
            'full': 'Detailed.',
            'severity': 'high',
            'steps': [{'step': 1, 'title': 'Inspect', 'action': '...', 'expected': '...'}],
            'parts': ['OEM-X'],
        })
        resolver = DTCResolver(tenant=self.tenant, provider=provider)
        sig = 'BMW|F30'

        # First call → external (cache miss)
        r1, denial1 = resolver.resolve('P9999', vehicle_signature=sig)
        self.assertIsNone(denial1)
        self.assertEqual(r1.source, 'external')
        self.assertEqual(provider.call_count, 1)

        # Second call → catalog upsert OR cache, never external
        r2, denial2 = resolver.resolve('P9999', vehicle_signature=sig)
        self.assertIsNone(denial2)
        self.assertIn(r2.source, ('catalog', 'cache'))
        self.assertEqual(provider.call_count, 1, "External must NOT be called again")

        # Quota: only 1 consumed
        sub = TenantSubscription.objects.get(tenant=self.tenant)
        self.assertEqual(sub.diag_api_scans_used_total, 1)
        self.assertEqual(sub.diag_api_quota_remaining, 9)

        # Cache row exists
        self.assertEqual(
            DTCExternalLookupCache.objects.filter(
                dtc_code='P9999', vehicle_signature=sig, provider='mock'
            ).count(), 1
        )
