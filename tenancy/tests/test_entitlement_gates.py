"""Regression coverage for the entitlement/quota gates landed in
June 2026. These tests pin the behavior of:

* ``EntitlementService.has`` against a Plan's entitlements dict.
* ``@require_feature`` view decorator (allow + deny + JSON shape).
* ``signals_quota`` quantitative guard on Branch/Treasury creation.
* ``signals_quota`` boolean-entitlement guard on MaintenanceContract.
* ``TenantQuotaMiddleware._resolve_plan_key`` slug/legacy fallback.

The middleware resolver is a pure function and is tested without DB
plumbing. Everything else runs against a per-class tenant schema via
:class:`ERPTenantTestCase` so signals fire on the real model save path.
"""
from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.http import JsonResponse
from django.test import RequestFactory, TestCase

from clients.middleware import TenantQuotaMiddleware
from clients.models import Client as TenantClient
from clients.models import Plan, TenantSubscription
from clients.services.entitlements import EntitlementService, require_feature
from inventory.models import Branch, MaintenanceContract, Treasury
from inventory.tests.base import ERPTenantTestCase

User = get_user_model()


# ─────────────────────────────────────────────────────────────────────
# Pure unit tests — no DB
# ─────────────────────────────────────────────────────────────────────
class ResolvePlanKeyTests(TestCase):
    """The middleware's tier resolver must prefer the FK slug over the
    legacy CharField, strip the industry prefix, and degrade gracefully
    when either side is missing."""

    def test_subscription_slug_wins_over_legacy_field(self):
        tenant = SimpleNamespace(
            plan='silver',  # legacy drift
            subscription=SimpleNamespace(plan=SimpleNamespace(slug='auto-empire')),
        )
        self.assertEqual(TenantQuotaMiddleware._resolve_plan_key(tenant), 'empire')

    def test_printing_slugs_resolve(self):
        tenant = SimpleNamespace(
            plan=None,
            subscription=SimpleNamespace(plan=SimpleNamespace(slug='print-pro')),
        )
        self.assertEqual(TenantQuotaMiddleware._resolve_plan_key(tenant), 'pro')

    def test_falls_back_to_legacy_when_subscription_missing(self):
        tenant = SimpleNamespace(plan='gold', subscription=None)
        self.assertEqual(TenantQuotaMiddleware._resolve_plan_key(tenant), 'gold')

    def test_unknown_when_nothing_set(self):
        tenant = SimpleNamespace(plan=None, subscription=None)
        self.assertEqual(TenantQuotaMiddleware._resolve_plan_key(tenant), 'unknown')

    def test_subscription_with_no_plan_falls_back_to_legacy(self):
        tenant = SimpleNamespace(
            plan='silver',
            subscription=SimpleNamespace(plan=None),
        )
        self.assertEqual(TenantQuotaMiddleware._resolve_plan_key(tenant), 'silver')


