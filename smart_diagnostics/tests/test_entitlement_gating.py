"""Subscription-tier gating: a basic-plan tenant must be blocked from diagnostics features."""
from django.db import connection

from smart_diagnostics.tests.base import DiagnosticsTenantTestCase
from smart_diagnostics.tests.factories import (
    get_or_create_premium_plan,
    get_or_create_basic_plan,
    attach_subscription,
)
from smart_diagnostics.services.quota import (
    DiagnosticsQuotaService,
    FEATURE_LIVE_DATA,
    FEATURE_GUIDED_TESTS,
    FEATURE_EXTERNAL_API,
)


class EntitlementGatingTest(DiagnosticsTenantTestCase):

    def test_premium_plan_allows_features(self):
        connection.set_schema_to_public()
        plan = get_or_create_premium_plan()
        attach_subscription(self.tenant, plan, quota_remaining=10)
        connection.set_tenant(self.tenant)

        self.assertTrue(DiagnosticsQuotaService.check_feature(self.tenant, FEATURE_LIVE_DATA).allowed)
        self.assertTrue(DiagnosticsQuotaService.check_feature(self.tenant, FEATURE_GUIDED_TESTS).allowed)

    def test_basic_plan_denies_features(self):
        connection.set_schema_to_public()
        plan = get_or_create_basic_plan()
        attach_subscription(self.tenant, plan)
        connection.set_tenant(self.tenant)

        gate = DiagnosticsQuotaService.check_feature(self.tenant, FEATURE_LIVE_DATA)
        self.assertFalse(gate.allowed)
        self.assertTrue(gate.upgrade_required)

    def test_inactive_subscription_denies(self):
        connection.set_schema_to_public()
        plan = get_or_create_premium_plan()
        attach_subscription(self.tenant, plan, active=False)
        connection.set_tenant(self.tenant)

        gate = DiagnosticsQuotaService.check_feature(self.tenant, FEATURE_LIVE_DATA)
        self.assertFalse(gate.allowed)

    def test_no_subscription_denies(self):
        # Nothing attached
        connection.set_tenant(self.tenant)
        gate = DiagnosticsQuotaService.check_feature(self.tenant, FEATURE_EXTERNAL_API)
        self.assertFalse(gate.allowed)
        self.assertTrue(gate.upgrade_required)
