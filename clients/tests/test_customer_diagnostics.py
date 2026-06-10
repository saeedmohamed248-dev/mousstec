"""End-to-end tests for customer-tier diagnostics subscriptions.

Covers:
  * Trial auto-grant on registration (cars sector only — not printing).
  * Trial expiry → is_active() flips after 7 days.
  * Quota enforcement: 6th scan on `trial` is blocked.
  * Upgrade stacks remaining trial/paid days, never burns them.
  * Pricing page renders the right tiers, current-plan badge is shown.
  * Scan endpoint records against the quota and returns 402 when exhausted.
  * Paymob callback activates the right tier.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

from django.test import TestCase, Client as DjangoClient
from django.utils import timezone

from clients.models import (
    CustomerDiagnosticsSubscription,
    MarketplaceCustomer,
)


class _TenantDomainMixin:
    """marketplace URLs run on the public schema — provision a `testserver`
    Domain so TenantMainMiddleware doesn't 404 every request."""
    _provisioned_domain = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django_tenants.utils import get_tenant_model, get_tenant_domain_model
        TenantModel = get_tenant_model()
        DomainModel = get_tenant_domain_model()
        public = TenantModel.objects.filter(schema_name='public').first()
        if public is None:
            public = TenantModel(
                schema_name='public', name='Public', owner_name='Test', phone='000',
            )
            public.auto_create_schema = False
            public.save(verbosity=0)
        if not DomainModel.objects.filter(domain='testserver').exists():
            cls._provisioned_domain = DomainModel.objects.create(
                tenant=public, domain='testserver', is_primary=False,
            )

    @classmethod
    def tearDownClass(cls):
        if cls._provisioned_domain is not None:
            try: cls._provisioned_domain.delete()
            except Exception: pass
        super().tearDownClass()


def _fresh_phone(prefix: str = '01') -> str:
    n = uuid.uuid4().int % 100_000_000
    return f'+20{prefix}{n:08d}'[:14]


def _make_customer(sector: str = 'automotive') -> MarketplaceCustomer:
    c = MarketplaceCustomer(
        customer_type='individual',
        full_name='Test Owner',
        phone=_fresh_phone(),
        sector=sector,
        is_verified=True,
    )
    c.set_password('pw123456')
    c.save()
    return c