# ─────────────────────────────────────────────────────────────────────
# Tenant-schema integration tests
# ─────────────────────────────────────────────────────────────────────
class EntitlementGateTests(ERPTenantTestCase):
    """End-to-end tests against a freshly migrated tenant schema."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Set up plans + subscriptions in the public schema; tenants
        # always live there.
        connection.set_schema_to_public()
        cls.plan_silver, _ = Plan.objects.get_or_create(
            slug='test-silver',
            defaults=dict(
                name='Test Silver', industry='automotive',
                monthly_price=Decimal('550.00'),
                max_branches=1, max_users=1, max_treasuries=1,
                entitlements={'core_invoicing': {'enabled': True}},
            ),
        )
        cls.plan_empire, _ = Plan.objects.get_or_create(
            slug='test-empire',
            defaults=dict(
                name='Test Empire', industry='automotive',
                monthly_price=Decimal('2500.00'),
                max_branches=5, max_users=10, max_treasuries=4,
                entitlements={
                    'core_invoicing':           {'enabled': True},
                    'b2b_marketplace':          {'enabled': True},
                    'workshop_fleet_contracts': {'enabled': True},
                },
            ),
        )
        # Attach a subscription pointing the tenant at Silver by default;
        # individual tests rewire to Empire when they need the feature.
        TenantSubscription.objects.update_or_create(
            tenant=cls.tenant,
            defaults={'plan': cls.plan_silver, 'is_active': True},
        )
        connection.set_tenant(cls.tenant)

    def _set_plan(self, plan):
        """Swap the tenant's subscription onto the given plan and return
        a freshly-fetched tenant whose ``.subscription`` and ``.max_*``
        fields reflect the new state.

        We rebind ``self.tenant`` and ``connection.tenant`` so every
        downstream call (signal handlers, middleware, EntitlementService)
        sees the updated row instead of the Python instance cached in
        setUpClass.
        """
        connection.set_schema_to_public()
        sub = TenantSubscription.objects.get(tenant=self.tenant)
        sub.plan = plan
        # Reset the locked snapshot so effective_entitlements falls
        # through to Plan.entitlements (the fresh state). The
        # snapshot_from_plan signal only fires on create, so this
        # update keeps locked_at=None.
        sub.locked_at = None
        sub.locked_entitlements = {}
        sub.save(update_fields=['plan', 'locked_at', 'locked_entitlements'])
        # sync_limits_to_tenant() runs inside save() and writes
        # max_branches/max_users/max_treasuries onto the Client row —
        # but our self.tenant Python instance is stale until we re-fetch.
        self.tenant = TenantClient.objects.get(pk=self.tenant.pk)
        connection.set_tenant(self.tenant)

    # ── EntitlementService basics ────────────────────────────────────
    def test_has_returns_true_for_enabled_feature(self):
        self._set_plan(self.plan_empire)
        self.assertTrue(EntitlementService.has(self.tenant, 'b2b_marketplace'))

    def test_has_returns_false_when_feature_absent(self):
        self._set_plan(self.plan_silver)
        self.assertFalse(EntitlementService.has(self.tenant, 'b2b_marketplace'))

    # ── @require_feature decorator ───────────────────────────────────
    def test_require_feature_allows_when_enabled(self):
        self._set_plan(self.plan_empire)

        @require_feature('b2b_marketplace')
        def view(_request):
            return JsonResponse({'ok': True})

        req = RequestFactory().get('/api/anything/')
        resp = view(req)
        self.assertEqual(resp.status_code, 200)

    def test_require_feature_denies_with_json_for_api_path(self):
        self._set_plan(self.plan_silver)

        @require_feature('b2b_marketplace')
        def view(_request):
            return JsonResponse({'ok': True})

        req = RequestFactory().get('/api/whatever/')
        resp = view(req)
        self.assertEqual(resp.status_code, 403)
        # JsonResponse doesn't expose .json(); parse manually.
        body = json.loads(resp.content)
        self.assertEqual(body['code'], 'feature_not_in_plan')
        self.assertEqual(body['feature'], 'b2b_marketplace')

    def test_require_feature_denies_with_html_for_browser(self):
        self._set_plan(self.plan_silver)

        @require_feature('b2b_marketplace')
        def view(_request):
            return JsonResponse({'ok': True})

        req = RequestFactory().get('/inventory/b2b-market/')
        resp = view(req)
        self.assertEqual(resp.status_code, 403)
        self.assertIn(b'b2b_marketplace', resp.content)

    # ── Quantitative quota signals ───────────────────────────────────
    def test_treasury_creation_blocked_at_limit(self):
        """Silver plan allows 1 treasury — the second one must be blocked."""
        self._set_plan(self.plan_silver)
        branch = Branch.objects.create(name='Main', location='Cairo', phone='01000000001')

        Treasury.objects.create(name='Cash 1', branch=branch, type='cash', balance=0)
        with self.assertRaises(ValidationError):
            Treasury.objects.create(name='Cash 2', branch=branch, type='cash', balance=0)

    def test_treasury_creation_unlimited_when_plan_allows(self):
        """Empire plan allows 4 treasuries (per the seed)."""
        self._set_plan(self.plan_empire)
        # Refresh the cached tenant attrs by re-reading from public.
        connection.set_schema_to_public()
        t = type(self.tenant).objects.get(pk=self.tenant.pk)
        # Push the cached limits on the tenant row so the signal sees
        # the new allotment. In production a re-login or middleware
        # refresh handles this.
        t.max_treasuries = self.plan_empire.max_treasuries
        t.save(update_fields=['max_treasuries'])
        connection.set_tenant(t)

        branch = Branch.objects.create(name='Branch1', location='Cairo', phone='01000000002')
        for i in range(self.plan_empire.max_treasuries):
            Treasury.objects.create(
                name=f'T{i}', branch=branch, type='cash', balance=0,
            )
        # The 5th should fail.
        with self.assertRaises(ValidationError):
            Treasury.objects.create(name='T5', branch=branch, type='cash', balance=0)

    # ── Boolean entitlement signal ───────────────────────────────────
    def test_maintenance_contract_blocked_without_entitlement(self):
        """Silver doesn't include workshop_fleet_contracts — signal must veto."""
        self._set_plan(self.plan_silver)
        with self.assertRaises(ValidationError) as ctx:
            MaintenanceContract.objects.create(
                contract_code='C-001',
                total_value=Decimal('10000'),
                is_active=True,
            )
        self.assertIn('عقود أساطيل', str(ctx.exception))

    def test_maintenance_contract_allowed_with_entitlement(self):
        """Empire grants workshop_fleet_contracts — creation must pass."""
        self._set_plan(self.plan_empire)
        # MaintenanceContract requires customer/start_date/end_date — we
        # only need to prove the signal doesn't veto, so catch any *non*-
        # ValidationError that comes from missing FK fields and treat as
        # "signal passed". The signal raises ValidationError; field
        # checks raise IntegrityError / ValidationError-on-clean. To keep
        # the test focused, we just confirm the signal-level error
        # message does NOT appear.
        try:
            MaintenanceContract.objects.create(
                contract_code='C-002',
                total_value=Decimal('10000'),
                is_active=True,
            )
        except ValidationError as e:
            self.assertNotIn('عقود أساطيل', str(e))
        except Exception:
            # Any other persistence error means the signal already let
            # the row through — exactly what we want to assert.
            pass
