"""Verify the monthly quota refill task resets diag_api_quota_remaining to plan limit."""
from django.db import connection

from smart_diagnostics.tests.base import DiagnosticsTenantTestCase
from smart_diagnostics.tests.factories import (
    get_or_create_premium_plan,
    get_or_create_basic_plan,
    attach_subscription,
)
from smart_diagnostics.tasks import monthly_refill_diag_api_quotas


class MonthlyRefillTaskTest(DiagnosticsTenantTestCase):

    def test_refills_premium_subscriber_to_plan_limit(self):
        from clients.models import TenantSubscription
        connection.set_schema_to_public()
        plan = get_or_create_premium_plan()
        attach_subscription(self.tenant, plan, quota_remaining=3)

        result = monthly_refill_diag_api_quotas()

        sub = TenantSubscription.objects.get(tenant=self.tenant)
        # Premium plan has monthly_limit=200 in seed
        self.assertEqual(sub.diag_api_quota_remaining, 200)
        self.assertGreaterEqual(result['refilled'], 1)

    def test_skips_non_subscriber(self):
        from clients.models import TenantSubscription
        connection.set_schema_to_public()
        plan = get_or_create_basic_plan()
        sub = attach_subscription(self.tenant, plan, quota_remaining=5)

        result = monthly_refill_diag_api_quotas()

        sub.refresh_from_db()
        self.assertEqual(sub.diag_api_quota_remaining, 5, "Basic plan should not be refilled")