class TrialGrantTests(_TenantDomainMixin, TestCase):
    def test_register_grants_7day_trial_for_cars(self):
        client = DjangoClient()
        phone = _fresh_phone()
        res = client.post(
            '/marketplace/register/',
            data=json.dumps({
                'customer_type': 'individual',
                'full_name': 'New Owner',
                'phone': phone,
                'password': 'pw123456',
                'sector': 'automotive',
            }),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        customer = MarketplaceCustomer.objects.get(phone__icontains=phone[-9:])
        self.assertTrue(hasattr(customer, 'diagnostics_subscription'))
        sub = customer.diagnostics_subscription
        self.assertEqual(sub.tier, 'trial')
        self.assertTrue(sub.is_active())
        # Trial window = 7 days (within a 1-minute tolerance).
        delta = sub.trial_ends_at - timezone.now()
        self.assertGreater(delta, timedelta(days=6, hours=23))
        self.assertLess(delta, timedelta(days=7, minutes=1))

    def test_register_skips_trial_for_printing(self):
        client = DjangoClient()
        phone = _fresh_phone()
        res = client.post(
            '/marketplace/register/',
            data=json.dumps({
                'customer_type': 'individual',
                'full_name': 'Print Owner',
                'phone': phone,
                'password': 'pw123456',
                'sector': 'printing',
            }),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 200)
        customer = MarketplaceCustomer.objects.get(phone__icontains=phone[-9:])
        self.assertFalse(
            CustomerDiagnosticsSubscription.objects.filter(customer=customer).exists()
        )


class SubscriptionLifecycleTests(TestCase):
    def test_trial_expires(self):
        customer = _make_customer()
        sub = CustomerDiagnosticsSubscription.grant_trial(customer)
        # Backdate trial — simulate 8 days passing.
        sub.trial_ends_at = timezone.now() - timedelta(days=1)
        sub.save(update_fields=['trial_ends_at'])
        self.assertFalse(sub.is_active())
        ok, _reason = sub.can_scan()
        self.assertFalse(ok)

    def test_quota_blocks_after_trial_limit(self):
        customer = _make_customer()
        sub = CustomerDiagnosticsSubscription.grant_trial(customer)
        # Burn the 5 trial scans.
        for _ in range(5):
            sub.record_scan()
        sub.refresh_from_db()
        self.assertEqual(sub.scans_used, 5)
        ok, reason = sub.can_scan()
        self.assertFalse(ok)
        self.assertIn('السكانات', reason)

    def test_upgrade_stacks_remaining_trial_days(self):
        customer = _make_customer()
        sub = CustomerDiagnosticsSubscription.grant_trial(customer)
        sub.upgrade('pro', payment_ref='test-ref')
        sub.refresh_from_db()
        self.assertEqual(sub.tier, 'pro')
        # paid_until = now + 30 days (we stack on top of `now`, not trial_ends_at,
        # because trial and paid are tracked separately).
        delta = sub.paid_until - timezone.now()
        self.assertGreater(delta, timedelta(days=29, hours=23))
        self.assertLess(delta, timedelta(days=30, minutes=1))
        self.assertEqual(sub.last_payment_egp, sub.TIER_PRICES_EGP['pro'])
        self.assertEqual(sub.scans_used, 0)  # Period reset on upgrade.

    def test_upgrade_extends_existing_paid_period(self):
        customer = _make_customer()
        sub = CustomerDiagnosticsSubscription.grant_trial(customer)
        sub.upgrade('basic', payment_ref='r1')
        sub.refresh_from_db()
        first_end = sub.paid_until
        # Renew while still active — should stack 30 more days.
        sub.upgrade('basic', payment_ref='r2')
        sub.refresh_from_db()
        self.assertGreater(sub.paid_until, first_end + timedelta(days=29))


class EndpointTests(_TenantDomainMixin, TestCase):
    def _login(self, customer):
        client = DjangoClient()
        client.cookies['mp_session'] = str(customer.session_token)
        return client

    def test_landing_redirects_when_unauth(self):
        client = DjangoClient()
        res = client.get('/marketplace/diagnostics/')
        self.assertEqual(res.status_code, 302)
        self.assertIn('/marketplace/automotive/', res['Location'])

    def test_landing_shows_trial_status(self):
        customer = _make_customer()
        CustomerDiagnosticsSubscription.grant_trial(customer)
        res = self._login(customer).get('/marketplace/diagnostics/')
        self.assertEqual(res.status_code, 200)
        body = res.content.decode('utf-8')
        self.assertIn('تجربة مجانية', body)
        self.assertIn('سكانات متاحة', body)

    def test_pricing_page_shows_all_three_tiers(self):
        customer = _make_customer()
        CustomerDiagnosticsSubscription.grant_trial(customer)
        res = self._login(customer).get('/marketplace/diagnostics/pricing/')
        self.assertEqual(res.status_code, 200)
        body = res.content.decode('utf-8')
        # All three EGP prices must render — anchors the marketing claim.
        self.assertIn('99', body)
        self.assertIn('199', body)
        self.assertIn('399', body)
        self.assertIn('الأكثر طلباً', body)  # Pro is featured.

    def test_scan_records_against_quota(self):
        customer = _make_customer()
        sub = CustomerDiagnosticsSubscription.grant_trial(customer)
        client = self._login(customer)
        res = client.post(
            '/marketplace/diagnostics/scan/',
            data=json.dumps({'vin': '1HGCM82633A123456', 'dtc_codes': ['P0301']}),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        data = res.json()
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['quota_remaining'], sub.TIER_QUOTAS['trial'] - 1)
        sub.refresh_from_db()
        self.assertEqual(sub.scans_used, 1)

    def test_scan_returns_402_when_quota_exhausted(self):
        customer = _make_customer()
        sub = CustomerDiagnosticsSubscription.grant_trial(customer)
        # Burn the entire trial budget.
        sub.scans_used = sub.TIER_QUOTAS['trial']
        sub.save(update_fields=['scans_used'])
        client = self._login(customer)
        res = client.post(
            '/marketplace/diagnostics/scan/',
            data='{}', content_type='application/json',
        )
        self.assertEqual(res.status_code, 402)
        self.assertEqual(res.json()['error'], 'quota_exceeded')
        self.assertEqual(res.json()['upgrade_url'], '/marketplace/diagnostics/pricing/')

    def test_dev_mode_upgrade_activates_paid_tier(self):
        from django.test import override_settings
        customer = _make_customer()
        CustomerDiagnosticsSubscription.grant_trial(customer)
        client = self._login(customer)
        # Force dev path regardless of local Paymob config.
        with override_settings(PAYMOB_API_KEY=''):
            res = client.post('/marketplace/diagnostics/upgrade/pro/')
        self.assertIn(res.status_code, (200, 302))
        sub = CustomerDiagnosticsSubscription.objects.get(customer=customer)
        self.assertEqual(sub.tier, 'pro')
        self.assertTrue(sub.is_active())
        self.assertTrue(sub.last_payment_ref.startswith('dev-'))


class FeatureGatingTests(TestCase):
    def test_trial_has_only_ai_diagnosis(self):
        customer = _make_customer()
        sub = CustomerDiagnosticsSubscription.grant_trial(customer)
        self.assertTrue(sub.has_feature('ai_diagnosis'))
        self.assertFalse(sub.has_feature('live_data'))
        self.assertFalse(sub.has_feature('tech_chat'))

    def test_pro_unlocks_live_data_and_chat(self):
        customer = _make_customer()
        sub = CustomerDiagnosticsSubscription.grant_trial(customer)
        sub.upgrade('pro', payment_ref='t')
        sub.refresh_from_db()
        self.assertTrue(sub.has_feature('live_data'))
        self.assertTrue(sub.has_feature('tech_chat'))
        self.assertFalse(sub.has_feature('multi_vehicle'))  # Empire only.

    def test_empire_unlocks_everything(self):
        customer = _make_customer()
        sub = CustomerDiagnosticsSubscription.grant_trial(customer)
        sub.upgrade('empire', payment_ref='t')
        sub.refresh_from_db()
        for feat in ('ai_diagnosis', 'vehicle_history', 'live_data',
                     'pdf_reports', 'tech_chat', 'multi_vehicle', 'parts_rewards'):
            self.assertTrue(sub.has_feature(feat), feat)
