"""
CarMD adapter — mocked HTTP integration tests.
Covers: auth headers, payload parsing, error handling, cost attribution,
quota refund on provider failure.
"""
from decimal import Decimal
from unittest.mock import patch, MagicMock

import requests

from django.db import connection
from django.test import override_settings

from smart_diagnostics.services.adapters import CarMDDTCProvider
from smart_diagnostics.services.dtc_resolver import DTCResolver
from smart_diagnostics.tests.base import DiagnosticsTenantTestCase
from smart_diagnostics.tests.factories import (
    get_or_create_premium_plan,
    attach_subscription,
)


def _fake_response(json_payload, status_code=200):
    r = MagicMock(spec=requests.Response)
    r.json.return_value = json_payload
    r.status_code = status_code
    r.raise_for_status = MagicMock()
    if status_code >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status_code}")
    return r


class CarMDAdapterUnitTest(DiagnosticsTenantTestCase):
    """Direct calls into the CarMDDTCProvider, mocking requests.get."""

    def test_sends_auth_headers_and_parses_response(self):
        provider = CarMDDTCProvider(auth_key='AUTH123', partner_token='PART456')
        payload = {
            'data': {
                'description': 'Misfire',
                'repair': 'Replace coil',
                'severity': 'high',
                'steps': [{'step': 1, 'title': 's', 'action': 'a', 'expected': 'e'}],
                'parts': ['OEM-IGN-1'],
            }
        }
        with patch('smart_diagnostics.services.adapters.requests.get') as mock_get:
            mock_get.return_value = _fake_response(payload)
            result = provider.lookup('P0301', vehicle_signature='BMW|F30')

        # Headers and params asserted
        call_kwargs = mock_get.call_args.kwargs
        self.assertEqual(call_kwargs['headers']['authorization'], 'Basic AUTH123')
        self.assertEqual(call_kwargs['headers']['partner-token'], 'PART456')
        self.assertEqual(call_kwargs['params']['dtc'], 'P0301')
        self.assertEqual(call_kwargs['params']['vehicle'], 'BMW|F30')

        # Parsed result
        self.assertEqual(result.short_description, 'Misfire')
        self.assertEqual(result.severity, 'high')
        self.assertEqual(result.likely_oem_parts, ['OEM-IGN-1'])
        self.assertEqual(result.provider, 'carmd')

    def test_raises_without_credentials(self):
        provider = CarMDDTCProvider(auth_key='', partner_token='')
        with self.assertRaises(RuntimeError):
            provider.lookup('P0301')

    def test_propagates_http_error(self):
        provider = CarMDDTCProvider(auth_key='K', partner_token='T')
        with patch('smart_diagnostics.services.adapters.requests.get') as mock_get:
            mock_get.return_value = _fake_response({'error': 'rate-limited'}, status_code=429)
            with self.assertRaises(requests.HTTPError):
                provider.lookup('P0301')


class CarMDViaResolverTest(DiagnosticsTenantTestCase):
    """End-to-end through DTCResolver — quota deduction, APICallLog, cache write."""

    def test_quota_deducted_and_logged_and_cached(self):
        from clients.models import TenantSubscription
        from diagnostics_catalog.models import DTCExternalLookupCache, APICostRate
        from smart_diagnostics.models import APICallLog

        connection.set_schema_to_public()
        plan = get_or_create_premium_plan()
        attach_subscription(self.tenant, plan, quota_remaining=10)
        # Ensure CarMD cost rate exists at $0.07
        APICostRate.objects.update_or_create(
            provider='carmd', endpoint='dtc_lookup',
            defaults={'cost_usd': Decimal('0.07'), 'is_active': True},
        )
        connection.set_tenant(self.tenant)

        provider = CarMDDTCProvider(auth_key='K', partner_token='T')
        resolver = DTCResolver(tenant=self.tenant, provider=provider)

        payload = {'data': {
            'description': 'Catalyst inefficient', 'repair': 'Replace cat',
            'severity': 'medium', 'steps': [], 'parts': ['CAT-001'],
        }}
        with patch('smart_diagnostics.services.adapters.requests.get') as mock_get:
            mock_get.return_value = _fake_response(payload)
            # Use a code NOT in seeded catalog so it must call external
            resolved, denial = resolver.resolve('P9991', vehicle_signature='BMW|F30')

        self.assertIsNone(denial)
        self.assertEqual(resolved.source, 'external')
        self.assertEqual(resolved.short_description, 'Catalyst inefficient')

        # Quota -1
        sub = TenantSubscription.objects.get(tenant=self.tenant)
        self.assertEqual(sub.diag_api_quota_remaining, 9)
        self.assertEqual(sub.diag_api_scans_used_total, 1)

        # APICallLog with cost recorded
        log = APICallLog.objects.filter(provider='carmd', dtc_code='P9991').first()
        self.assertIsNotNone(log)
        self.assertFalse(log.cache_hit)
        self.assertEqual(log.cost_usd, Decimal('0.07'))

        # Cache row exists — second call MUST NOT trigger HTTP
        self.assertEqual(
            DTCExternalLookupCache.objects.filter(
                dtc_code='P9991', vehicle_signature='BMW|F30', provider='carmd'
            ).count(), 1
        )
        with patch('smart_diagnostics.services.adapters.requests.get') as mock_get2:
            r2, d2 = resolver.resolve('P9991', vehicle_signature='BMW|F30')
            self.assertIsNone(d2)
            self.assertIn(r2.source, ('catalog', 'cache'))
            mock_get2.assert_not_called()

    def test_quota_refunded_on_provider_failure(self):
        from clients.models import TenantSubscription
        from smart_diagnostics.models import APICallLog

        connection.set_schema_to_public()
        plan = get_or_create_premium_plan()
        attach_subscription(self.tenant, plan, quota_remaining=5)
        connection.set_tenant(self.tenant)

        provider = CarMDDTCProvider(auth_key='K', partner_token='T')
        resolver = DTCResolver(tenant=self.tenant, provider=provider)

        with patch('smart_diagnostics.services.adapters.requests.get') as mock_get:
            mock_get.return_value = _fake_response({'error': 'down'}, status_code=503)
            with self.assertRaises(requests.HTTPError):
                resolver.resolve('P9992', vehicle_signature='BMW|F30')

        sub = TenantSubscription.objects.get(tenant=self.tenant)
        # Started at 5, deducted to 4, then refunded back to 5 on failure
        self.assertEqual(sub.diag_api_quota_remaining, 5)

        # Error logged
        log = APICallLog.objects.filter(provider='carmd', dtc_code='P9992').first()
        self.assertIsNotNone(log)
        self.assertNotEqual(log.error, '')
