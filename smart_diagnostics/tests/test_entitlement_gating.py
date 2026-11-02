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
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(status='suspended')   # مش تجربة
        self.tenant.status = 'suspended'
        connection.set_tenant(self.tenant)

        gate = DiagnosticsQuotaService.check_feature(self.tenant, FEATURE_LIVE_DATA)
        self.assertFalse(gate.allowed)

    def test_no_subscription_denies(self):
        # Nothing attached
        connection.set_tenant(self.tenant)
        gate = DiagnosticsQuotaService.check_feature(self.tenant, FEATURE_EXTERNAL_API)
        self.assertFalse(gate.allowed)
        self.assertTrue(gate.upgrade_required)


class FreeTrialDiagnosticsTest(DiagnosticsTenantTestCase):
    """🧪 التجربة المجانية: التشخيص اللي مالوش تكلفة مفتوح لـ 5 عربيات، والفحوصات الخارجية لأ."""

    def _trial(self, ends_in_days=7):
        from datetime import timedelta
        from django.utils import timezone
        connection.set_schema_to_public()
        attach_subscription(self.tenant, get_or_create_premium_plan(), active=False)
        ends = timezone.localdate() + timedelta(days=ends_in_days)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(status='trial', trial_ends_at=ends)
        self.tenant.status, self.tenant.trial_ends_at = 'trial', ends
        connection.set_tenant(self.tenant)

    def test_trial_opens_free_features_but_not_paid_external_scans(self):
        self._trial()
        self.assertTrue(DiagnosticsQuotaService.check_feature(self.tenant, FEATURE_LIVE_DATA).allowed)
        self.assertTrue(DiagnosticsQuotaService.check_feature(self.tenant, FEATURE_GUIDED_TESTS).allowed)
        self.assertFalse(DiagnosticsQuotaService.check_feature(self.tenant, FEATURE_EXTERNAL_API).allowed)

    def test_expired_trial_is_closed(self):
        self._trial(ends_in_days=-1)
        self.assertFalse(DiagnosticsQuotaService.check_feature(self.tenant, FEATURE_GUIDED_TESTS).allowed)

    def test_trial_room_scans_stop_at_five_and_do_not_reset_monthly(self):
        from datetime import date
        from unittest import mock
        from clients.models import Plan, TenantSubscription
        from clients.services import diagnostics_quota as dq
        self._trial()
        connection.set_schema_to_public()
        sub = TenantSubscription.objects.get(tenant=self.tenant)
        Plan.objects.filter(pk=sub.plan_id).update(monthly_diagnostics_scans_quota=70)
        tenant = type(self.tenant).objects.get(pk=self.tenant.pk)
        for _ in range(5):
            self.assertTrue(dq.consume_quota(tenant, kind='scan').allowed)
        self.assertFalse(dq.consume_quota(tenant, kind='scan').allowed)
        type(tenant).objects.filter(pk=tenant.pk).update(trial_ends_at=date(2099, 1, 10))
        tenant = type(tenant).objects.get(pk=tenant.pk)
        with mock.patch.object(dq, '_today', return_value=date(2099, 1, 2)):      # شهر جديد جوه التجربة
            self.assertFalse(dq.check_quota(tenant, kind='scan').allowed)
        connection.set_tenant(self.tenant)
