"""⏳ حجوزات رسوم الفحص اللي جلستها ماخلصتش بتتقفل — ومابتتورثش لجلسة جديدة.

قبل كده الحجز «authorized» لجلسة وقعت في النص كان بيفضل مفتوح للأبد، وأي
جلسة على نفس العربية بعد أيام كانت بترجع نفس الحجز القديم (is_new=False).
"""
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.utils import timezone

from inventory.tests.base import ERPTenantTestCase

from bmw_ecu.models import DiagnosticFeeCharge
from bmw_ecu.services.billing_gate import LocalBillingGate, expire_stale_authorizations


class StaleFeeAuthorizationTests(ERPTenantTestCase):
    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)

    @staticmethod
    def _authorize(vin):
        """الـ sync core بتاع authorize — عشان الكويري يفضل على thread الاختبار
        (اللي عليه set_tenant)، زي test_granular_billing."""
        return LocalBillingGate._authorize_sync.func(vin, Decimal('450.00'), 'EGP', None)

    def _hold(self, vin, hours_ago, ref):
        row = DiagnosticFeeCharge.objects.create(
            vin=vin, amount=Decimal('450.00'), status='authorized', authorization_ref=ref)
        DiagnosticFeeCharge.objects.filter(pk=row.pk).update(
            authorized_at=timezone.now() - timedelta(hours=hours_ago))
        return row

    def test_reaper_expires_only_old_holds(self):
        old = self._hold('WBAOLD00000000001', 30, 'REF-OLD')
        fresh = self._hold('WBAFRESH000000001', 1, 'REF-FRESH')
        self.assertEqual(expire_stale_authorizations(), 1)
        old.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(old.status, 'expired')
        self.assertIsNotNone(old.finalised_at)
        self.assertEqual(fresh.status, 'authorized')

    def test_new_session_does_not_inherit_stale_hold(self):
        old = self._hold('WBASTALE000000001', 48, 'REF-STALE')
        auth = self._authorize('WBASTALE000000001')
        self.assertTrue(auth.is_new)
        self.assertNotEqual(auth.authorization_ref, 'REF-STALE')
        old.refresh_from_db()
        self.assertEqual(old.status, 'expired')
        # ولسه حجز واحد بس مفتوح للعربية
        self.assertEqual(DiagnosticFeeCharge.objects.filter(
            vin='WBASTALE000000001', status='authorized').count(), 1)

    def test_recent_hold_is_still_idempotent(self):
        self._hold('WBARECENT00000001', 2, 'REF-RECENT')
        auth = self._authorize('WBARECENT00000001')
        self.assertFalse(auth.is_new)
        self.assertEqual(auth.authorization_ref, 'REF-RECENT')

    def test_celery_task_runs_across_tenants(self):
        from bmw_ecu.tasks import expire_stale_fee_authorizations
        self._hold('WBATASK0000000001', 25, 'REF-TASK')
        out = expire_stale_fee_authorizations()
        connection.set_tenant(self.tenant)
        self.assertIn('1', out)
        self.assertEqual(DiagnosticFeeCharge.objects.get(authorization_ref='REF-TASK').status, 'expired')
