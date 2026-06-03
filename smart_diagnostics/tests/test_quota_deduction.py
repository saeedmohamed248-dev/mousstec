"""Quota is deducted on external API hits and refilled correctly."""
from django.db import connection

from smart_diagnostics.tests.base import DiagnosticsTenantTestCase
from smart_diagnostics.tests.factories import (
    get_or_create_premium_plan,
    attach_subscription,
)
from smart_diagnostics.services.quota import DiagnosticsQuotaService


class QuotaDeductionTest(DiagnosticsTenantTestCase):

    def test_deducts_on_consume(self):
        from clients.models import TenantSubscription
        connection.set_schema_to_public()
        plan = get_or_create_premium_plan()
        attach_subscription(self.tenant, plan, quota_remaining=3)
        connection.set_tenant(self.tenant)

        self.assertTrue(DiagnosticsQuotaService.consume_external_api_quota(self.tenant))
        self.assertTrue(DiagnosticsQuotaService.consume_external_api_quota(self.tenant))
        self.assertTrue(DiagnosticsQuotaService.consume_external_api_quota(self.tenant))
        # 4th call should fail — quota exhausted
        self.assertFalse(DiagnosticsQuotaService.consume_external_api_quota(self.tenant))

        sub = TenantSubscription.objects.get(tenant=self.tenant)
        self.assertEqual(sub.diag_api_quota_remaining, 0)
        self.assertEqual(sub.diag_api_scans_used_total, 3)

    def test_check_quota_returns_deny_when_zero(self):
        connection.set_schema_to_public()
        plan = get_or_create_premium_plan()
        attach_subscription(self.tenant, plan, quota_remaining=0)
        connection.set_tenant(self.tenant)

        gate = DiagnosticsQuotaService.check_external_api_quota(self.tenant)
        self.assertFalse(gate.allowed)
        self.assertIn('نفدت', gate.reason)

    def test_refill_action(self):
        from clients.models import TenantSubscription
        connection.set_schema_to_public()
        plan = get_or_create_premium_plan()
        sub = attach_subscription(self.tenant, plan, quota_remaining=5)
        sub.refill_diag_api_quota(50)
        sub.refresh_from_db()
        self.assertEqual(sub.diag_api_quota_remaining, 55)
